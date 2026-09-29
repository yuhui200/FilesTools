"""上传文件的类型校验。

校验分三层，任何一层不过都直接拒绝：
1. 扩展名是否在白名单内；
2. 文件头（magic bytes）是否与声称的类型一致 —— 防止 .exe 改名成 .jpg；
3. Pillow 能否真正解码 —— 防止文件头正确但内容是损坏/伪造的图片。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from config import settings
from utils.errors import CorruptedFileError, UnsupportedTypeError, ValidationError

# 各格式的文件头特征
#
# 顺序有讲究：``sniff_format`` 按插入顺序比对，短签名（BMP 的 ``BM`` 只有
# 两个字节）必须排在长签名之后，否则一个以 ``BM`` 开头的别的东西会被先认成
# BMP。不过这一层从来不是唯一防线 —— 认下来之后还要过 ``verify_image``
# 的 Pillow 真解码，魔数撞车最多是「多读了 16 个字节」。
_MAGIC_SIGNATURES: dict[str, list[bytes]] = {
    "jpeg": [b"\xff\xd8\xff"],
    "png": [b"\x89PNG\r\n\x1a\n"],
    "webp": [b"RIFF"],  # WEBP 需要额外检查第 8-12 字节
    "tiff": [b"II*\x00", b"MM\x00*"],  # 小端与大端两种字节序
    "gif": [b"GIF87a", b"GIF89a"],
    "bmp": [b"BM"],
    # HEIC / HEIF **没有固定前缀**，见 :data:`_MAGIC_FOLLOWUPS`。
    # 空列表不是遗漏，是「这个格式只能靠后续判据认」。
    "heif": [],
}

#: ISO-BMFF 容器里属于 HEIF 一族的主品牌（``ftyp`` box 的 brand 字段）。
#:
#: ``mif1`` / ``msf1`` 是 HEIF 的通用品牌，部分相机与工具会写它们；
#: 其余几个是 HEVC 图像的具体品牌。**这里没有 ``avif`` / ``avis``**：
#: AVIF 是另一族（Pillow 12 自己就支持它），把它认成 HEIC 会让一个
#: 被改名成 .heic 的 AVIF 走错校验路径 —— 它会在下一层被 Pillow
#: 报成 ``avif``，而 ``avif`` 不在 :data:`_MAGIC_SIGNATURES` 里，于是
#: 得到一个干净的「不支持的图片格式：AVIF」。
_HEIF_BRANDS = frozenset(
    {
        b"heic",
        b"heix",
        b"hevc",
        b"hevx",
        b"heim",
        b"heis",
        b"hevm",
        b"hevs",
        b"mif1",
        b"msf1",
    }
)

#: 前缀对上之后还要看的第二个判据。
#:
#: 为什么需要它：``_MAGIC_SIGNATURES`` 那套是「文件以什么开头」，而
#: 有两族格式的开头**长得一样**——
#:
#: * ``RIFF`` 也是 wav / avi 的开头，要看第 8-12 字节是不是 ``WEBP``；
#: * HEIC 是 ISO-BMFF 容器：前 4 字节是 box 长度（**每张图都不一样**），
#:   第 4-8 字节才是 ``ftyp``，第 8-12 字节是品牌。所以它连一个固定前缀
#:   都写不出来，只能整个交给这条判据。
_MAGIC_FOLLOWUPS: dict[str, Callable[[bytes], bool]] = {
    "webp": lambda head: head[8:12] == b"WEBP",
    "heif": lambda head: head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS,
}

# 扩展名 -> 内部格式名
_EXT_TO_FORMAT: dict[str, str] = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".webp": "webp",
    ".bmp": "bmp",
    ".gif": "gif",
    ".tif": "tiff",
    ".tiff": "tiff",
    # .heic 与 .heif 是同一种容器的两个扩展名，理由同 .jpg / .jpeg。
    # 两者都收到同一个内部名 ``heif``，与 Pillow 报的 ``HEIF`` 对齐。
    ".heic": "heif",
    ".heif": "heif",
}

# 面向用户的统一提示文案
UNSUPPORTED_FORMAT_MESSAGE = (
    "暂不支持该文件格式，请上传 JPG、PNG、WEBP、BMP、GIF、TIFF 或 HEIC 图片。"
)
BROKEN_IMAGE_MESSAGE = "无法读取该图片，请检查文件是否损坏。"
UNSUPPORTED_SVG_MESSAGE = "暂不支持该文件格式，请上传 SVG 矢量图。"
#: 扩展名对、文件头也对，只是这台服务器**没装 HEIC 组件**时说的话。
#:
#: 与上面那句「暂不支持该文件格式」**必须分开**：那句话的意思是
#: 「HEIC 不在支持范围内」，而事实是「支持，但这台机器缺组件」。
#: 拿前一句去回答后一种情况是在说假话 —— 用户会以为换个网站就能转。
HEIC_COMPONENT_MISSING_MESSAGE = (
    "服务器未安装 HEIC 组件，暂时无法处理 HEIC / HEIF 图片；"
    "JPG、PNG、WEBP 等格式不受影响。"
)


def normalize_format(raw: str | None) -> str | None:
    """把 Pillow 返回的格式名统一成小写内部名。"""
    if not raw:
        return None
    value = raw.strip().lower()
    if value == "jpg":
        return "jpeg"
    return value


def check_extension(filename: str) -> str:
    """校验扩展名，返回内部格式名。"""
    ext = Path(filename).suffix.lower()
    if ext not in settings.ALLOWED_IMAGE_EXTENSIONS:
        raise UnsupportedTypeError(UNSUPPORTED_FORMAT_MESSAGE)
    return _EXT_TO_FORMAT[ext]


def _matches(fmt: str, head: bytes) -> bool:
    """这一段文件头是不是 ``fmt``。

    两层：先看有没有命中的前缀签名（HEIC 一个都没有，跳过这一层），
    再看有没有第二个判据（``_MAGIC_FOLLOWUPS``）。
    """
    signatures = _MAGIC_SIGNATURES[fmt]
    if signatures and not any(head.startswith(sig) for sig in signatures):
        return False
    followup = _MAGIC_FOLLOWUPS.get(fmt)
    return followup is None or followup(head)


def sniff_format(path: Path) -> str:
    """读取文件头判断真实格式，返回内部格式名。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError as exc:  # pragma: no cover - 磁盘异常
        raise ValidationError("无法读取上传的文件") from exc

    if len(head) < 12:
        raise CorruptedFileError(BROKEN_IMAGE_MESSAGE)

    for fmt in _MAGIC_SIGNATURES:
        if _matches(fmt, head):
            return fmt

    raise UnsupportedTypeError(UNSUPPORTED_FORMAT_MESSAGE)


