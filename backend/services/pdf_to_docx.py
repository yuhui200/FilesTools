"""PDF → Word 的工作流（第六阶段 A）。

只做一件事：把一份 PDF 转成 DOCX。不碰 PDF → Excel、PDF → PPT。

流程是：

1. 收文件（复用第三阶段的 ``receive_pdf``，类型 / 大小 / 页数上限全都一样）；
2. 打开 PDF，**逐页判断**这一页是文字页还是扫描页 —— 封面是图、正文是字的
   PDF 不该被整份送去 OCR；
3. 文字页走 ``pdf/analyzer.py`` 直接抽文字；
4. 扫描页走 OCR：按 200 DPI 渲染当页（**逐页**，绝不把整份转成一张大图）
   → ``services/ocr_service.py`` 识别 → 拿不到文字就换 300 DPI 重试一次；
   结果做成「原图 + 可编辑文字」——原图是 150 DPI 的 JPEG，文字是真段落。
   OCR 组件没装时给 ``OCR_UNAVAILABLE``：既不能返回 500，也**不能把这一页
   悄悄跳过** —— 跳过的话用户会拿到一份悄悄少了几页的 Word，比报错糟糕得多；
5. ``office/docx_writer.py`` 把结果写成 DOCX；
6. **用 python-docx 重新打开验证一次**，打不开就不交给用户。

超时是**协作式**的。``asyncio.wait_for`` 取消不了线程池里的线程，
OCR 又不像 ``office_converter`` 那样有子进程可杀（它靠
``subprocess.run(timeout=)`` 杀 LibreOffice）。所以工作线程自己在每页开工前
检查一次是否超时，主动抛 :class:`PdfConversionTimeoutError`。
外层 ``PDF_TO_WORD_TIMEOUT_SECONDS`` 仍然兜底，且必须**严格大于**
``PDF_TO_WORD_WORKER_BUDGET_SECONDS``，好让协作式那一路先触发 ——
否则用户看到的是笼统的「处理超时」，而不是这里写得更具体的说明。
"""

from __future__ import annotations

import importlib.util
import io
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import UploadFile

from config import settings
from office.document_ir import (
    PAGE_KIND_SCAN,
    PAGE_KIND_TEXT,
    Block,
    ImageBlock,
    PageContent,
    Paragraph,
    TextRun,
)
from pdf.analyzer import analyze_text_page, detect_page_kind
from pdf.loader import EMPTY_PDF_MESSAGE, open_pdf, render_page, render_pixmap
from services import ocr_service
from services.intake import run_in_pool
from services.pdf_service import (
    DOCX_MEDIA_TYPE,
    PdfOutput,
    PdfResult,
    ReceivedPdf,
    receive_pdf,
    register_pdf_result,
)
from services.progress import (
    STAGE_ANALYZING,
    STAGE_DETECTING,
    STAGE_EXTRACTING,
    STAGE_OCR,
    STAGE_VERIFYING,
    STAGE_WRITING,
    progress_store,
)
from utils.errors import (
    ConverterUnavailableError,
    CorruptedFileError,
    DocxGenerationError,
    OcrUnavailableError,
    PdfConversionTimeoutError,
    PdfEmptyError,
    PdfNoTextError,
    ProcessingTimeoutError,
)
from utils.files import build_output_name, new_token, remove_file

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查用
    from office.docx_writer import DocxBuild

__all__ = [
    "DOCUMENT_KIND_MIXED",
    "DOCUMENT_KIND_SCAN",
    "DOCUMENT_KIND_TEXT",
    "EXTRACTION_MIXED",
    "EXTRACTION_OCR",
    "EXTRACTION_TEXT",
    "DocumentResult",
    "convert_pdf_to_docx",
    "docx_available",
    "verify_docx",
]

logger = logging.getLogger(__name__)

#: 整份文档的形态
DOCUMENT_KIND_TEXT = "text"    # 全部页面都有可用文字层
DOCUMENT_KIND_SCAN = "scan"    # 全部页面都要靠 OCR
DOCUMENT_KIND_MIXED = "mixed"  # 两种都有

#: 文字是怎么拿到的
EXTRACTION_TEXT = "text"
EXTRACTION_OCR = "ocr"
EXTRACTION_MIXED = "mixed"

#: 上传表单里没带文件名时用的占位名
FALLBACK_NAME = "document.pdf"

