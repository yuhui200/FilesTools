"""把 :mod:`office.document_ir` 的中间表示写成 DOCX 字节。

用 python-docx，不手搓 OOXML。手搓 XML 意味着自己负责关系表、内容类型表、
样式表的一致性 —— 任何一处写错，用户拿到的就是一份 Word 打不开的文件，
而「文件能打开」是这一阶段的头号要求。python-docx 生成的骨架由它自己保证。

**这一层只认 IR，不认 PDF。** 所以「段落合并对不对」能在没有真 PDF 的情况下
单独测，「分页符数量对不对」也不用先跑一遍 OCR。
"""

from __future__ import annotations

import io
import logging

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor
from PIL import Image, UnidentifiedImageError

from office.document_ir import (
    ImageBlock,
    PageContent,
    Paragraph,
    TableBlock,
    TextRun,
)
from utils.errors import DocxGenerationError

__all__ = ["DocxBuild", "build_docx"]

logger = logging.getLogger(__name__)

#: A4，和绝大多数中文 PDF 一致。python-docx 默认模板是 Letter，
#: 不改的话同样的内容在 A4 打印机上会挤成两页。
PAGE_WIDTH_CM = 21.0
PAGE_HEIGHT_CM = 29.7
MARGIN_CM = 2.54

#: 正文可用宽度（磅）。图片超过这个宽度会被裁掉，所以要按它等比缩放。
_USABLE_WIDTH_PT = (PAGE_WIDTH_CM - 2 * MARGIN_CM) / 2.54 * 72

#: 图片下方那句说明的样式
CAPTION_SIZE_PT = 9.0
CAPTION_COLOR = RGBColor(0x60, 0x60, 0x60)

#: 标题显式用黑色。Word 默认模板的 Heading 样式是主题蓝，
#: 一份合同转出来标题变成蓝色，用户会以为是转坏了。
HEADING_COLOR = RGBColor(0x00, 0x00, 0x00)

_ALIGNMENTS = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
}

#: python-docx 自己认得的图片格式。其余（PDF 里常见的 jpx / ccitt / jbig2）
#: 必须先转码，直接塞进去会抛 UnrecognizedImageError。
_PASSTHROUGH_FORMATS = {"PNG", "JPEG", "GIF", "BMP", "TIFF"}


class DocxBuild:
    """一次构建的产物，以及「有什么没能原样放进去」。

    返回对象而不是裸 bytes，是因为被跳过的图片必须能传到用户眼前 ——
    悄悄少一张图，用户是打开 Word 之后才发现，那时候已经不知道是哪一步的问题了。
    """

    __slots__ = ("data", "skipped_images", "total_images")

    def __init__(self, data: bytes, *, skipped_images: int = 0, total_images: int = 0) -> None:
        self.data = data
        self.skipped_images = skipped_images
        self.total_images = total_images


def build_docx(pages: list[PageContent], *, east_asian_font: str) -> DocxBuild:
    """把若干页 IR 写成一份 DOCX。

    ``pages`` 的顺序就是页序；页与页之间插分页符，共 ``len(pages) - 1`` 个。
    页内的块按列表顺序落下去，这就是「页面顺序正确」的实现方式。
    """
    if not pages:
        raise DocxGenerationError("没有内容可以写入 Word 文档。")

    counts = {"skipped": 0, "total": 0}
    try:
        document = Document()
        _setup_page(document)
        _setup_base_font(document, east_asian_font)

        for index, page in enumerate(pages):
            if index:
                # 分页符是 DOCX 里「页」唯一真实的表示，也是页序的锚点
                document.add_page_break()
            for block in page.blocks:
                _write_block(document, block, east_asian_font, counts)

        buffer = io.BytesIO()
        document.save(buffer)
    except DocxGenerationError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何写入失败都要变成明确的错误码
        logger.warning("生成 DOCX 失败", exc_info=True)
        raise DocxGenerationError(
            "生成 Word 文档时失败，请重试或换一份文件。"
        ) from exc

    data = buffer.getvalue()
    if not data:
        raise DocxGenerationError("生成 Word 文档时失败，请重试或换一份文件。")
    return DocxBuild(
        data, skipped_images=counts["skipped"], total_images=counts["total"]
    )


def _setup_page(document) -> None:
    """页面尺寸与页边距。改的是默认模板的 Normal 节。"""
    for section in document.sections:
        section.page_width = Cm(PAGE_WIDTH_CM)
        section.page_height = Cm(PAGE_HEIGHT_CM)
        section.left_margin = Cm(MARGIN_CM)
        section.right_margin = Cm(MARGIN_CM)
        section.top_margin = Cm(MARGIN_CM)
        section.bottom_margin = Cm(MARGIN_CM)


def _setup_base_font(document, east_asian_font: str) -> None:
    """把中文字体写进 Normal 样式。

    只设 ``font.name`` 是不够的：它落在 ``w:ascii`` / ``w:hAnsi`` 上，
    管不到中文。中文走的是 ``w:eastAsia``，不显式给的话由阅读器自己挑
    兜底字体，同一份文件在不同电脑上会长得不一样。
    """
    try:
        normal = document.styles["Normal"]
    except KeyError:  # pragma: no cover - 默认模板一定有 Normal
        logger.warning("默认模板里没有 Normal 样式，跳过基础字体设置")
        return
    normal.font.name = east_asian_font
    _set_east_asian(normal.element.get_or_add_rPr(), east_asian_font)


def _set_east_asian(rpr, font_name: str) -> None:
    """给某个 rPr 节点补上 ``w:eastAsia``。"""
    rpr.get_or_add_rFonts().set(qn("w:eastAsia"), font_name)


