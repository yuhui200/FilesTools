"""PDF 的打开、校验与渲染。

所有 PDF 功能共用这一层，保证三件事只有一份实现：

- PyMuPDF 的异常类型五花八门，统一翻译成用户能看懂的中文提示；
- 页数上限、加密检测、渲染像素上限等资源限制统一在这里卡住，
  新加功能时不会漏掉某一条安全约束；
- 渲染（缩略图、转图片）只走这一条路径，不会各处写一套参数。

PyMuPDF 的渲染是 CPU 密集型的，调用方必须通过 ``services/intake.run_in_pool``
放进线程池执行，不要直接在事件循环里调用。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from config import settings
from utils.errors import (
    CorruptedFileError,
    PdfEncryptedError,
    PdfTooManyPagesError,
    ProcessingError,
    UnsupportedTypeError,
    ValidationError,
)

__all__ = [
    "BROKEN_PDF_MESSAGE",
    "ENCRYPTED_PDF_MESSAGE",
    "UNSUPPORTED_PDF_MESSAGE",
    "looks_like_pdf",
    "open_pdf",
    "page_pixel_size",
    "render_page",
    "render_pixmap",
    "render_thumbnail",
    "validate_pdf_upload",
]

# 面向用户的统一提示文案
BROKEN_PDF_MESSAGE = "PDF 文件损坏，无法读取。"
ENCRYPTED_PDF_MESSAGE = "该 PDF 已加密，需要密码才能打开，暂时无法处理。"
UNSUPPORTED_PDF_MESSAGE = "暂不支持该文件格式，请上传 PDF 文件。"
EMPTY_PDF_MESSAGE = "该 PDF 没有任何页面，无法处理。"

# PDF 文件头。规范允许文件头出现在前 1024 字节内（前面可能有 BOM 或空白）
_PDF_MAGIC = b"%PDF-"
_HEADER_SCAN_BYTES = 1024


def looks_like_pdf(data: bytes) -> bool:
    """按文件头判断是不是 PDF。

    只做「像不像」的粗筛，真正的可用性判断在 :func:`open_pdf` 里。
    """
    return _PDF_MAGIC in data[:_HEADER_SCAN_BYTES]


def validate_pdf_upload(path: Path, original_filename: str) -> int:
    """完整校验一个上传的 PDF，返回页数。

    与图片一样是三层：扩展名白名单 → 文件头 → 真正打开一次。
    差别在于 PDF 没有「解码」这一步，打开成功并且页数正常就算通过，
    因此这里直接复用 :func:`open_pdf` 的全部检查。
    """
    ext = Path(original_filename).suffix.lower()
    if ext not in settings.ALLOWED_PDF_EXTENSIONS:
        raise UnsupportedTypeError(UNSUPPORTED_PDF_MESSAGE)

    doc = open_pdf(path)
    try:
        return doc.page_count
    finally:
        doc.close()


def open_pdf(path: Path, *, max_pages: int | None = None) -> pymupdf.Document:
    """打开 PDF 并做完全部资源与安全检查，返回可直接使用的文档对象。

    调用方负责关闭返回的文档（用 ``with`` 或 ``try/finally``）。

    检查顺序是刻意的：先确认「是不是 PDF」，再确认「能不能打开」，
    最后才是「大不大」——这样用户拿一个 Word 文档过来，
    看到的是「请上传 PDF」而不是「文件损坏」。

    **从内存流打开而不是按路径打开**，有两个实测出来的理由：

    1. 按路径打开时，PyMuPDF 会持有文件句柄；一旦打开失败
       （损坏的 PDF 正是这种情况），句柄要等异常对象被回收才释放，
       Windows 上表现为临时文件删不掉 —— 清理动作会盖住真正的报错。
       走内存流则完全没有文件句柄，删文件和打开文件互不影响。
    2. 按路径打开时 PyMuPDF 会用扩展名猜类型，而我们的落盘文件名是
       ``<随机 token>.upload``，显式指定 ``filetype="pdf"`` 更稳妥。
    """
    if not path.is_file():
        raise ValidationError("上传的文件不存在或已被清理，请重新上传")

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValidationError("无法读取上传的文件") from exc

    if not looks_like_pdf(data):
        raise UnsupportedTypeError(UNSUPPORTED_PDF_MESSAGE)

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # PyMuPDF 对损坏文件抛的异常类型不止一种
        raise CorruptedFileError(BROKEN_PDF_MESSAGE) from exc

    try:
        if doc.needs_pass:
            raise PdfEncryptedError(ENCRYPTED_PDF_MESSAGE)

        if not doc.is_pdf:
            raise UnsupportedTypeError(UNSUPPORTED_PDF_MESSAGE)

        if doc.page_count <= 0:
            raise CorruptedFileError(EMPTY_PDF_MESSAGE)

        limit = settings.MAX_PDF_PAGES if max_pages is None else max_pages
        if doc.page_count > limit:
            raise PdfTooManyPagesError(
                f"该 PDF 有 {doc.page_count} 页，超过一次最多处理 {limit} 页的限制。"
                "请先用「PDF 拆分」分成几个小文件再处理。"
            )
    except Exception:
        doc.close()
        raise

    return doc


def page_pixel_size(page: pymupdf.Page, zoom: float) -> tuple[int, int]:
    """算出某一页在给定缩放倍数下会渲染成多少像素。"""
    rect = page.rect
    return max(1, round(rect.width * zoom)), max(1, round(rect.height * zoom))


def _safe_zoom(page: pymupdf.Page, zoom: float) -> tuple[float, bool]:
    """把缩放倍数限制在像素上限内，返回 (安全倍数, 是否被下调)。"""
    width, height = page_pixel_size(page, zoom)
    pixels = width * height
    limit = settings.PDF_RENDER_MAX_PIXELS
    if pixels <= limit:
        return zoom, False

    # 按面积等比缩小缩放倍数
    factor = (limit / pixels) ** 0.5
    return zoom * factor, True


def render_pixmap(
    doc: pymupdf.Document,
    index: int,
    *,
    dpi: int | None = None,
    width: int | None = None,
) -> tuple[pymupdf.Pixmap, bool]:
    """把某一页渲染成位图，返回 (位图, 是否因像素上限被降采样)。

    ``width`` 优先：按目标宽度渲染（缩略图用）；
    否则按 ``dpi`` 渲染，默认 :data:`PDF_RENDER_DEFAULT_DPI`。

    需要自己编码格式（JPG / WEBP）的调用方直接用这个函数，
    只要 PNG 的话用 :func:`render_page`。
    """
    if not 0 <= index < doc.page_count:
        raise ProcessingError(f"页码超出范围：第 {index + 1} 页不存在")

    page = doc[index]
    base = page.rect
    if base.width <= 0 or base.height <= 0:
        raise ProcessingError(f"第 {index + 1} 页尺寸异常，无法渲染")

    if width is not None:
        zoom = width / base.width
    else:
        zoom = (settings.PDF_RENDER_DEFAULT_DPI if dpi is None else dpi) / 72.0

    zoom, clamped = _safe_zoom(page, zoom)

    try:
        # alpha=False：透明区域压到白底，导出 JPG 时不会出现黑块
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    except Exception as exc:
        raise ProcessingError(f"第 {index + 1} 页渲染失败，文件可能已损坏") from exc

    return pixmap, clamped


def render_page(
    doc: pymupdf.Document,
    index: int,
    *,
    dpi: int | None = None,
    width: int | None = None,
) -> tuple[bytes, bool]:
    """把某一页渲染成 PNG 字节，返回 (png 字节, 是否因像素上限被降采样)。"""
    pixmap, clamped = render_pixmap(doc, index, dpi=dpi, width=width)
    return pixmap.tobytes("png"), clamped


def render_thumbnail(doc: pymupdf.Document, index: int) -> bytes:
    """渲染页面缩略图（PNG），用于前端的页面选择器。"""
    data, _clamped = render_page(doc, index, width=settings.PDF_THUMBNAIL_WIDTH)
    return data