EMPTY_PDF_MESSAGE_FOR_USER = "这份 PDF 一页都没有，无法转换。"
EMPTY_DOCX_MESSAGE = "这份 PDF 没能抽出任何文字，无法生成 Word 文档。"

#: 扫描页需要 OCR。OCR 组件没装时**不能**返回 500 ——
#: 文件本身没毛病，重传多少次都一样，得有人去服务器上装组件。
SCAN_NEEDS_OCR_MESSAGE = (
    "这份 PDF 没有可用的文字层，需要 OCR 识别，"
    "但当前服务器未安装 OCR 组件，暂时无法处理扫描 PDF。"
)

#: 生成后会被打开验证，验证时只关心「能不能读」，不关心内容
VERIFY_FAILED_MESSAGE = "生成的 Word 文档无法正常打开，已放弃这次转换。"

#: 扫描页里原图下方那句提示。用户看到「识别结果」时得知道它是机器认出来的，
#: 而且要拿它跟上面的原图核对 —— 这句话是随页走的，不能只在结果页说一次。
OCR_PAGE_NOTICE = "以下文字由 OCR 识别，可能存在错字或漏字，请对照上方原图核对。"

#: 一页认完一个字都没有。空白页（分隔页、背面）在真实扫描件里很常见，
#: 这是**事实**不是失败，所以只说明，不报错。
OCR_EMPTY_PAGE_NOTICE = "这一页没有识别出文字（可能是空白页或整页为图），已保留原图。"

#: 超出 OCR 页数上限的页。**不能悄悄跳过**：原图照样嵌进去，
#: 但必须让用户知道这一页没有可编辑文字。
OCR_LIMIT_PAGE_NOTICE = "这一页超出了本次 OCR 的页数上限，只保留了原图，没有可编辑文字。"

#: 一页的平均置信度低于这个值才提醒。定得低是有意的：实测里
#: **完全正确**的中文识别平均置信度就在 0.72 上下，拿 0.8 当线会把
#: 认对了的页也报成「把握偏低」，那种提醒等于噪声。
LOW_CONFIDENCE_PAGE = 0.6

#: 服务器上少了 python-docx。和缺 LibreOffice 一样：用户的下一步是找管理员，
#: 重传多少次都没用，所以走 503 而不是「处理失败」。
MISSING_DOCX_MESSAGE = "当前服务器缺少 Word 生成组件，请联系管理员。"


def docx_available() -> bool:
    """服务器上能不能生成 Word 文档。

    **只看 python-docx，与 LibreOffice 无关** —— PDF → Word 全程不启动
    soffice，拿 LibreOffice 的可用性去挡这个按钮，会在没装 LibreOffice
    的服务器上白白禁掉一个本来能用的功能。

    用 ``find_spec`` 而不是 ``import docx``：``/api/config`` 是打开首页就会
    调的，为了回答这个问题去把 lxml 和整个 python-docx 加载起来不值得。
    """
    return importlib.util.find_spec("docx") is not None


def _writer():
    """懒加载 DOCX 写入层。

    **不能放在模块顶层 import。** 少了 python-docx 的话，
    ``import office.docx_writer`` 会直接抛 ImportError，而路由是模块级
    import 的 —— 整个应用都起不来，「缺组件时其它功能照常可用」就成了空话。
    """
    try:
        from office.docx_writer import build_docx
    except ImportError as exc:  # pragma: no cover - 只有缺组件时才会走到
        logger.error("python-docx 不可用", exc_info=True)
        raise ConverterUnavailableError(MISSING_DOCX_MESSAGE) from exc
    return build_docx


@dataclass(slots=True)
class DocumentResult(PdfResult):
    """PDF → Word 的结果。

    在 :class:`PdfResult` 之上补了「这份文档是什么形态」「文字是怎么来的」，
    结果页要显示的就是这几项。
    """

    document_kind: str = DOCUMENT_KIND_TEXT
    extraction_method: str = EXTRACTION_TEXT
    text_pages: int = 0
    ocr_pages: int = 0
    #: 内置模型覆盖的字符集，只读。见 config.OCR_LANGUAGES 的说明。
    ocr_languages: list[str] = field(default_factory=list)
    #: 每一页的形态，顺序与页序一致
    page_kinds: list[str] = field(default_factory=list)

    @property
    def saved_bytes(self) -> int | None:
        """DOCX 通常比源 PDF 大，「省了多少」在这里没有意义。

        与其在结果页显示一个负数，不如不显示 —— 基类那两个字段是为
        「压缩 PDF」这类场景准备的。
        """
        return None

    @property
    def saved_percent(self) -> float | None:
        return None


