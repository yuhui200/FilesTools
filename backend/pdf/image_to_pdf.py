"""图片 → PDF。

把若干张图片按给定顺序排版成一份 PDF。页面大小、方向、适应方式、
页边距都在这里决定，是纯计算，不碰文件系统（调用方负责落盘）。

几个刻意的取舍：

- **JPEG 直通**：不需要旋转的 JPEG 原样嵌入（DCTDecode），不重新编码，
  既不损画质也不增加体积；PNG/WEBP 会被 MuPDF 转成无损的 FlateDecode。
- **自动页面尺寸 = 图片尺寸**（1 像素 = 1 点），页面完全没有白边；
  需要真实纸张大小就选 A4 / A5 / Letter / 自定义。
- **自动方向**：固定页面尺寸时按每张图片的长宽比自动选纵向或横向，
  因此一批横竖混排的照片不会出现大片留白。
- **填充页面 = 铺满整页**（cover）：按比例放大到盖住整个页面区域，
  超出的部分落在页面之外，会被自然裁掉 —— 不变形，但会裁边。
- **原始大小 = 1 像素 1 磅、不缩放**（original，第九阶段 §十 新增）：
  与「自动页面尺寸」同一套换算，所以「自动 + 原始大小」得到的页面正好
  贴合图片；放在固定纸张上时可能溢出，溢出的部分同样会被裁掉。
- **页边距档位是整毫米**：0 / 5 / 10 / 20 毫米（第九阶段把 18/36/72 磅
  改成了整毫米，让界面上的数字与真实结果对得上）。
- **多帧 / 多页源文件只取第一帧**（第九阶段决策 C）：动图 GIF、多页 TIFF
  都只嵌入第 0 帧，并在 ``notes`` 里如实说明还有多少内容没进来。
  这段说明与图片转图片那条路共用 ``compressors.loader.source_frame_info``
  与 ``compressors.pipeline.frame_note``，两处措辞不会漂移。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pymupdf

from compressors.loader import source_frame_info
from compressors.pipeline import frame_note
from config import settings
from utils.errors import ValidationError

__all__ = ["ImageSource", "PdfLayout", "PdfBuildResult", "build_pdf_from_images"]

# 毫米 -> PDF 点
_MM_TO_PT = 72.0 / 25.4

# 自定义页面尺寸的合理区间（毫米）：比名片还小或比 A0 还大都没有意义
# 值与 ``config.settings`` 同源（见那里的说明）。
_MIN_CUSTOM_MM = settings.PDF_MIN_CUSTOM_MM
_MAX_CUSTOM_MM = settings.PDF_MAX_CUSTOM_MM

PAGE_SIZE_AUTO = "auto"
FIT_CONTAIN = "contain"
FIT_FILL = "fill"
FIT_ORIGINAL = "original"
ORIENT_AUTO = "auto"
ORIENT_PORTRAIT = "portrait"
ORIENT_LANDSCAPE = "landscape"

PAGE_SIZES = (PAGE_SIZE_AUTO, "a4", "a5", "letter", "custom")
ORIENTATIONS = (ORIENT_AUTO, ORIENT_PORTRAIT, ORIENT_LANDSCAPE)
FITS = (FIT_CONTAIN, FIT_FILL, FIT_ORIGINAL)
MARGINS = ("none", "small", "medium", "large")

#: PyMuPDF 的 ``insert_image(stream=...)`` **实测**能直接嵌入的容器名
#: （Pillow 报的那个名字，不是扩展名）。
#:
#: 这张表是量出来的，不是查文档查来的：拿每种格式各存一张 60×40 的小图，
#: 逐一 ``insert_image`` 看它认不认。结果 —— JPEG / PNG / BMP / GIF / TIFF
#: 通过；**WEBP 与 HEIF 抛 ``FzErrorFormat: unknown image file format``**。
#:
#: 写这张表的直接原因是两个真机 bug：
#:
#: * ``webp → pdf`` 从第九阶段起就**发布着却转不出来** ——
#:   注册表、能力 API、界面按钮一应俱全，用户点下去拿到
#:   「无法把 xxx.webp 放入 PDF」。它一直没有测试跑到，因为
#:   「新格式逐格真跑」那条只覆盖了 bmp / gif / tiff；
#: * ``heic → pdf`` 是第十阶段 A 新加的一格，一加就撞上同一堵墙。
#:
#: 表外的容器一律先用 Pillow 解码、重新编码成 PNG 或 JPEG 再嵌入
#: （见 :func:`_prepare_image`）。**这是有损的**（JPEG 源会再压一代），
#: 所以结果里会带一句说明，不偷偷做。
#: ``test_embeddable_formats_match_pymupdf`` 逐格式重测这张表，
#: 免得将来 MuPDF 支持了新格式而这里还在一律转码。
_EMBEDDABLE_FORMATS = frozenset({"JPEG", "PNG", "BMP", "GIF", "TIFF"})


@dataclass(slots=True)
class ImageSource:
    """一张待排版的图片。"""

    filename: str
    data: bytes
    width: int   # 像素
    height: int  # 像素


@dataclass(slots=True)
class PdfLayout:
    """排版参数。"""

    page_size: str = PAGE_SIZE_AUTO
    orientation: str = ORIENT_AUTO
    fit: str = FIT_CONTAIN
    margin: str = "none"
    custom_width_mm: float | None = None
    custom_height_mm: float | None = None


@dataclass(slots=True)
class PdfBuildResult:
    """排版结果。"""

    data: bytes
    notes: list[str]


def build_pdf_from_images(images: list[ImageSource], layout: PdfLayout) -> PdfBuildResult:
    """按顺序把图片排成一份 PDF，返回 PDF 字节与需要说明的事项。"""
    if not images:
        raise ValidationError("请至少上传一张图片")

    notes: list[str] = []
    rotated = 0
    transcoded: list[str] = []
    multi_frame: list[tuple[str, int]] = []
    doc = pymupdf.open()
    try:
        for image in images:
            prepared = _prepare_image(image)
            if prepared.rotated:
                rotated += 1
            if prepared.transcoded:
                transcoded.append(image.filename)
            frames = _multi_frame_info(image)
            if frames is not None:
                multi_frame.append(frames)
            _append_image(doc, prepared, layout)
        # deflate + garbage + clean：JPEG 直通之外的内容（PNG、文字）会被压到最小，
        # 不加这一步，一份几页的相册 PDF 会平白大出几十倍
        data = doc.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        doc.close()

    # 「你给的文件里还有内容没进来」是最该让用户先看到的一句话，排在旋转之前
    notes.extend(_multi_frame_notes(multi_frame))
    if rotated:
        notes.append(f"有 {rotated} 张图片带有旋转信息，已按 EXIF 方向摆正后放入 PDF。")
    if transcoded:
        # 如实说，不偷偷做：这一步对 JPEG 之外的源是有损的（再压一代），
        # 用户拿到一份比原图糊一点的照片时该知道为什么。
        notes.append(
            f"有 {len(transcoded)} 张图片（{'、'.join(transcoded)}）"
            "的格式无法直接嵌入 PDF，已重新编码后放入。"
        )
    if layout.fit == FIT_FILL:
        notes.append("「填充页面」会把图片按比例放大到铺满整页，超出页面的部分已被裁剪。")
    elif layout.fit == FIT_ORIGINAL:
        notes.append(
            "「原始大小」按 1 像素 = 1 磅放置、不做缩放；"
            "比版心大的图片会超出页面，超出部分已被裁剪。"
        )
    return PdfBuildResult(data=data, notes=notes)


# ----------------------------------------------------------------------
# 单页排版
# ----------------------------------------------------------------------

@dataclass(slots=True)
class PreparedImage:
    """摆正之后、真正要嵌入 PDF 的图片。

    ``width`` / ``height`` 是**旋转之后**的尺寸：手机竖拍的照片
    原始像素是横的，靠 EXIF 标记转 90 度显示，页面尺寸必须按转正后的算。

    ``rotated`` 与 ``transcoded`` 是**两件不同的事**，所以是两个字段：
    前者是「按 EXIF 摆正了」，后者是「这个容器 PyMuPDF 嵌不进去，
    重新编码过了」。合成一个布尔会逼着那句说明撒谎 ——
    一张方向正常的 WEBP 没有旋转，但它确实被重新编码了。
    """

    data: bytes
    width: int
    height: int
    rotated: bool
    filename: str
    transcoded: bool = False

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 1.0


def _append_image(
    doc: pymupdf.Document,
    image: PreparedImage,
    layout: PdfLayout,
) -> None:
    page_rect = _page_rect(image, layout)
    page = doc.new_page(width=page_rect.width, height=page_rect.height)

    content = _content_rect(page_rect, layout.margin)
    if content.is_empty or content.width <= 0 or content.height <= 0:
        raise ValidationError("页边距过大，页面内没有可放置图片的区域，请减小页边距")

    if layout.fit == FIT_FILL:
        target = _cover_rect(content, image.aspect)
    elif layout.fit == FIT_ORIGINAL:
        target = _natural_rect(content, image)
    else:
        target = content

    try:
        page.insert_image(target, stream=image.data, keep_proportion=True)
    except Exception as exc:  # pragma: no cover - 正常校验过的图片不会走到这里
        raise ValidationError(f"无法把 {image.filename} 放入 PDF，请换一张图片试试") from exc


def _page_rect(image: PreparedImage, layout: PdfLayout) -> pymupdf.Rect:
    """算出这一页的尺寸。"""
    margin = settings.PDF_MARGINS.get(layout.margin, 0.0)

    if layout.page_size == PAGE_SIZE_AUTO:
        # 自动：页面 = 图片尺寸（1 像素 = 1 点），方向参数此时没有意义，忽略
        width = float(image.width) + margin * 2
        height = float(image.height) + margin * 2
        return pymupdf.Rect(0, 0, width, height)

    width, height = _fixed_page_size(layout)

    # 固定尺寸时，方向可以是「跟随图片」
    landscape = width > height
    if layout.orientation == ORIENT_AUTO:
        landscape = image.width > image.height
    elif layout.orientation == ORIENT_PORTRAIT:
        landscape = False
    elif layout.orientation == ORIENT_LANDSCAPE:
        landscape = True

    short, long = min(width, height), max(width, height)
    if landscape:
        return pymupdf.Rect(0, 0, long, short)
    return pymupdf.Rect(0, 0, short, long)


def _fixed_page_size(layout: PdfLayout) -> tuple[float, float]:
    """固定页面尺寸，返回 (宽, 高)，单位点。"""
    if layout.page_size == "custom":
        return _custom_page_size(layout)
    return settings.PDF_PAGE_SIZES[layout.page_size]


def _custom_page_size(layout: PdfLayout) -> tuple[float, float]:
    width_mm = layout.custom_width_mm
    height_mm = layout.custom_height_mm
    if width_mm is None or height_mm is None:
        raise ValidationError("请填写自定义页面的宽度和高度")
    for label, value in (("宽度", width_mm), ("高度", height_mm)):
        if not _MIN_CUSTOM_MM <= value <= _MAX_CUSTOM_MM:
            raise ValidationError(
                f"自定义页面{label}需要在 {_MIN_CUSTOM_MM:.0f}–{_MAX_CUSTOM_MM:.0f} 毫米之间"
            )
    return width_mm * _MM_TO_PT, height_mm * _MM_TO_PT


def _content_rect(page_rect: pymupdf.Rect, margin: str) -> pymupdf.Rect:
    """页面去掉页边距之后，真正可以放图片的区域。"""
    value = settings.PDF_MARGINS.get(margin, 0.0)
    return pymupdf.Rect(
        page_rect.x0 + value,
        page_rect.y0 + value,
        page_rect.x1 - value,
        page_rect.y1 - value,
    )


def _cover_rect(box: pymupdf.Rect, aspect: float) -> pymupdf.Rect:
    """铺满版心：按比例放大到完全覆盖 box 并居中，超出部分落在页面外被裁掉。"""
    if box.width / box.height > aspect:
        # 版心比图片更「宽」，以宽度为准，高度溢出
        width = box.width
        height = box.width / aspect
    else:
        height = box.height
        width = box.height * aspect

    center_x = (box.x0 + box.x1) / 2
    center_y = (box.y0 + box.y1) / 2
    return pymupdf.Rect(
        center_x - width / 2,
        center_y - height / 2,
        center_x + width / 2,
        center_y + height / 2,
    )


def _natural_rect(box: pymupdf.Rect, image: PreparedImage) -> pymupdf.Rect:
    """原始大小：**1 像素 = 1 磅**，居中放在版心内，不缩放。

    与「自动页面尺寸」用的是同一套换算（那里也是 1 像素 = 1 磅），
    所以「自动页面 + 原始大小」会得到一张正好贴合图片、四周只有页边距的页面。
    放在固定纸张上时图片可能比版心大，超出的部分落在页面外被自然裁掉 ——
    这是「原始大小」这个选项**本身**的含义，不是缺陷，界面上会如实说明。
    """
    center_x = (box.x0 + box.x1) / 2
    center_y = (box.y0 + box.y1) / 2
    half_width = float(image.width) / 2
    half_height = float(image.height) / 2
    return pymupdf.Rect(
        center_x - half_width,
        center_y - half_height,
        center_x + half_width,
        center_y + half_height,
    )


# ----------------------------------------------------------------------
# 多帧源文件
# ----------------------------------------------------------------------

def _multi_frame_info(image: ImageSource) -> tuple[str, int] | None:
    """这张图片是不是多帧 / 多页文件；不是就返回 ``None``。"""
    source_format, frames = source_frame_info(image.data)
    if frames <= 1:
        return None
    return source_format, frames


def _multi_frame_notes(found: list[tuple[str, int]]) -> list[str]:
    """多帧源文件的说明文案（第九阶段决策 C：只取第一帧，但要如实说明）。

    只有一个多帧源时用 ``frame_note`` 的原文案 —— 与图片转图片那条路
    一字不差。一批里出现多个多帧源时（``/api/pdf/from-images`` 可以一次
    收多张图）逐条重复会很吵，汇总成一句，但每个文件的帧数照样列出来。
    """
    if not found:
        return []
    if len(found) == 1:
        return [frame_note(*found[0])]
    described = "、".join(
        f"TIFF {count} 页" if source_format == "tiff" else f"{source_format.upper()} {count} 帧"
        for source_format, count in found
    )
    return [
        f"这批图片里有 {len(found)} 个多帧 / 多页文件（{described}），"
        "本次每个只取了第一帧（页），其余内容没有放进 PDF。"
    ]


# ----------------------------------------------------------------------
# 图片预处理
# ----------------------------------------------------------------------

def _prepare_image(image: ImageSource) -> PreparedImage:
    """把图片弄成「PyMuPDF 嵌得进去、方向也对」的字节。

    两件事都可能需要重新编码，它们是**独立**的：

    1. **EXIF 方向**：手机照片常常带着「需要旋转 90°」的标记。PDF 里没有
       EXIF 这一说，直接嵌入会让照片躺倒，所以这类图片先用 Pillow 转正。
    2. **容器 PyMuPDF 认不认**：WEBP 与 HEIF 实测嵌不进去（见
       :data:`_EMBEDDABLE_FORMATS`），必须重新编码成 PNG / JPEG。

    两件都不需要时**原样直通** —— JPEG 不重新编码，画质和体积都不受影响。
    这一条是刻意保留的：JPEG 是最常见的输入，重压一代的代价（画质下降、
    体积反弹）是白挨的。
    """
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(image.data)) as img:
            container = img.format or ""
            orientation = img.getexif().get(0x0112, 1)
            needs_rotation = orientation not in (None, 1)
            embeddable = container in _EMBEDDABLE_FORMATS

            if embeddable and not needs_rotation:
                return PreparedImage(
                    data=image.data,
                    width=image.width,
                    height=image.height,
                    rotated=False,
                    filename=image.filename,
                )

            work = ImageOps.exif_transpose(img) if needs_rotation else img
            # ``exif_transpose`` 返回新对象，``img`` 还在 ``with`` 里，
            # 所以没旋转的那一支要复制出来再离开上下文管理器。
            work = work.copy()
            width, height = work.size
    except Exception:
        # 读不出 EXIF 就当它不需要旋转；真正的解码失败在前面校验阶段已经拦掉了
        return PreparedImage(
            data=image.data,
            width=image.width,
            height=image.height,
            rotated=False,
            filename=image.filename,
        )

    buffer = io.BytesIO()
    # 重新编码的目标容器怎么选：
    #
    # * **本来就是 PNG** → 还是 PNG。PNG 是无损的，转成 JPEG 白掉一代画质，
    #   而截图类 PNG 转 JPEG 之后反而更大；
    # * **带透明** → PNG。JPEG 存不下 alpha，转 RGB 会把透明压成黑底，
    #   那是把内容弄丢了，不是「有损」；
    # * **其余**（WEBP / HEIC 这类要转码的摄影图）→ JPEG q95。
    #   12MP 的照片存成 PNG 能有几十兆，塞进 PDF 不可接受。
    if container == "PNG" or work.mode in ("RGBA", "LA", "P", "PA"):
        work.save(buffer, "PNG")
    else:
        work.convert("RGB").save(buffer, "JPEG", quality=95)
    return PreparedImage(
        data=buffer.getvalue(),
        width=width,
        height=height,
        rotated=needs_rotation,
        filename=image.filename,
        transcoded=not embeddable,
    )