def verify_image(path: Path) -> tuple[int, int, str]:
    """用 Pillow 打开并校验图片，返回 (宽, 高, 格式)。

    只做校验，不保留 Image 对象；调用方需要重新打开。
    """
    # 延迟导入，避免模块加载顺序影响 Pillow 的全局配置
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS

    try:
        with Image.open(path) as img:
            img.verify()  # verify() 之后对象不可再用
    except UnidentifiedImageError as exc:
        raise UnsupportedTypeError(UNSUPPORTED_FORMAT_MESSAGE) from exc
    except Image.DecompressionBombError as exc:
        raise ValidationError("图片尺寸过大，超过服务器处理上限") from exc
    except Exception as exc:  # Pillow 对损坏文件抛出的异常类型很杂
        raise CorruptedFileError(BROKEN_IMAGE_MESSAGE) from exc

    # verify 通过后再打开一次读取真实尺寸
    try:
        with Image.open(path) as img:
            width, height = img.size
            fmt = normalize_format(img.format) or ""
    except Exception as exc:  # pragma: no cover
        raise CorruptedFileError(BROKEN_IMAGE_MESSAGE) from exc

    if width <= 0 or height <= 0:
        raise ValidationError("图片尺寸无效")

    if width * height > settings.MAX_IMAGE_PIXELS:
        raise ValidationError("图片像素总量过大，超过服务器处理上限")

    return width, height, fmt