@dataclass(slots=True)
class _PageStats:
    """工作线程内部用的计数器。"""

    kinds: list[str] = field(default_factory=list)
    multi_column_pages: int = 0
    dropped_tables: int = 0
    #: 因为页面过大被降采样后才拿去识别的页数
    clamped_pages: int = 0
    #: 真正跑过 OCR 的页数（受页数上限约束，可能小于扫描页总数）
    ocr_processed: int = 0
    #: 因为超出 OCR 页数上限而只保留原图的页数
    limited_pages: int = 0
    #: 换更高分辨率重试过的页数
    retried_pages: int = 0
    #: 认完一个字都没有的页数（空白页在扫描件里很常见，这不是失败）
    blank_ocr_pages: int = 0
    #: 平均置信度低于 LOW_CONFIDENCE_PAGE 的页数
    low_confidence_pages: int = 0
    #: 文字页里因为字节预算或格式问题没嵌进去的图片数
    skipped_images: int = 0

    @property
    def text_pages(self) -> int:
        return sum(1 for kind in self.kinds if kind == PAGE_KIND_TEXT)

    @property
    def ocr_pages(self) -> int:
        return sum(1 for kind in self.kinds if kind == PAGE_KIND_SCAN)


@dataclass(slots=True)
class _EmbedBudget:
    """整份文档共用的嵌入图片字节预算。

    嵌进 DOCX 的是**整页渲染图**，一页 150 DPI 的 JPEG 约 200–400 KB。
    不设预算的话，一份 100 页的扫描件会生成几百 MB 的 Word：用户下不动，
    服务器磁盘也受不了。用完就不再嵌图，并如实写进 notes —— 不静默丢，
    文字部分照常输出。
    """

    remaining: int = 0
    skipped: int = 0

    def spend(self, size: int) -> None:
        """记一笔已经嵌进去的字节。"""
        self.remaining = max(self.remaining - max(int(size), 0), 0)

    def skip(self, count: int = 1) -> None:
        self.skipped += count


async def convert_pdf_to_docx(
    file: UploadFile,
    work_dir: Path,
    *,
    progress_id: str | None = None,
) -> DocumentResult:
    """把上传的 PDF 转成 Word，返回可直接下载的结果。"""
    progress_store.start(progress_id, page_count=None)
    received = await _receive(file, work_dir)

    progress_store.update(progress_id, STAGE_DETECTING, page_count=received.page_count)
    timeout = settings.PDF_TO_WORD_TIMEOUT_SECONDS
    try:
        pages, stats, build = await run_in_pool(
            _analyze_document,
            received,
            progress_id=progress_id,
            budget=settings.PDF_TO_WORD_WORKER_BUDGET_SECONDS,
            timeout=timeout,
            timeout_message=(
                f"这份 PDF 的转换超过 {timeout} 秒还没完成，"
                "请先用「PDF 拆分」分成几份更小的文件再转。"
            ),
        )
    except ProcessingTimeoutError as exc:
        # 外层兜底超时。协作式那一路抛的已经是正确的类型，别重复包装。
        if isinstance(exc, PdfConversionTimeoutError):
            raise
        raise PdfConversionTimeoutError(str(exc)) from exc
    finally:
        # 源 PDF 读完即删：后面要的都在内存里了
        remove_file(received.path)

    progress_store.update(progress_id, STAGE_VERIFYING, page_count=received.page_count)
    verify_docx(build.data)

    output = _write_output(work_dir, received, build.data)
    notes = _build_notes(stats, build)

    result = register_pdf_result(
        work_dir,
        [output],
        archive_stem=output.filename,
        original_size=received.size,
        original_pages=received.page_count,
        page_count=received.page_count,
        notes=notes,
    )
    progress_store.finish(progress_id)

    return DocumentResult(
        job=result.job,
        outputs=result.outputs,
        archived=result.archived,
        archive_filename=result.archive_filename,
        page_count=result.page_count,
        original_size=result.original_size,
        original_pages=result.original_pages,
        notes=result.notes,
        document_kind=_document_kind(stats),
        extraction_method=_extraction_method(stats),
        text_pages=stats.text_pages,
        ocr_pages=stats.ocr_pages,
        ocr_languages=list(settings.OCR_LANGUAGES),
        page_kinds=list(stats.kinds),
    )


