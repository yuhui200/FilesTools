"""图片解码与基础几何变换。

从 image_compressor 中抽取，供压缩 / 格式转换 / 尺寸调整共用。
这里只做「把文件变成一张可安全处理的图片」，不涉及任何编码决策。
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

from config import settings
from utils.errors import CorruptedFileError, ValidationError

# 允许 Pillow 解码的最大像素数，超过会抛 DecompressionBombError
Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS


def load_image(path: Path) -> tuple[Image.Image, str]:
    """打开图片、修正 EXIF 方向、限制最大边长，返回 (图片, 原始格式)。

    注意这里的两个既有取舍（第一阶段的决定，至今未变）：

    * **方向标记会被应用并摘掉** —— ``exif_transpose`` 把像素摆正后删掉
      Orientation 标记，因此后续任何旋转都不会和它叠加；
    * **其余 EXIF 不会自动写回结果** —— 重新编码时只有显式传入 ``exif=``
      才会带上（见 ``compressors.encoder``）。压缩 / 尺寸调整两个页面正是
      靠这一点丢掉拍摄信息（含 GPS），既减小体积也不把用户的位置带进结果。
      统一转换中心把这件事交给了用户（§九 的「元数据：保留 / 清除」）。

    格式必须在任何变换之前读取 —— Pillow 中图片一旦被修改，``.format`` 就变成 None。
    """
    try:
        img = Image.open(path)
        img.load()
        raw_format = (img.format or "").lower()
    except Image.DecompressionBombError as exc:
        raise ValidationError("图片尺寸过大，超过服务器处理上限") from exc
    except Exception as exc:
        raise CorruptedFileError("无法读取该图片，请检查文件是否损坏。") from exc

    fmt = "jpeg" if raw_format == "jpg" else raw_format

    # **在 exif_transpose 之前**把拍摄信息取出来（第十阶段 C §四）。
    #
    # 这一步不是可有可无的备份：``exif_transpose`` 会返回一个**新对象**
    # （实测：即使源文件没有方向标记，返回的也不是同一个对象），新对象上
    # ``getexif()`` 是空的。JPEG / PNG / WebP 没事 —— 它们的 EXIF 在
    # ``info["exif"]`` 里，info 会跟着复制过去；**TIFF 不是**：它的拍摄
    # 信息只在 ``getexif()`` 上，转置之后就彻底没了。
    #
    # 于是在这之前，TIFF 源文件选「保留元数据」等于什么都没保留，而且
    # 一声不吭。取出字节挂到 ``info`` 上之后，TIFF 就与别的格式走同一条
    # 通道，后面不再有第二个特殊分支。
    #
    # 延迟 import：``compressors.encoder`` 在模块顶层 import 本模块的
    # ``resize_by_scale``，顶层反向 import 会成环。与 ``decode_source``
    # 里那处延迟 import 是同一个理由。
    if not img.info.get("exif"):
        from compressors.encoder import read_exif

        captured = read_exif(img)
        if captured:
            img.info["exif"] = captured

    try:
        img = ImageOps.exif_transpose(img) or img
    except Exception:
        # EXIF 异常不影响主流程，忽略方向修正即可
        pass

    if max(img.width, img.height) > settings.MAX_IMAGE_EDGE:
        img = resize_to_max_edge(img, settings.MAX_IMAGE_EDGE)

    return img, fmt


def decode_source(path: Path) -> tuple[Image.Image, str, list[str]]:
    """解码**任意**受支持的源文件，返回 ``(图片, 格式, 说明)``。

    与 :func:`load_image` 的分工：``load_image`` 是**位图**解码器（Pillow），
    这里是**分派器** —— 按文件内容选一个解码器，并把「解码时动过什么」
    一并交出来。

    SVG 走 ``compressors.svg``：它渲染出来的是一张普通的 ``PIL.Image``，
    所以拿到之后裁剪 / 缩放 / 旋转 / 翻转 / 编码那一整套一行都不用改。
    这就是 §二「不要创建第二套图片转换系统」在解码这一层的落点 ——
    SVG 不是一条平行的流水线，只是流水线开头多了一个解码器。

    ``notes`` 是**净化时的如实说明**（脚本被拿掉、外链被忽略、尺寸被缩小）。
    位图那条路没有这类话可说，返回空列表。它必须一路传到结果里：
    用户拿到一张被削过的图却没有一句解释，比直接报错更让人困惑。

    这里用 ``looks_like_svg`` 而不是扩展名来分流。扩展名在这一层已经
    不可信（``detect_source`` 早就用内容复核过一遍），而这条流水线
    还会被压缩 / 尺寸调整两个专用页面调用 —— 那两处只做扩展名白名单，
    多一道内容判断正是它们需要的。
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(64)
    except OSError as exc:
        raise ValidationError("读取上传文件失败") from exc

    # 延迟导入：``compressors.svg`` 会拉起 PyMuPDF（还在 import 期注册
    # ElementTree 的命名空间）。纯位图的那几条路 —— 压缩页、尺寸调整页 ——
    # 不该因为多了一个 SVG 解码器就在 import 期背上 MuPDF。
    from compressors import svg as svg_codec

    if not svg_codec.looks_like_svg(head):
        img, fmt = load_image(path)
        return img, fmt, []

    data = svg_codec.read_svg(path)
    clean, notes = svg_codec.sanitize_svg(data)
    rendered = svg_codec.render_svg(clean)
    return rendered.image, "svg", notes + rendered.notes


