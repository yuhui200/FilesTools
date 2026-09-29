"""图片编码与「按目标大小优化」的核心算法。

本模块从 image_compressor 中抽取，供三个功能共用：
图片压缩、图片格式转换、图片尺寸调整。

对外提供两件事：

1. ``encode_image`` —— 按指定格式和质量编码一次；
2. ``encode_under_target`` —— 先降质量、必要时再缩尺寸，把体积压到目标以内。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, PngImagePlugin

from compressors.loader import resize_by_scale
from compressors.metadata import TIFF_STRUCTURAL_TAGS
from config import settings
from utils.errors import ProcessingError, ValidationError

# 输出格式 -> 文件扩展名
FORMAT_EXTENSIONS: dict[str, str] = {
    "jpeg": ".jpg",
    "png": ".png",
    "webp": ".webp",
    "bmp": ".bmp",
    "gif": ".gif",
    "tiff": ".tiff",
    "ico": ".ico",
    "heif": ".heic",
}

# 允许作为输出目标的格式。
#
# 前三个是第一 / 第二阶段就有的；后四个由第九阶段（§七）补上；
# ``heif`` 由第十阶段 A（§五–§八）补上。
# **顺序是历史顺序，不是偏好**：``routers/params.parse_format`` 与
# ``converters.image_converter.parse_target_format`` 都会拿它拼一句
# 「请选择 …」的提示，改顺序会改动用户看到的文案。
#
# ``heif`` 是这里面**唯一一个可能不可用**的格式：它要 ``pillow-heif``
# 提供的 HEVC 编码器。这里的表说的是「编码器认识它」，能不能真写出来
# 由 ``compressors.heif.heif_support().encode`` 回答，两者别混
# （与 ``conversion.registry`` 里 ``SOURCE_REQUIREMENTS`` 的分工一致）。
OUTPUT_FORMATS: tuple[str, ...] = (
    "jpeg",
    "png",
    "webp",
    "bmp",
    "gif",
    "tiff",
    "ico",
    "heif",
)

#: 能携带 EXIF 的格式。Pillow 的 ``save(exif=...)`` 只在这几种里有落点；
#: GIF / BMP / ICO **不会报错，但会静默丢掉** —— 后者更糟，调用方会以为
#: 写进去了。所以这里的意义是「值得传」，不是「传了会不会炸」。
#: 与 ``conversion.options`` 里那份线上词汇表（``jpg`` / ``webp`` / ``png`` /
#: ``tiff`` / ``heic``）是同一件事的两种写法，由测试对账。
#:
#: HEIC 经**实测**进来的：写进去再读回来，Make / Model 都在。
EXIF_FORMATS: tuple[str, ...] = ("jpeg", "png", "webp", "tiff", "heif")

#: 能写入密度（DPI）的格式。
#:
#: ``bmp`` 有意不在其中：BMP 文件头**必须**有一个密度字段，Pillow 不给
#: ``dpi=`` 时会写它自己的默认值（96）。也就是说 BMP 目标的「保持原样」
#: 并不能真的保住源文件的密度 —— 与其发一个半真半假的旋钮，
#: 不如不提供，并在报告里如实写明这一点。
#:
#: ``heif`` 也不在其中，理由与 BMP 不同、**比它更彻底**：Pillow 收下
#: ``dpi=`` 不报任何错，而回读 ``img.info['dpi']`` 是 ``None`` ——
#: 一点都没写进去。所以对 HEIC 来说 DPI 是一个纯装饰的旋钮。
DENSITY_FORMATS: tuple[str, ...] = ("jpeg", "png", "tiff")

#: 能携带 XMP 的格式（第十阶段 C §四）。
#:
#: **这张表是探针实测出来的，不是照抄 EXIF 那张表** —— 两边恰好重合，
#: 但重合是巧合，理由完全不同，将来任一边变了都不该带着另一边一起动：
#: ``xmp=`` 只是 Pillow 的一个 ``save()`` 关键字参数，插件认不认各写各的。
#:
#: 实测（Pillow 12.3.0，写进去再读回来验的，不是看抛不抛错）：
#:
#: * ``jpeg`` / ``webp`` / ``heif`` —— ``save(xmp=...)`` 直接可用；
#: * ``png`` —— ``xmp=`` **不报错但什么都不写**，必须走
#:   ``pnginfo=PngInfo`` 里的 ``XML:com.adobe.xmp`` 文本块；
#: * ``tiff`` —— ``xmp=`` 同样静默失效，只能把 XMP 塞进 EXIF 的
#:   700 号标签（``tiffinfo={700:...}`` 也行，但它会把同时传的
#:   ``exif=`` 挤掉，实测拍下来的 Model 会丢 —— 所以这里不用它）；
#: * ``bmp`` / ``gif`` / ``ico`` —— 容器里根本没有放 XMP 的地方，
#:   实测三种写法都静默丢弃。它们不进这张表，由调用方**如实告诉用户**。
#:
#: 「静默丢弃」是这张表存在的全部理由：Pillow 对不认识的 ``xmp=``
#: 不抛错，所以「传了」与「写进去了」是两件事，只有读回来才算数。
XMP_FORMATS: tuple[str, ...] = ("jpeg", "png", "webp", "tiff", "heif")

#: 读回时 XMP 可能落在哪个 ``info`` 键上。
#:
#: PNG 的插件把它放在 iTXt 的键名 ``XML:com.adobe.xmp`` 下（回读是
#: ``str``），其余格式统一放 ``xmp``（回读是 ``bytes``）。两个键都要看，
#: 否则「PNG 源 → 任何目标」这条路上 XMP 会被当成不存在。
XMP_INFO_KEYS: tuple[str, ...] = ("xmp", "XML:com.adobe.xmp")

#: PNG 装 XMP 用的文本块键名。
_PNG_XMP_KEY = "XML:com.adobe.xmp"

#: EXIF 里放 XMP 的标签号（TIFF/EXIF 规范里的 ``XMP``）。
_EXIF_XMP_TAG = 700

#: 这些格式的 Pillow 插件会**自己**从图片对象上抓元数据 —— 对它们来说
#: 「不传 ``exif=``」**不等于**「不写 EXIF」。
#:
#: 实测（pillow-heif 1.8.0）：``as_plugin._pil_encode_image`` 在
#: ``info.update(**kwargs)`` 之前无条件执行
#: ::
#:     info["exif"] = _exif_from_pillow(img)
#:     info["xmp"]  = _xmp_from_pillow(img)
#:
#: 而这两个函数只要在图片对象上找得到就原样返回（``_exif_from_pillow``
#: 还会退回 ``img.getexif()``）。于是 ``metadata="remove"`` 在 HEIC 上
#: **静默失效**：用户选了「清除元数据」，相机型号与 XMP（可能含 GPS）
#: 照样写进结果文件。这比报错更糟 —— 调用方以为清干净了。
#:
#: 所以在这张表里的格式上，要清就得**显式**清（见 :func:`build_extra`）。
#: 表外格式实测都是「不传就不写」。
_HARVESTS_METADATA: frozenset[str] = frozenset({"heif"})

#: 一个合法的「空 EXIF」：TIFF 头 + 0 个条目。``Image.Exif().tobytes()``
#: 正好是这个 —— **不能**传 ``b""``，pillow-heif 会抛
#: ``Could not find location of TIFF header in Exif metadata``。
#: 回读时 ``bool(getexif())`` 是 ``False``，与「没有 EXIF」一致。
_NO_EXIF: bytes = Image.Exif().tobytes()

#: ICO 每帧的最小边长。Pillow 的图标尺寸表从 16 起，短边不足 16 像素的
#: 源图一帧都生成不出来 —— 而它不会报错，只会写出一个零帧空壳。
ICO_MIN_EDGE = 16

#: ICONDIR 头长度（6 字节：保留 2 + 类型 2 + 帧数 2）。
_ICO_HEADER_BYTES = 6

# PNG 达到这个质量值按无损处理，低于则做调色板量化
LOSSLESS_PNG_QUALITY = 95

# 二分搜索的最大编码次数（每次编码都要完整压缩一遍，需要控制开销）
MAX_SEARCH_STEPS = 7

# 缩小到这个边长以下就停止，避免生成无意义的缩略图
MIN_EDGE = 32


def supports_exif(fmt: str) -> bool:
    """这种输出格式能不能带上 EXIF。"""
    return fmt in EXIF_FORMATS


def supports_density(fmt: str) -> bool:
    """这种输出格式能不能写入 DPI。"""
    return fmt in DENSITY_FORMATS


def supports_xmp(fmt: str) -> bool:
    """这种输出格式能不能带上 XMP。"""
    return fmt in XMP_FORMATS


def read_xmp(img: Image.Image) -> bytes | None:
    """从解码好的图片上取出 XMP，取不到就返回 ``None``。

    归一化成 ``bytes``：PNG 的插件给的是 ``str``（iTXt 里读出来的文本），
    其余格式给 ``bytes``。写回去时两种通道各自的编码方式不同，但**读**的
    这一侧统一成字节，调用方就不必到处判类型。

    ``latin-1`` 不是随手挑的：PNG 的 XMP 文本块就是按 latin-1 存字节的
    （XMP 规范里那个 UTF-8 的 ``begin`` 标记会被写成一堆高位字符），
    用 ``latin-1`` 编解码是**逐字节往返**的，换成 ``utf-8`` 反而会炸或改字节。
    """
    for key in XMP_INFO_KEYS:
        value = img.info.get(key)
        if isinstance(value, bytes) and value:
            return value
        if isinstance(value, str) and value:
            return value.encode("latin-1", errors="replace")
    return None


def read_exif(img: Image.Image) -> bytes | None:
    """从解码好的图片上取出**可以安全带走的** EXIF 字节，取不到返回 ``None``。

    ## 为什么不能只读 ``img.info["exif"]``

    多数格式（JPEG / PNG / WebP / HEIC）的插件会把整段 EXIF 原样放进
    ``info["exif"]``，读它就行。**TIFF 不是**：它的拍摄信息就在文件自己的
    IFD 里，Pillow 只通过 ``getexif()`` 暴露，``info`` 里根本没有 ``exif``
    这个键（实测）。

    在此之前 ``_resolve_media_info`` 只看 ``info["exif"]``，于是
    **TIFF 源文件选「保留元数据」等于什么都没保留**，而且一声不吭 ——
    拍摄信息、GPS 全丢，界面上那一项写的却是「保留」。这是第十阶段 C §四
    查出来的，比同一条路上的 XMP 更彻底：XMP 是「没实现」，这个是
    「实现了但走不到」。

    ## 重打包时剔掉什么

    ``getexif()`` 交出来的是**整张 0 号 IFD**，里面混着 TIFF 用来描述像素
    怎么排布的结构字段（宽高、条带偏移……）。这些不是拍摄信息，其中
    ``273``（StripOffsets）更是「像素在文件的第几个字节」这种只对源文件
    成立的数字 —— 原样带走写进别的容器的 EXIF，轻则一堆噪声，重则让
    别的查看器去一个不存在的位置找像素。所以按
    :data:`compressors.metadata.TIFF_STRUCTURAL_TAGS` 剔掉（与元数据
    查看器用同一张表，不另抄一份）。

    也剔掉 ``700``（XMP）：XMP 现在有自己的专用通道
    （:func:`build_extra` 的 ``xmp`` 参数），让它再夹带一份只会造成
    「同一段字节在结果文件里出现两次、删一处还剩一处」。
    """
    raw = img.info.get("exif")
    if isinstance(raw, bytes) and raw:
        return raw

    try:
        exif = img.getexif()
    except Exception:  # noqa: BLE001 - 读不出来就是没有，不因此让整次转换失败
        return None
    if not exif:
        return None

    # ``load`` → ``tobytes`` 这一步是**无损**的：实测顶层标签与
    # Exif / GPS 两个子 IFD（拍摄时间、镜头型号、坐标）逐项还在。
    rebuilt = Image.Exif()
    rebuilt.load(exif.tobytes())
    for tag in list(rebuilt):
        if tag in TIFF_STRUCTURAL_TAGS or tag == _EXIF_XMP_TAG:
            del rebuilt[tag]
    if not rebuilt:
        return None
    return rebuilt.tobytes()


def normalize_format(raw: str | None) -> str | None:
    """把各种写法的格式名统一成内部名（``OUTPUT_FORMATS`` 里的那八个）。

    做三件映射：``jpg`` / ``jpe`` → ``jpeg``、``tif`` → ``tiff``、
    ``heic`` → ``heif``。其余一律按 ``OUTPUT_FORMATS`` 判定 ——
    加一种格式时不需要在这里再补一行 ``if value in ("xxx",)``，也就不会漏。

    ``heic`` → ``heif`` 这一条**非有不可**，它不是锦上添花：
    注册表里那个目标类型叫 ``heic``（用户手里的文件就叫 .heic），
    而 Pillow 给这个容器报的名字是 ``HEIF``。两边对不上的表现很安静 ——
    ``normalize_format`` 返回 ``None``，调用方把一次**合法**的转换
    判成「暂不支持这个图片目标格式」，用户看到一句没头没脑的拒绝。
    """
    if not raw:
        return None
    value = raw.strip().lower().lstrip(".")
    if value in ("jpg", "jpe", "jpeg"):
        return "jpeg"
    if value in ("tif", "tiff"):
        return "tiff"
    if value in ("heic", "heif"):
        return "heif"
    if value in OUTPUT_FORMATS:
        return value
    return None


def extension_for(fmt: str) -> str:
    """取格式对应的扩展名，未知格式兜底为 .bin。"""
    return FORMAT_EXTENSIONS.get(fmt, ".bin")


# ----------------------------------------------------------------------
# 单次编码
# ----------------------------------------------------------------------

def _subsampling_for(quality: int) -> int:
    """按质量选择色度采样：质量越高保留的色度细节越多。"""
    if quality >= 90:
        return 0  # 4:4:4
    if quality >= 70:
        return 1  # 4:2:2
    return 2      # 4:2:0


def flatten_alpha(img: Image.Image) -> Image.Image:
    """把透明通道合成到白色背景上，返回 RGB 图片。

    JPEG 不支持透明。如果直接丢弃 alpha 通道，半透明像素会变成黑色，
    因此这里先在白底上做一次 alpha 合成，视觉结果符合直觉。
    """
    if img.mode == "RGB":
        return img
    if img.mode == "P" and "transparency" not in img.info:
        return img.convert("RGB")
    rgba = img.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _encode_jpeg(
    img: Image.Image, quality: int, extra: dict | None = None
) -> bytes:
    """JPEG 编码。

    ``optimize`` 与 ``progressive`` 都要**第二次扫过系数数组**：前者重算
    最优 Huffman 表，后者把一次扫描拆成多遍。这两个选项让文件更小，
    正常情况下没有理由不要。

    ## 唯一的例外：4:4:4 色度采样

    本机这一版 Pillow / libjpeg 在 **4:4:4**（``subsampling=0``，也就是
    ``_subsampling_for`` 给「质量 ≥ 90」选的那一档）下，只要带
    ``optimize`` 或 ``progressive`` 中的任意一个，遇到**体积压不下去的
    大图**就会在第二次扫描时报
    ``OSError: broken data stream when writing image file``。
    实测 100×100 的图无事，400×300 起的纯噪声图必挂；平滑的图（测试里
    那些渐变加椭圆的样张）也一直没暴露过 —— 所以这是第十阶段 A 把质量
    档位摆到「最高 / 100」之后才大量冒出来的**第一阶段遗留缺陷**。
    修之前，用户选高质量压缩一张真实照片，拿到的是「服务器处理失败」。

    三种修法里选了第三种：

    1. 干脆不用 4:4:4 —— 那是拿**画质**换稳定，用户要的正是高画质；
    2. 把 ``optimize`` / ``progressive`` 永久关掉 —— 所有图都白胖一圈；
    3. **先按最优参数编，挂了再退回基线**（当前做法）。

    退回来的只是容器层的打包方式（不做 Huffman 优化、基线而非渐进），
    **像素与色度采样仍然是用户要的那一份**，所以这里不做任何
    「已降级」的说明 —— 说明一件用户看不出、也不影响结果的事只是噪音。
    真正需要说明的情形（目标大小没达到）由 ``encode_under_target`` 负责。

    重试只对 ``subsampling == 0`` 生效：别的组合本来就正常，一旦报错
    就是真错（写 BytesIO 不涉及磁盘，没有「盘满了」这类可恢复原因），
    必须原样抛出去，不能吞掉。
    """
    params: dict = {
        "quality": quality,
        "optimize": True,
        "progressive": True,
        "subsampling": _subsampling_for(quality),
    }
    params.update(extra or {})

    out = flatten_alpha(img)
    try:
        return _save_jpeg(out, params)
    except OSError:
        if params["subsampling"] != 0:
            raise
        return _save_jpeg(out, {**params, "optimize": False, "progressive": False})


def _save_jpeg(img: Image.Image, params: dict) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", **params)
    return buf.getvalue()


def _encode_webp(
    img: Image.Image, quality: int, extra: dict | None = None
) -> bytes:
    # WebP 支持透明，但不支持 CMYK / I;16 等模式，需要先归一化
    out = img if img.mode in ("RGB", "RGBA", "L") else img.convert("RGBA")
    params: dict = {"quality": quality, "method": 4}
    params.update(extra or {})
    buf = io.BytesIO()
    out.save(buf, format="WEBP", **params)
    return buf.getvalue()


def _encode_png(
    img: Image.Image, quality: int, extra: dict | None = None
) -> bytes:
    """PNG 是无损格式，用「调色板颜色数」来近似 quality 的语义。

    quality >= 95 时保持原样无损压缩；否则量化到更少的颜色以获得更小体积。
    带透明通道的图片不做量化（调色板会丢失半透明信息），只做无损压缩。
    """
    buf = io.BytesIO()
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )

    if quality >= LOSSLESS_PNG_QUALITY or has_alpha:
        out = img
    else:
        colors = max(8, min(256, int(round(quality / 100 * 256))))
        rgb = img.convert("RGB") if img.mode != "RGB" else img
        out = rgb.quantize(colors=colors, method=Image.MEDIANCUT, dither=Image.FLOYDSTEINBERG)

    params: dict = {"optimize": True, "compress_level": 9}
    params.update(extra or {})
    out.save(buf, format="PNG", **params)
    return buf.getvalue()


def _encode_bmp(
    img: Image.Image, quality: int, extra: dict | None = None
) -> bytes:
    """BMP：无压缩、无质量参数。

    ``quality`` 在这里**没有意义**（BMP 只有 RLE 可选压缩，Pillow 不暴露），
    所以它被忽略 —— 不是忘了用。界面那边也不会给 BMP 摆质量滑杆
    （``conversion.options._LOSSY_TARGETS`` 只有 jpg / webp），
    两处说的是同一件事。

    色彩模式由 ``prepare_for_format`` 归一化好（``P`` / ``1`` / ``L`` / ``RGB``
    原样保留，带 alpha 的合成到白底）—— 32 位 BMP 的 alpha 通道在多数
    查看器里被忽略，留着它等于让用户看到一张与预期不同的图。
    """
    params: dict = {}
    params.update(extra or {})
    buf = io.BytesIO()
    img.save(buf, format="BMP", **params)
    return buf.getvalue()


def _encode_gif(
    img: Image.Image, quality: int, extra: dict | None = None
) -> bytes:
    """GIF：调色板 + 1 位透明度，无质量参数。

    RGBA 直接交给 Pillow 量化 —— 它自己会把「完全透明」的像素映射到
    一个透明索引，并且**保证不透明像素的颜色不变**。手写调色板
    （``quantize`` + ``paste`` 一个透明索引）反而更差：实测半透明像素
    会整个消失，变成一片透明。

    GIF 只有 1 位透明度，所以半透明像素会被量化为不透明 —— 这是容器
    的限制，不是实现取舍，报告里如实写明。
    """
    params: dict = {}
    params.update(extra or {})
    buf = io.BytesIO()
    img.save(buf, format="GIF", **params)
    return buf.getvalue()


def _encode_tiff(
    img: Image.Image, quality: int, extra: dict | None = None
) -> bytes:
    """TIFF：无损，LZW 压缩。

    选 LZW 而不是不压缩：TIFF 的默认（无压缩）会让一份 50 MB 的输入
    膨胀成几百 MB，而 LZW 是无损的，画质一点不差。``quality`` 同 BMP
    一样无意义 —— TIFF 虽能存储 JPEG 压缩的块，但那是另一种格式的语义，
    不是「质量旋钮」。
    """
    params: dict = {"compression": "tiff_lzw"}
    params.update(extra or {})
    buf = io.BytesIO()
    img.save(buf, format="TIFF", **params)
    return buf.getvalue()


def _encode_ico(
    img: Image.Image, quality: int, extra: dict | None = None
) -> bytes:
    """ICO：图标容器，无质量参数、无 EXIF、无密度。

    不需要在这里手动缩放到方形或 256 像素：Pillow 的 ``IcoImagePlugin``
    会遍历它自己那张尺寸表（16 / 24 / 32 / 48 / 64 / 128 / 256），
    跳过所有比源图大的档位，并对每一档做**等比**缩略
    （``frame.thumbnail(size, LANCZOS)``）。实测 400×200 的源得到最大
    128×64 的一帧，2000×1000 得到 256×128 —— 补白成正方形反而会破坏
    用户图片的长宽比。

    **小图必须提前拦下**：尺寸表从 16 起，短边不足 16 像素的源图
    连一档都够不着，而 Pillow 在那种情况下**不抛异常** —— 它写出一个
    6 字节、零帧的空壳（``00 00 01 00 00 00``）就返回了。实测 17×9 与
    15×15 都会中招。不拦的话用户会拿到一个打不开的 ``.ico``，
    任务状态却是「已完成」。这里选择**明确拒绝**而不是放大到 16×16：
    与「尺寸预设只缩不放」同一条原则，不把图拉糊充数。
    """
    short_edge = min(img.width, img.height)
    if short_edge < ICO_MIN_EDGE:
        raise ValidationError(
            f"图片太小，无法生成 ICO 图标：图标每一档至少需要 "
            f"{ICO_MIN_EDGE}×{ICO_MIN_EDGE} 像素，当前图片的短边只有 "
            f"{short_edge} 像素"
        )

    params: dict = {}
    params.update(extra or {})
    buf = io.BytesIO()
    img.save(buf, format="ICO", **params)
    data = buf.getvalue()

    # 兜底：Pillow 的图标写入器遇到写不出帧的情况是「静默成功」，
    # 所以产物必须自己验一遍再交出去 —— 空壳绝不能冒充成功结果。
    if len(data) < _ICO_HEADER_BYTES or int.from_bytes(data[4:6], "little") < 1:
        raise ProcessingError("图标生成失败，请换一张图片试试")
    return data


def _encode_heif(
    img: Image.Image, quality: int, extra: dict | None = None
) -> bytes:
    """HEIC / HEIF（第十阶段 A §五–§八）。

    **依赖 ``pillow-heif``**。``compressors.heif`` 在包导入期就把它注册进
    Pillow 了，所以这里直接用 ``format="HEIF"`` 即可 —— 没有包时
    ``Image.SAVE`` 里根本没有 ``HEIF``，Pillow 会抛 ``KeyError``，
    在下面被翻译成一句干净的 ``ProcessingError``。

    色彩模式：实测 ``RGBA`` 能原样往返（``L``/``LA``/``P`` 会被提升成
    ``RGBA``/``RGB``），所以这里**不需要**像 JPEG 那样丢掉透明通道。
    只把 Pillow 不认的模式（``CMYK``、``I;16`` 之类）归一化掉。
    """
    from compressors.heif import PILLOW_FORMAT

    out = img if img.mode in ("RGB", "RGBA", "L") else img.convert("RGBA")
    params: dict = {"quality": quality}
    params.update(extra or {})
    buf = io.BytesIO()
    try:
        out.save(buf, format=PILLOW_FORMAT, **params)
    except KeyError as exc:  # Pillow 的 SAVE 表里没有 HEIF
        # 走到这里说明 ``pillow-heif`` 不在（或加载失败），而注册表那边
        # 本该已经把「转成 HEIC」这一格藏掉了。**不能返回 traceback**：
        # 这是一句用户看得懂的、说清了原因的话。
        raise ProcessingError(
            "服务器未安装 HEIC 组件，暂时无法把图片转成 HEIC。"
        ) from exc
    except Exception as exc:
        # **不把 ``exc`` 拼进用户看到的这句话。** 它是第三方库（Pillow 与
        # libheif 的绑定）抛出来的原文，内容不由我们控制：实测现在给的是
        # ``ValueError: Invalid parameter value`` 这类干净文本，但
        # 「今天干净」不是一条能长期依赖的性质，而一个带路径的 ``OSError``
        # 就足以把服务器目录结构摆到用户面前（第十阶段 C §十）。
        #
        # 技术细节留在异常链里（``from exc``）：日志与调试照样拿得到，
        # 用户拿到的是一句他能看懂、也知道下一步做什么的话。
        raise ProcessingError(
            "HEIC 编码失败：这张图片无法编码成 HEIC。请改选其他目标格式。"
        ) from exc
    return buf.getvalue()


#: 格式 -> 编码函数。**每种格式一格**，与 §八「不许为每个组合建一个
#: converter」是同一件事的两面：``jpeg → png`` 与 ``bmp → png`` 走的是
#: 同一个 ``_encode_png``，组合数在 ``conversion.registry`` 里，
#: 不在这里。
_ENCODERS = {
    "jpeg": _encode_jpeg,
    "png": _encode_png,
    "webp": _encode_webp,
    "bmp": _encode_bmp,
    "gif": _encode_gif,
    "tiff": _encode_tiff,
    "ico": _encode_ico,
    "heif": _encode_heif,
}


def build_extra(
    fmt: str,
    *,
    exif: bytes | None,
    dpi: tuple[int, int] | None,
    xmp: bytes | None = None,
) -> dict:
    """把 EXIF / DPI / XMP 收敛成这个格式**真的能接受**的附加保存参数。

    不能接受的一律不传：GIF / BMP / ICO 拿到 ``exif=`` 不会报错，而是
    **静默丢掉** —— 比抛错更糟，调用方会以为写进去了；给不支持密度的
    格式传 ``dpi=`` 同理，``xmp=`` 更是三种格式全都静默丢弃。不支持的
    情况由调用方负责告诉用户（``pipeline`` 的说明），这里不猜。

    **反过来也要管**：``exif=None`` 在多数格式上等于「不写」，但在
    :data:`_HARVESTS_METADATA` 里那几种上插件会自己去图片对象上抓，
    等于「照抄源文件」。两边都是「不说清楚就会撒谎」，所以两种情况
    都在这里说清楚：要么给真的 EXIF，要么显式给一个空的。

    ``xmp`` 同样要显式置 ``None``：插件抓 XMP 的那一步不看 ``exif`` 参数，
    只清 EXIF 的话 XMP 会漏出去（实测 PNG 源的 XMP 会整段进 HEIC）。

    **三种格式各有各的装法**（第十阶段 C §四，逐条实测见 :data:`XMP_FORMATS`）：
    JPEG / WebP / HEIC 收 ``xmp=`` 关键字；PNG 要造一个只带 XMP 文本块的
    ``PngInfo``；TIFF 得把 700 号标签并进 EXIF 字节里 —— 所以 TIFF 那一路
    会**重新打包 EXIF**，原标签实测逐个还在（Model / Make / Software /
    DateTime 都对得上），不是拿 XMP 换 EXIF。
    """
    extra: dict = {}
    keep_xmp = xmp if (xmp and supports_xmp(fmt)) else None

    if keep_xmp:
        if fmt == "tiff":
            # 见下：TIFF 的 XMP 与 EXIF 是同一次写入，合并处理
            pass
        elif fmt == "png":
            pnginfo = PngImagePlugin.PngInfo()
            pnginfo.add_text(_PNG_XMP_KEY, keep_xmp.decode("latin-1"))
            extra["pnginfo"] = pnginfo
        else:
            extra["xmp"] = keep_xmp

    if fmt == "tiff" and keep_xmp:
        merged = Image.Exif()
        if exif:
            merged.load(exif)
        merged[_EXIF_XMP_TAG] = keep_xmp
        extra["exif"] = merged.tobytes()
    elif exif and supports_exif(fmt):
        extra["exif"] = exif
    elif fmt in _HARVESTS_METADATA:
        extra["exif"] = _NO_EXIF
        # 插件会自己去图片对象上抓 XMP。只有当我们**没打算**保留时才清空，
        # 否则这一行会把上面刚放好的 ``xmp`` 覆盖成 ``None``。
        if "xmp" not in extra:
            extra["xmp"] = None
    if dpi and supports_density(fmt):
        extra["dpi"] = dpi
    return extra


def encode_image(
    img: Image.Image,
    fmt: str,
    quality: int,
    *,
    extra: dict | None = None,
) -> bytes:
    """按格式编码图片，quality 取值范围 1-100。

    调用前必须先用 converters.image_converter.prepare_for_format 把
    图片调整成目标格式支持的色彩模式。

    ``extra`` 是直接交给 Pillow ``save()`` 的附加参数（EXIF / DPI），
    由 :func:`build_extra` 生成 —— 这里不再做一次「这个格式支不支持」的判断，
    否则同一件事会有两个地方各判一次。
    """
    encoder = _ENCODERS.get(fmt)
    if encoder is None:
        raise ProcessingError(f"不支持输出格式：{fmt.upper()}")
    return encoder(img, max(1, min(100, int(quality))), extra)


# ----------------------------------------------------------------------
# 按目标大小优化
# ----------------------------------------------------------------------

@dataclass(slots=True)
class FitResult:
    """按目标大小压缩的结果。"""

    data: bytes
    image: Image.Image     # 实际编码所用的图片（可能已缩小）
    quality: int
    scale: float           # 相对入参图片的缩放比例
    target_met: bool


def search_quality(
    img: Image.Image,
    fmt: str,
    target_bytes: int,
    low: int,
    high: int,
    *,
    extra: dict | None = None,
) -> tuple[int, bytes]:
    """在 [low, high] 内二分搜索满足体积限制的最高质量。

    返回 (使用的质量, 编码结果)。若区间内任何质量都超标，返回 low 的结果，
    由调用方决定是否继续缩小尺寸。

    ``extra``（EXIF / DPI）对每一档质量都相同，所以二分本身仍然成立 ——
    它只是让每条候选都多出同样的一段字节。
    """
    cache: dict[int, bytes] = {}

    def data_at(q: int) -> bytes:
        if q not in cache:
            cache[q] = encode_image(img, fmt, q, extra=extra)
        return cache[q]

    # 最高质量就已达标 —— 直接用最好的
    if len(data_at(high)) <= target_bytes:
        return high, cache[high]

    # 最低质量仍然超标 —— 质量上已无空间，交给上层缩尺寸
    if len(data_at(low)) > target_bytes:
        return low, cache[low]

    # 不变量：size(low) <= target < size(high)
    steps = 0
    while low < high and steps < MAX_SEARCH_STEPS:
        mid = (low + high + 1) // 2
        if len(data_at(mid)) <= target_bytes:
            low = mid
        else:
            high = mid - 1
        steps += 1

    return low, data_at(low)


def encode_under_target(
    img: Image.Image,
    fmt: str,
    target_bytes: int,
    low: int,
    high: int,
    *,
    allow_downscale: bool = True,
    extra: dict | None = None,
) -> FitResult:
    """把图片压到目标大小以内。

    先在该质量区间内搜索；如果连最低质量都超标，就等比缩小尺寸后再搜一轮，
    直到达标或触发轮次上限。返回的结果保证「尽力最小」，但不保证一定达标 ——
    调用方需要通过 ``target_met`` 判断并向用户说明。
    """
    if high < low:
        high = low

    best: tuple[bytes, Image.Image, int] | None = None
    source_width = img.width
    scale = 1.0
    rounds = settings.MAX_DOWNSCALE_ROUNDS if allow_downscale else 0

    for _ in range(rounds + 1):
        candidate = img if scale == 1.0 else resize_by_scale(img, scale)
        quality, data = search_quality(
            candidate, fmt, target_bytes, low, high, extra=extra
        )

        if len(data) <= target_bytes:
            return FitResult(
                data=data,
                image=candidate,
                quality=quality,
                scale=round(candidate.width / source_width, 4),
                target_met=True,
            )

        if best is None or len(data) < len(best[0]):
            best = (data, candidate, quality)

        if max(candidate.width, candidate.height) <= MIN_EDGE:
            break
        scale *= settings.DOWNSCALE_FACTOR

    assert best is not None
    data, candidate, quality = best
    return FitResult(
        data=data,
        image=candidate,
        quality=quality,
        scale=round(candidate.width / source_width, 4),
        target_met=False,
    )


# 目标大小无法达成时的统一说明
TARGET_UNREACHABLE_NOTE = (
    "已达到该格式的压缩极限，仍略大于目标大小。可尝试降低目标或改用其他输出格式。"
)