async def _receive(file: UploadFile, work_dir: Path) -> ReceivedPdf:
    """收下 PDF，并把「一页都没有」翻译成这一阶段专属的错误码。

    ``receive_pdf`` 对空 PDF 抛的是通用的 ``CORRUPTED_FILE``。
    **只在这一条流程里翻译**，阶段 3–5 的返回码一个字都不动。
    """
    try:
        return await receive_pdf(file, work_dir, fallback_name=FALLBACK_NAME)
    except CorruptedFileError as exc:
        if exc.message == EMPTY_PDF_MESSAGE:
            raise PdfEmptyError(EMPTY_PDF_MESSAGE_FOR_USER) from exc
        raise


def _analyze_document(
    received: ReceivedPdf, *, progress_id: str | None, budget: int
) -> tuple[list[PageContent], _PageStats, DocxBuild]:
    """在线程池里跑的那一段：逐页分析 + 生成 DOCX。

    返回 ``(页内容, 统计, 构建结果)``。DOCX 只在这里生成、不落盘 ——
    落盘和登记令牌是异步侧的事，线程里不做 I/O 之外的协调。
    """
    deadline = time.monotonic() + budget
    stats = _PageStats()
    pages: list[PageContent] = []
    embed = _EmbedBudget(remaining=settings.PDF_TO_WORD_MAX_EMBED_BYTES)

    progress_store.update(progress_id, STAGE_EXTRACTING)
    doc = open_pdf(received.path, max_pages=settings.MAX_PDF_PAGES)
    try:
        for index in range(doc.page_count):
            _check_deadline(deadline)
            page = doc[index]

            kind = detect_page_kind(page)
            stats.kinds.append(kind)

            if kind == PAGE_KIND_SCAN:
                # 扫描页必须走 OCR。组件没装时不能返回 500，也不能把这一页
                # 悄悄跳过 —— 跳过的话用户会拿到一份悄悄少了几页的 Word。
                if not ocr_service.is_available():
                    raise OcrUnavailableError(SCAN_NEEDS_OCR_MESSAGE)
                progress_store.update(
                    progress_id, STAGE_OCR, page=index + 1, page_count=doc.page_count
                )
                pages.append(
                    _ocr_page(
                        doc, index, page, stats=stats, embed=embed, deadline=deadline
                    )
                )
                continue

            # 图片和表格按纵向位置插进文字流，页序和阅读顺序都由 analyse 保证。
            analysis = analyze_text_page(page, image_budget=embed.remaining)
            embed.spend(analysis.image_bytes)
            embed.skip(analysis.skipped_images)
            pages.append(analysis.content)
            if analysis.multi_column:
                stats.multi_column_pages += 1
            stats.dropped_tables += analysis.dropped_tables
    finally:
        doc.close()

    # 预算里跳过的图和文字页里跳过的图合成一个数：对用户来说都是「这张图没进来」
    stats.skipped_images += embed.skipped

    if not pages or not _has_text(pages):
        # 兜底：宁可报错，也不交给用户一份空白的 Word
        raise PdfNoTextError(EMPTY_DOCX_MESSAGE)

    progress_store.update(progress_id, STAGE_WRITING)
    build = _writer()(pages, east_asian_font=settings.PDF_TO_WORD_FONT)
    return pages, stats, build