def source_frame_info(source: Path | bytes) -> tuple[str, int]:
    """源文件的 ``(格式, 帧数)`` —— GIF 的帧数、TIFF 的页数。

    第九阶段对这两种容器**只处理第一帧**（``load_image`` 里
    ``img.load()`` 拿到的就是第 0 帧）。这两个值都不参与任何处理决策，
    只用来决定要不要如实告诉用户「你给的文件里还有别的内容，这次没转」——
    所以读不出来时按 ``("", 1)`` 算是安全的：最多是少说一句话，
    不会凭空给单帧文件加一句「只转了第一帧」的假话。

    ``exif_transpose`` 会返回一个新对象并丢掉帧信息，所以这里只能重新打开
    一次文件；``Image.open`` 是惰性的，只读文件头，不解码像素。
    接受 ``Path`` 也接受 ``bytes``：图片 → PDF 那条路上拿到的是字节流
    （``pdf.image_to_pdf.ImageSource.data``），文件早就落在临时目录里了，
    没必要为了问一句帧数再绕回文件系统。
    """
    try:
        with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as img:
            fmt = (img.format or "").lower()
            frames = getattr(img, "n_frames", 1)
    except Exception:
        # 走到这里说明文件有问题，但那由 load_image 去报错并给出面向用户的文案，
        # 这里只负责「问不出帧数」这一件小事。
        return "", 1
    return fmt, frames if isinstance(frames, int) and frames > 1 else 1


def source_frame_count(path: Path) -> int:
    """源文件有多少帧，见 :func:`source_frame_info`。"""
    return source_frame_info(path)[1]


#: EXIF 里方向标记的 tag 号。
EXIF_ORIENTATION_TAG = 0x0112
#: 这几种方向下宽高是反的（需要旋转 90 度才摆正）。
_SWAPPING_ORIENTATIONS = frozenset({5, 6, 7, 8})