def _write_block(document, block, east_asian_font: str, counts: dict) -> None:
    if isinstance(block, Paragraph):
        _write_paragraph(document, block, east_asian_font)
    elif isinstance(block, TableBlock):
        _write_table(document, block, east_asian_font)
    elif isinstance(block, ImageBlock):
        _write_image(document, block, east_asian_font, counts)
    else:  # pragma: no cover - IR 只有这三种块
        logger.warning("跳过无法识别的块类型 %s", type(block).__name__)


def _write_paragraph(document, block: Paragraph, east_asian_font: str) -> None:
    if block.level:
        # 不用 add_heading(text)：这里要按 run 分别还原粗体斜体，
        # 先建一个套了 Heading 样式的空段落再往里加 run。
        paragraph = document.add_paragraph(style=f"Heading {block.level}")
    else:
        paragraph = document.add_paragraph()

    if block.align in _ALIGNMENTS:
        paragraph.alignment = _ALIGNMENTS[block.align]

    for source in block.runs:
        _add_run(paragraph, source, east_asian_font, heading=bool(block.level))


def _add_run(paragraph, source: TextRun, east_asian_font: str, *, heading: bool) -> None:
    if not source.text:
        return
    run = paragraph.add_run(source.text)
    if source.bold:
        run.bold = True
    if source.italic:
        run.italic = True
    if source.size_pt:
        run.font.size = Pt(source.size_pt)
    if heading:
        # Heading 样式自带主题色，不覆盖的话标题会是蓝的
        run.font.color.rgb = HEADING_COLOR
    _set_east_asian(run._element.get_or_add_rPr(), east_asian_font)


def _write_table(document, block: TableBlock, east_asian_font: str) -> None:
    rows = [row for row in block.rows if row]
    if not rows:
        return
    columns = max(len(row) for row in rows)
    if columns < 1:
        return

    table = document.add_table(rows=len(rows), cols=columns)
    try:
        table.style = "Table Grid"
    except KeyError:  # pragma: no cover - 默认模板里有这个样式
        # 没有边框样式也比没有表格强，至少内容还在
        logger.warning("默认模板里没有 Table Grid 样式，表格将没有边框")

    for row_index, row in enumerate(rows):
        for column_index in range(columns):
            value = row[column_index] if column_index < len(row) else ""
            cell = table.cell(row_index, column_index)
            cell.text = value
            if not value:
                continue
            # 单元格里的字体同样要补 w:eastAsia
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    _set_east_asian(run._element.get_or_add_rPr(), east_asian_font)


def _write_image(document, block: ImageBlock, east_asian_font: str, counts: dict) -> None:
    counts["total"] += 1
    data = _to_supported_image(block.data)
    if data is None:
        # 一张图读不出来不该让整份文档失败 —— 但也不能装作没这回事
        counts["skipped"] += 1
        logger.warning("跳过一张无法转码的图片（%s 字节）", len(block.data or b""))
        return

    width_pt, height_pt = _fit_size(block.width_pt, block.height_pt)
    try:
        if width_pt > 0 and height_pt > 0:
            # 宽高都给：只给 width 的话 python-docx 会按图片自身的像素比算高度，
            # 而 IR 里的宽高是从 PDF 页面矩形来的 —— 那才是「图在原文档里占多大」
            # 的权威来源，渲染取整带来的像素比误差不该让它跑偏。
            document.add_picture(
                io.BytesIO(data), width=Inches(width_pt / 72.0), height=Inches(height_pt / 72.0)
            )
        else:
            document.add_picture(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - 单张图失败不牵连整份文档
        counts["skipped"] += 1
        logger.warning("插入图片失败，已跳过", exc_info=True)
        return

    if block.caption:
        _write_caption(document, block.caption, east_asian_font)


def _write_caption(document, text: str, east_asian_font: str) -> None:
    paragraph = document.add_paragraph()
    run = paragraph.add_run(text)
    run.italic = True
    run.font.size = Pt(CAPTION_SIZE_PT)
    run.font.color.rgb = CAPTION_COLOR
    _set_east_asian(run._element.get_or_add_rPr(), east_asian_font)


def _fit_size(width_pt: float, height_pt: float) -> tuple[float, float]:
    """把图片尺寸夹到正文宽度以内，等比缩放。

    不夹的话，一张按原始页面宽度给的图会顶出页边距，Word 里直接被裁掉一截。
    """
    try:
        width = float(width_pt)
        height = float(height_pt)
    except (TypeError, ValueError):
        return (0.0, 0.0)
    if width <= 0 or height <= 0:
        # 尺寸不可信时交给 python-docx 按图片自带 DPI 算，总比不给强
        return (0.0, 0.0)
    if width <= _USABLE_WIDTH_PT:
        return (width, height)
    ratio = _USABLE_WIDTH_PT / width
    return (_USABLE_WIDTH_PT, height * ratio)


def _to_supported_image(data: bytes) -> bytes | None:
    """把图片转成 python-docx 认得的格式；转不了返回 None。

    PDF 里嵌的图常常是 jpx（JPEG 2000）/ ccitt / jbig2，python-docx 一个都不认，
    直接塞会抛 ``UnrecognizedImageError``。PNG / JPEG 原样透传（不重新编码，
    免得白白掉一次画质），其余一律交给 PIL 转成 PNG。
    """
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = (image.format or "").upper()
            if image_format in _PASSTHROUGH_FORMATS:
                return data
            buffer = io.BytesIO()
            # 带透明通道的转成 RGBA 存 PNG，其余按 RGB 存 PNG
            mode = "RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB"
            image.convert(mode).save(buffer, format="PNG")
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        logger.warning("图片无法解码，已跳过", exc_info=True)
        return None