def _ocr_page(
    doc,
    index: int,
    page,
    *,
    stats: _PageStats,
    embed: _EmbedBudget,
    deadline: float,
) -> PageContent:
    """把一页扫描件做成「原图 + 可编辑文字」。

    形态是**刻意选的**：规格里提到的另一种做法是「原图 + 隐藏文字层」，
    那要手搓浮动文本框 XML，在 Word 里不稳定（换阅读器就散架）。规格也说了
    不稳定时优先保证文字可编辑 —— 所以这里的文字是货真价实的段落，
    用户能直接改；原图留在上方供对照。
    """
    if stats.ocr_processed >= settings.PDF_TO_WORD_MAX_OCR_PAGES:
        # 页数上限是硬的：500 页 × 2 秒 = 17 分钟，不卡住就是拿服务器赌气。
        # 超出的页不静默丢弃，原图照样进 Word，只是没有可编辑文字。
        stats.limited_pages += 1
        return _scan_page_content(
            doc, index, page, embed=embed, notice=OCR_LIMIT_PAGE_NOTICE
        )

    lines, clamped = _recognize_page(
        doc, index, dpi=settings.PDF_TO_WORD_OCR_DPI, stats=stats, deadline=deadline
    )
    if not lines:
        # 认不出东西有两种可能：空白页，或者这一页太糊。空白页重试确实白费，
        # 但代价只是一次渲染（几十毫秒），漏掉一页真实内容要严重得多。
        _check_deadline(deadline)
        stats.retried_pages += 1
        lines, retry_clamped = _recognize_page(
            doc,
            index,
            dpi=settings.PDF_TO_WORD_OCR_RETRY_DPI,
            stats=stats,
            deadline=deadline,
        )
        clamped = clamped or retry_clamped

    stats.ocr_processed += 1
    if clamped:
        stats.clamped_pages += 1
    if not lines:
        stats.blank_ocr_pages += 1
    elif sum(line.confidence for line in lines) / len(lines) < LOW_CONFIDENCE_PAGE:
        stats.low_confidence_pages += 1

    blocks: list[Block] = []
    image = _embed_page_image(doc, index, page, embed=embed)
    if image is not None:
        blocks.append(image)
    blocks.append(
        Paragraph(
            runs=[
                TextRun(text=OCR_EMPTY_PAGE_NOTICE if not lines else OCR_PAGE_NOTICE)
            ]
        )
    )
    blocks.extend(
        Paragraph(runs=[TextRun(text=line.text)]) for line in lines if line.text
    )
    return PageContent(blocks=blocks, kind=PAGE_KIND_SCAN)


def _scan_page_content(
    doc, index: int, page, *, embed: _EmbedBudget, notice: str
) -> PageContent:
    """不做 OCR 的扫描页：只保留原图 + 一句说明。"""
    blocks: list[Block] = []
    image = _embed_page_image(doc, index, page, embed=embed)
    if image is not None:
        blocks.append(image)
    blocks.append(Paragraph(runs=[TextRun(text=notice)]))
    return PageContent(blocks=blocks, kind=PAGE_KIND_SCAN)


def _recognize_page(
    doc, index: int, *, dpi: int, stats: _PageStats, deadline: float
) -> tuple[list, bool]:
    """渲染一页并识别，返回 (识别出的行, 这一页是否被降采样)。"""
    _check_deadline(deadline)
    png, clamped = render_page(doc, index, dpi=dpi)
    return ocr_service.recognize(png, dpi=dpi), clamped


def _embed_page_image(doc, index: int, page, *, embed: _EmbedBudget) -> ImageBlock | None:
    """把整页渲成一张低 DPI JPEG 嵌进 Word；预算不够就返回 None。

    **不直接抽 PDF 里那张原图**：扫描页的位图常常是 CCITT / JBIG2 / JPEG2000，
    python-docx 不认，转码还得再来一次全页解码。按 150 DPI 重渲一张 JPEG
    又小又稳 —— 200 DPI 的 PNG 一页 3 MB 上下，150 DPI 的 JPEG 只要几百 KB。

    图嵌不进去不是错误：文字才是这一页的主要内容，图只是对照用的。
    """
    try:
        pixmap, _clamped = render_pixmap(doc, index, dpi=settings.PDF_TO_WORD_EMBED_DPI)
        data = pixmap.tobytes(
            "jpeg", jpg_quality=settings.PDF_TO_WORD_EMBED_JPEG_QUALITY
        )
    except Exception:  # noqa: BLE001 - 图嵌不进去不该让整次转换失败
        logger.warning("第 %s 页的原图渲染失败，这一页只保留文字", index + 1, exc_info=True)
        embed.skip()
        return None

    if not data or len(data) > embed.remaining:
        embed.skip()
        return None
    embed.spend(len(data))

    rect = page.rect
    return ImageBlock(
        data=data,
        width_pt=float(rect.width),
        height_pt=float(rect.height),
        caption=None,
    )


def _check_deadline(deadline: float) -> None:
    """协作式超时检查：每页开工前看一眼。

    之所以不用信号 / 强杀：工作线程跑在共享线程池里，强杀会留下持锁的僵尸线程
    （``office_converter`` 那个单飞锁就是被这个坑过）。
    """
    if time.monotonic() > deadline:
        raise PdfConversionTimeoutError(
            "这份 PDF 的转换用时过长已中止，"
            "请先用「PDF 拆分」分成几份更小的文件再转。"
        )


def _has_text(pages: list[PageContent]) -> bool:
    return any(page.text.strip() for page in pages)