def source_dimensions(path: Path) -> tuple[int, int] | None:
    """源文件**声明的**宽高，不解码像素。位图读文件头，SVG 读根元素。

    存在的唯一理由：``decode_source`` 会把超过 ``MAX_IMAGE_EDGE`` 的图缩下来，
    而裁剪坐标是用户照着**自己那份原图**量的。一张 20000×10000 的图被缩到
    12000×6000 之后，用户填的 x=15000 会被判越界 —— 除非我们知道自己
    缩过图，并如实告诉他坐标是按缩小后的尺寸算的。

    位图不能改用 ``exif_transpose`` 来算：它会真的解码像素，等于为了问一句
    尺寸先把整张大图读一遍。方向 5–8 会交换宽高，所以这里只读方向标记自己换。

    SVG 同理不能靠渲染来问尺寸 —— 那正是我们要避免的开销。它走
    ``svg.declared_size``：只解析 XML，不栅格化。渲染器缩小过的情形
    由 ``svg._render_zoom`` 自己记一条 note，两条路各自说各自的话。

    读不出来返回 None —— 「不知道」比编一个数字安全。SVG 那一段额外包了
    ``except Exception``：走到这里时图已经解码出来了，问尺寸只是锦上添花，
    不该因为一次 XML 解析失败把整个转换掀翻。
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(64)
    except OSError:
        return None

    if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        from compressors import svg as svg_codec

        try:
            data = svg_codec.read_svg(path)
            clean, _notes = svg_codec.sanitize_svg(data)
            declared = svg_codec.declared_size(clean)
        except Exception:
            return None
        if declared is None:
            return None
        return int(declared[0]), int(declared[1])

    try:
        with Image.open(path) as img:
            width, height = img.size
            orientation = img.getexif().get(EXIF_ORIENTATION_TAG, 1)
    except Exception:
        return None
    if orientation in _SWAPPING_ORIENTATIONS:
        width, height = height, width
    return width, height


def resize_to_max_edge(img: Image.Image, max_edge: int) -> Image.Image:
    """等比缩小，使最长边不超过 max_edge。"""
    if max(img.width, img.height) <= max_edge:
        return img
    scale = max_edge / max(img.width, img.height)
    return resize_by_scale(img, scale)


def resize_by_scale(img: Image.Image, scale: float) -> Image.Image:
    """按比例缩放，最小 1 像素。"""
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    if size == img.size:
        return img
    return img.resize(size, Image.Resampling.LANCZOS)


def resize_exact(img: Image.Image, width: int, height: int) -> Image.Image:
    """缩放到精确的宽高。"""
    width = max(1, int(width))
    height = max(1, int(height))
    if (width, height) == img.size:
        return img
    return img.resize((width, height), Image.Resampling.LANCZOS)


#: 顺时针角度 -> ``Image.transpose`` 的方法。
#: 用 transpose 而不是 ``rotate``：后者默认会重采样并改变画布尺寸，
#: 90 的整数倍本来是无损的像素重排，重采样只会白白糊掉细节。
_TRANSPOSE_BY_ANGLE = {
    90: Image.Transpose.ROTATE_270,   # PIL 的 ROTATE_* 是逆时针
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def rotate_image(img: Image.Image, clockwise: int) -> Image.Image:
    """按顺时针角度旋转（只接受 0 / 90 / 180 / 270），宽高随之互换。"""
    angle = clockwise % 360
    if angle == 0:
        return img
    method = _TRANSPOSE_BY_ANGLE.get(angle)
    if method is None:
        raise ValidationError("旋转角度只能是 0、90、180 或 270")
    return img.transpose(method)


def rotate_arbitrary(
    img: Image.Image, clockwise: float, *, expand: bool = True
) -> Image.Image:
    """按**任意**角度顺时针旋转（第十阶段 A §二十三）。

    与 :func:`rotate_image` 分工明确，不是重复实现：

    * 90 的整数倍走 ``transpose`` —— **无损**的像素重排，不重采样；
    * 其余角度只能重采样（``BICUBIC``），画面细节必然有损失。

    调用方（``pipeline``）负责先判断走哪一条：把 90/180/270 也丢进这里
    重采样一遍，等于让用户在只想转个方向时白白损失画质。

    ``expand``：旋转后的画布要不要放大到刚好装下整张图。

    * ``True``（默认，也是 §二十三 建议的语义）—— 四角不会切掉，
      代价是画布变大、出现需要填充的角；
    * ``False`` —— 画布尺寸不变，超出部分被切掉。

    填充色用**全透明**。JPEG 那条路最后会经 ``flatten_alpha`` 合成到白底，
    于是不透明格式拿到白角、PNG/WEBP 拿到真透明 —— 一处选择，
    两种格式各自得到正确的默认，不用在这里按目标格式分支。
    """
    angle = float(clockwise) % 360
    if angle == 0:
        return img
    if angle % 90 == 0:
        return rotate_image(img, int(angle))

    source = img if img.mode in ("RGBA", "LA") else img.convert("RGBA")
    rotated = source.rotate(
        -angle,  # PIL 的 rotate 是逆时针
        resample=Image.Resampling.BICUBIC,
        expand=expand,
        fillcolor=(0, 0, 0, 0),
    )
    return rotated


#: 翻转方向 -> 说明。``both`` 等价于水平 + 垂直（= 旋转 180°），
#: 但**保留成一个独立取值**：用户点的是「水平垂直都翻」，
#: 界面就该有这一档，让他自己去组合两次操作是多余的心智负担。
FLIP_MODES: tuple[str, ...] = ("none", "horizontal", "vertical", "both")

FLIP_LABELS: dict[str, str] = {
    "none": "不翻转",
    "horizontal": "水平翻转（左右镜像）",
    "vertical": "垂直翻转（上下镜像）",
    "both": "同时水平与垂直翻转",
}


def flip_image(img: Image.Image, mode: str) -> Image.Image:
    """按方向翻转（第十阶段 A §二十四）。

    镜像不改变像素值，只改排列 —— 没有重采样，没有画质损失。
    所以它与旋转不同：不产生任何需要向用户说明的副作用。
    """
    if mode in ("", "none"):
        return img
    if mode == "horizontal":
        return ImageOps.mirror(img)
    if mode == "vertical":
        return ImageOps.flip(img)
    if mode == "both":
        return ImageOps.mirror(ImageOps.flip(img))
    raise ValidationError("翻转方式只能是水平、垂直或两者同时")


def crop_image(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """按 ``(x, y, 宽, 高)`` 裁剪。

    只做切割，不重采样 —— 裁剪本身**无损**。越界由
    ``compressors.cropper.compute_crop_box`` 提前拒掉，所以这里不再校验：
    同一个规则判两遍，早晚有一处被改漏。
    """
    x, y, width, height = box
    return img.crop((x, y, x + width, y + height))