def validate_image_upload(path: Path, original_filename: str) -> tuple[int, int, str]:
    """完整校验一张上传的图片，返回 (宽, 高, 格式)。"""
    declared = check_extension(original_filename)
    actual = sniff_format(path)

    if declared != actual:
        raise UnsupportedTypeError(
            f"文件扩展名（{declared.upper()}）与真实内容（{actual.upper()}）不一致，已拒绝处理"
        )

    # 扩展名与文件头都说这是 HEIC，但**这台服务器没装 HEIC 组件**时，
    # 必须给一句真话。往下走会撞在 Pillow 的 ``UnidentifiedImageError`` 上，
    # 那条路统一报「暂不支持该文件格式」—— 那是在说假话：HEIC 在支持范围里，
    # 只是这台机器缺组件。用户会以为是文件的问题，去换个网站重试。
    #
    # 延迟导入 ``compressors.heif``：``utils.validation`` 是很多纯 Pillow 路径
    # 的公共依赖，不该因为多了一个 HEIC 判断就在 import 期把 pillow-heif
    # 拉起来（与下面 ``verify_image`` 延迟导入 Pillow 同一个理由）。
    if actual == "heif":
        from compressors.heif import heif_support

        if not heif_support().decode:
            raise UnsupportedTypeError(HEIC_COMPONENT_MISSING_MESSAGE)

    width, height, pillow_fmt = verify_image(path)
    if pillow_fmt and pillow_fmt != actual:
        # Pillow 判定与文件头不一致，以 Pillow 为准但要求仍在白名单内。
        # 白名单直接复用 `_MAGIC_SIGNATURES` 的键：另抄一份集合的话，
        # 加一种格式时就会漏掉这里，表现是「新格式的图片偶尔被判不支持」。
        if pillow_fmt not in _MAGIC_SIGNATURES:
            raise UnsupportedTypeError(f"不支持的图片格式：{pillow_fmt.upper()}")
        return width, height, pillow_fmt

    return width, height, actual


def validate_svg_upload(path: Path, original_filename: str) -> tuple[int, int]:
    """校验一份上传的 SVG，返回它**自己声明的** ``(宽, 高)``（读不出来是 0）。

    与图片那三个函数是并列的一条路，不是它的分支 —— SVG 没有魔数、
    Pillow 也解不开，硬塞进 ``validate_image_upload`` 只会让那个函数
    变成两个不相干校验的拼盘。

    三层，与 Office / markup 那两条路同构：

    1. 扩展名在 ``settings.ALLOWED_SVG_EXTENSIONS`` 里；
    2. 文件非空、没超字节上限、**根元素真的是 ``svg``**；
    3. 净化能跑完（拿掉脚本/外链），并数出元素个数没超上限。

    第 2、3 层直接调用 ``compressors.svg`` 里那三个函数，**不另写一份
    判据** —— 「什么算 SVG」「什么算危险」只有一处定义。代价是净化在
    提交时跑一遍、真正转换时还会再跑一遍（渲染器绝不能拿到未净化的字节，
    那一遍省不掉）。这一遍买到的是：**坏文件在占用 worker 之前就被拒**，
    而且拿到的是 415/400 这样准确的响应，不是队列里的一条 failed 任务。

    返回的宽高来自 ``viewBox`` 或根元素的 ``width``/``height``，
    **只解析 XML、不栅格化**：提交路径上不该为了一张卡片的尺寸
    去渲染整张图。声明里读不出来就是 ``(0, 0)``，界面按「未知」显示 ——
    这里编一个数字出来，比留空更糟。
    """
    extension = Path(original_filename).suffix.lower()
    if extension not in settings.ALLOWED_SVG_EXTENSIONS:
        raise UnsupportedTypeError(UNSUPPORTED_SVG_MESSAGE)

    size = path.stat().st_size if path.exists() else 0
    if size <= 0:
        raise ValidationError("上传的文件为空")

    # 延迟导入：``compressors.svg`` 会拉起 PyMuPDF，而 ``utils.validation``
    # 是很多纯 Pillow 路径的公共依赖（图片压缩页就是），不该因为多了一个
    # SVG 校验函数就让它们全都在 import 期加载 MuPDF。与上面 ``verify_image``
    # 里延迟导入 Pillow 是同一个理由。
    from compressors import svg as svg_codec

    data = svg_codec.read_svg(path)
    if not svg_codec.looks_like_svg(data[:64]):
        # 走到这里说明扩展名是 .svg 但内容根本不是 XML 文本（例如改名过来的
        # 一张 PNG）。先报「不是 SVG」，比让 XML 解析器抛出「文件已损坏」准确。
        raise UnsupportedTypeError("这不是一个 SVG 文件，请上传真正的 SVG。")

    clean, _notes = svg_codec.sanitize_svg(data)
    declared = svg_codec.declared_size(clean)
    if declared is None:
        return 0, 0
    return max(0, int(declared[0])), max(0, int(declared[1]))