def verify_docx(data: bytes) -> None:
    """用 python-docx 把刚生成的字节重新打开一次。

    「文件能正常打开」是这一阶段的头号要求，所以宁可在这里失败，
    也不把一份打不开的 Word 交给用户 —— 用户拿到坏文件时，
    已经不知道是哪一步出的问题了。
    """
    if not data:
        raise DocxGenerationError(VERIFY_FAILED_MESSAGE)
    try:
        from docx import Document

        Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 打不开就是不交付
        logger.warning("生成的 DOCX 无法重新打开", exc_info=True)
        raise DocxGenerationError(VERIFY_FAILED_MESSAGE) from exc


def _write_output(work_dir: Path, received: ReceivedPdf, data: bytes) -> PdfOutput:
    """落盘。文件名沿用原文件名，只换扩展名：合同.pdf -> 合同.docx。"""
    path = work_dir / f"{new_token()}.docx"
    try:
        path.write_bytes(data)
    except OSError as exc:  # pragma: no cover - 磁盘异常
        raise DocxGenerationError("保存 Word 文档失败，请稍后重试。") from exc
    return PdfOutput(
        filename=build_output_name(received.filename, "", ".docx"),
        path=path,
        media_type=DOCX_MEDIA_TYPE,
        page_count=received.page_count,
        previewable=False,
    )


def _document_kind(stats: _PageStats) -> str:
    if stats.ocr_pages and stats.text_pages:
        return DOCUMENT_KIND_MIXED
    return DOCUMENT_KIND_SCAN if stats.ocr_pages else DOCUMENT_KIND_TEXT


def _extraction_method(stats: _PageStats) -> str:
    if stats.ocr_pages and stats.text_pages:
        return EXTRACTION_MIXED
    return EXTRACTION_OCR if stats.ocr_pages else EXTRACTION_TEXT


def _build_notes(stats: _PageStats, build: DocxBuild) -> list[str]:
    """要主动告诉用户的说明。

    这里写进去的每一条，都是「用户打开 Word 之后会发现的差异」的提前说明。
    与其让人自己看出不对，不如直接讲清楚哪些地方做不到。
    """
    notes: list[str] = []
    if stats.ocr_processed:
        notes.append(
            f"这份 PDF 有 {stats.ocr_processed} 页没有文字层，内容是用 OCR 识别出来的："
            "可能有个别错字，中英混排时的空格、全半角标点也不一定与原件一致。"
            "每页的识别结果上方都保留了原图，请对照核对后再使用。"
        )
    if stats.low_confidence_pages:
        notes.append(
            f"有 {stats.low_confidence_pages} 页的识别把握偏低"
            "（可能是字迹模糊或扫描分辨率不足），建议重点核对。"
        )
    if stats.blank_ocr_pages:
        notes.append(
            f"有 {stats.blank_ocr_pages} 页没有识别出任何文字（空白页或整页为图），"
            "已保留原图。"
        )
    if stats.retried_pages:
        notes.append(
            f"有 {stats.retried_pages} 页在默认分辨率下没有认出来，"
            "已用更高分辨率重新识别了一次。"
        )
    if stats.limited_pages:
        notes.append(
            f"扫描页超过一次最多识别 {settings.PDF_TO_WORD_MAX_OCR_PAGES} 页的上限，"
            f"多出的 {stats.limited_pages} 页没有做 OCR，只保留了原图。"
            "需要这些页的可编辑文字的话，请先用「PDF 拆分」把文件分开再逐份转换。"
        )
    if stats.clamped_pages:
        notes.append(
            f"有 {stats.clamped_pages} 页因为页面尺寸过大被降采样后才识别，"
            "文字准确率可能略低。"
        )
    if stats.multi_column_pages:
        notes.append(
            f"有 {stats.multi_column_pages} 页是多栏排版：文字是按从左到右、"
            "从上到下的顺序提取的，分栏内容可能不会按原来的栏顺序排列。"
        )
    if stats.dropped_tables:
        notes.append(
            f"有 {stats.dropped_tables} 处表格未能可靠识别，已按文字顺序输出，"
            "避免生成一个错位的表格。"
        )
    if stats.skipped_images:
        notes.append(
            f"有 {stats.skipped_images} 张图片没有嵌入"
            "（体积超出上限，或格式无法转换），文字内容不受影响。"
        )
    if build.skipped_images:
        notes.append(
            f"有 {build.skipped_images} 张图片无法转换格式，已跳过。"
        )
    notes.append(
        "Word 里的字号、粗体、斜体按原文档还原，但字体本身不保证与原件一致 —— "
        "字体需要在打开这份文档的电脑上存在。"
    )
    return notes
