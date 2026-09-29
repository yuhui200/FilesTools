"""PDF 页面分析：判断这一页有没有文字层，并把文字层抽成结构化的内容块。

这一层只读 PDF、只产出 :mod:`office.document_ir`，不碰 Word、不碰文件系统。

**两个实测出来的坑，决定了这里的写法：**

1. ``page.get_text("dict")`` 返回的块顺序是**内容流的绘制顺序，不是阅读顺序**。
   一份自下而上写出来的 PDF，读回来的行是倒着的。必须传 ``sort=True``
   （实测它甚至比不排序还快）。这和 OCR 那边的识别框顺序是同一类问题，
   只是低了一层。
2. 块级排序救不了密度高的页：几十次 ``insert_text`` 会被 PyMuPDF 合并成
   **一个** block。所以块内的 ``lines`` 还要按 y、行内的 ``spans`` 还要按 x
   再兜一层排序。

``sort=True`` 是「先上后下、同一行先左后右」，所以真正的双栏页面会读成
``左0 右0 左1 右1``，而不是先读完左栏。这一层不假装能重建分栏，
而是用 :func:`detect_multi_column` 把事实报出来，让上层如实写进结果说明。
"""

from __future__ import annotations

import logging
import math
import re

from config import settings
from office.document_ir import (
    PAGE_KIND_SCAN,
    PAGE_KIND_TEXT,
    ImageBlock,
    PageContent,
    Paragraph,
    TableBlock,
    TextRun,
)

__all__ = [
    "PageAnalysis",
    "TEXT_FONT_BOLD",
    "TEXT_FONT_ITALIC",
    "TEXT_FONT_MONOSPACED",
    "TEXT_FONT_SERIFED",
    "TEXT_FONT_SUPERSCRIPT",
    "analyze_text_page",
    "body_font_size",
    "detect_multi_column",
    "detect_page_kind",
    "derive_alignment",
    "extract_images",
    "text_margins",
    "heading_level",
    "span_style",
]

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# PyMuPDF 的 span flag 位。
# 取值来自 pymupdf 源码里 JM_char_font_flags 的累加方式，已在本机用
# base-14 字体实测核对过（hebo→16、heit→2、hebi→18、cour→8、tiro→4、helv→0）。
# ----------------------------------------------------------------------
TEXT_FONT_SUPERSCRIPT = 1
TEXT_FONT_ITALIC = 2
TEXT_FONT_SERIFED = 4
TEXT_FONT_MONOSPACED = 8
TEXT_FONT_BOLD = 16

#: 字号的可信区间。坏 PDF 会给出 0.4 或 900 这种值，直接写进 DOCX 是灾难。
MIN_FONT_SIZE_PT = 6.0
MAX_FONT_SIZE_PT = 72.0
#: 认不出字号时按正文处理
DEFAULT_BODY_SIZE_PT = 12.0

#: 标题判定：相对正文字号的倍数 -> 标题级别
HEADING_RATIOS: tuple[tuple[float, int], ...] = ((1.6, 1), (1.3, 2), (1.15, 3))

#: 行间距超过「行高中位数 × 这个倍数」就断成新段落。
#: 取 0.6 而不是 1.5：单倍行距的正文里，相邻两行的空白大约是行高的 0.2 倍，
#: 1.5 倍行距约 0.5 倍，而段与段之间通常留得更多。阈值定在 0.6 能把
#: 「同一段的两行」和「两段之间」分开；定成 1.5 就永远断不开，整页并成一段。
PARAGRAPH_GAP_FACTOR = 0.6

#: 对齐判定阈值，都是相对**这一页正文文字区宽度**的比例（不是页面宽度）。
#: 用文字区做基准，左对齐的正文才不会因为「最长的那行没排满」被判成居中。
MIN_INDENT_RATIO = 0.12
CENTER_TOLERANCE_RATIO = 0.06

#: 小于这个尺寸（磅）的「图片」当排版元素处理：细线、色块、1×1 占位图
#: 都不是用户眼里的图片，嵌进 Word 只会添乱。
MIN_IMAGE_PT = 8.0

#: 同一行里前后两截文字的横向间距超过「行高 × 这个倍数」就当成两栏，断开。
#: 实测同一句话被拆成多个 span（粗体切换）时间距只有几个点甚至为 0，
#: 而双栏页面的栏间距要大一个数量级，1.5 倍行高足以区分。
INLINE_GAP_FACTOR = 1.5
#: 已经判定是分栏页面时，门槛压低 —— 窄栏间距可能只有 20 来点，
#: 用 1.5 倍行高（约 25 点）会把两栏粘在一起，那比多断几段严重得多。
#: 但也不能压到 0.5：同一句话里的字体切换也可能隔开近 10 点。
MULTI_COLUMN_INLINE_FACTOR = 0.75
#: 两段文字横向完全错开、且间距超过「页宽 × 这个比例」，才算分栏的证据。
#: 不能只判「错开」：同一行里紧挨着的两个 span 也是错开的。
MIN_GUTTER_RATIO = 0.05
#: 至少要有这么多**行**都出现「同排两段被一大段空白隔开」，才认定是分栏。
#: 只要一行有这种情况就认定的话，页眉左边「第 1 页」右边「内部资料」
#: 这种很常见的版式会被误判成双栏。
MIN_COLUMN_ROWS = 2

#: 少于这么多条矢量线就不去试表格识别。find_tables 实测约 47 ms/页
#: （是 get_text 的五倍），不能在 500 页的文档上无脑跑。
MIN_TABLE_DRAWINGS = 6
MIN_TABLE_ROWS = 2
MIN_TABLE_COLS = 2

#: 中日韩文字与全角标点。用来决定换行拼接时要不要补空格。
_CJK = re.compile(
    r"[⺀-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]"
)


class PageAnalysis:
    """一页的分析结果。

    ``content`` 之外还带上「有什么没能原样保留」，这些都要如实告诉用户，
    而不是悄悄丢掉。
    """

    __slots__ = ("content", "multi_column", "dropped_tables", "image_bytes", "skipped_images")

    def __init__(
        self,
        content: PageContent,
        *,
        multi_column: bool = False,
        dropped_tables: int = 0,
        image_bytes: int = 0,
        skipped_images: int = 0,
    ) -> None:
        self.content = content
        self.multi_column = multi_column
        self.dropped_tables = dropped_tables
        #: 这一页嵌进 Word 的图片总共占了多少字节（用于整份文档的字节预算）
        self.image_bytes = image_bytes
        #: 因为超出预算或取不出来而没嵌进去的图片张数
        self.skipped_images = skipped_images

    def __repr__(self) -> str:  # pragma: no cover - 排查用
        return (
            f"PageAnalysis(kind={self.content.kind!r}, "
            f"blocks={len(self.content.blocks)}, "
            f"multi_column={self.multi_column}, "
            f"dropped_tables={self.dropped_tables})"
        )


# ----------------------------------------------------------------------
# 判断这一页是文字页还是扫描页
# ----------------------------------------------------------------------


def detect_page_kind(page, *, min_chars: int | None = None) -> str:
    """这一页有没有可用的文字层。

    **逐页判断，不整份判断**：封面是图、正文是字的 PDF 很常见，
    整份判成「扫描件」会把几十页本来清清楚楚的文字也送去 OCR。

    阈值不是 0 而是 :data:`config.settings.PDF_TO_WORD_MIN_TEXT_CHARS`：
    不少扫描件带一层由 OCR 软件塞进去的、只认出页眉页码的残缺文字层，
    阈值太低会让这些页被当成文字页抽出来，结果比老老实实 OCR 还差。
    """
    limit = settings.PDF_TO_WORD_MIN_TEXT_CHARS if min_chars is None else min_chars
    try:
        text = page.get_text()
    except Exception:  # noqa: BLE001 - 读不出文字页就当扫描页，后面会去渲染
        logger.warning("读取第 %s 页文字层失败，按扫描页处理", page.number + 1, exc_info=True)
        return PAGE_KIND_SCAN
    return PAGE_KIND_TEXT if len(text.strip()) >= limit else PAGE_KIND_SCAN


# ----------------------------------------------------------------------
# 抽取文字层
# ----------------------------------------------------------------------


def extract_images(page, *, max_bytes: int) -> tuple[list[tuple[tuple, ImageBlock]], int, int]:
    """抽出这一页**真正画出来**的位图，返回 ([(显示位置, 图片)], 用掉的字节, 跳过的张数)。

    用 ``get_image_info`` 而不是 ``get_images``：后者列的是这一页引用的图片资源，
    给不出「画在哪儿、画多大」；前者给的是每一次绘制的显示位置，图片才能
    按纵向位置插回文字流里，而不是统统堆到页尾。

    ``max_bytes`` 是**整份文档**余下的图片字节预算，由调用方逐页递减 ——
    一份 100 页扫描件按原样嵌图能到几百 MB，Word 打开都费劲。
    预算不够时**跳过并计数**，不静默丢弃：调用方会把「有几张图没嵌进去」
    如实写进结果说明。

    刻意不做格式转码：PDF 里的图常是 jpx / ccitt / jbig2，python-docx 不认，
    但转码那一步属于「写 Word」的职责，放在 :mod:`office.docx_writer` 里
    只做一次，两边各转一遍纯属浪费。
    """
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:  # noqa: BLE001 - 取不到图片信息就当这页没有图
        logger.warning("读取第 %s 页图片信息失败", getattr(page, "number", -1) + 1, exc_info=True)
        return [], 0, 0

    found: list[tuple[tuple, ImageBlock]] = []
    used = 0
    skipped = 0
    cache: dict[int, bytes] = {}

    for info in infos or []:
        bbox = info.get("bbox")
        if not bbox:
            continue
        width = float(bbox[2]) - float(bbox[0])
        height = float(bbox[3]) - float(bbox[1])
        # 排版用的细线、色块、1×1 占位图都不是「图片」，嵌进 Word 只会添乱
        if width < MIN_IMAGE_PT or height < MIN_IMAGE_PT:
            continue

        xref = int(info.get("xref") or 0)
        # 同一个 xref 在一页里画多次（页眉 logo 之类）时只解一次
        if xref not in cache:
            cache[xref] = _image_bytes(page, xref)

        data = cache[xref]
        if not data or used + len(data) > max_bytes:
            skipped += 1
            continue

        used += len(data)
        found.append(
            (
                (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                ImageBlock(data=data, width_pt=width, height_pt=height),
            )
        )

    found.sort(key=lambda item: (item[0][1], item[0][0]))
    return found, used, skipped


def _image_bytes(page, xref: int) -> bytes:
    """取出一张图的原始字节。取不出来就返回空 —— 上层按「跳过一张」处理。"""
    if not xref:
        return b""
    try:
        return page.parent.extract_image(xref).get("image") or b""
    except Exception:  # noqa: BLE001 - 单张图取不出来不该让整页失败
        logger.info("第 %s 页有一张图取不出来，已跳过", getattr(page, "number", -1) + 1)
        return b""


def analyze_text_page(page, *, image_budget: int = 0) -> PageAnalysis:
    """把一页文字型 PDF 抽成 :class:`PageContent`。

    ``image_budget`` 是整份文档余下的图片字节预算，见 :func:`extract_images`。
    """
    try:
        raw = page.get_text("dict", sort=True)
    except Exception:  # noqa: BLE001 - 单页抽不动不该让整份文档失败
        logger.warning("抽取第 %s 页文字失败", page.number + 1, exc_info=True)
        return PageAnalysis(PageContent(kind=PAGE_KIND_TEXT))

    all_blocks = raw.get("blocks") or []
    text_blocks = [
        block for block in all_blocks if block.get("type") == 0 and block.get("lines")
    ]
    body_size = body_font_size(all_blocks)
    page_rect = getattr(page, "rect", None)

    tables, dropped = _extract_tables(page, text_blocks)
    table_boxes = [bbox for bbox, _ in tables]
    visible = [
        block
        for block in text_blocks
        if not _inside_any(block.get("bbox") or (0.0, 0.0, 0.0, 0.0), table_boxes)
    ]

    # 图片和表格走同一套「按纵向位置插回文字流」的机制：生成顺序就是阅读顺序，
    # 统统堆到页尾的话，用户拿到的 Word 里图会集体跑到文字后面去。
    images, image_bytes, skipped_images = extract_images(page, max_bytes=image_budget)
    floaters = sorted(
        [*tables, *images],
        key=lambda item: (item[0][1], item[0][0]),
    )

    # 摊平成「(来自哪个块, 行)」的序列，再统一决定段落边界。
    # 之所以要带着块号：块是 PyMuPDF 自己分的，一次 insert_text 就是一个块，
    # 所以真实的段落**经常横跨多个块**；但跨块合并又可能把双栏页面的
    # 左右两栏粘成一段。下面的 _overlaps 就是用来区分这两种情况的。
    lines: list[tuple[int, dict]] = []
    for index, block in enumerate(visible):
        for line in sorted(block.get("lines") or [], key=lambda item: _line_bounds(item)[1]):
            lines.append((index, line))

    heights = [_line_height(line) for _, line in lines]
    line_height = _median([height for height in heights if height > 0])
    gap_limit = max(line_height * PARAGRAPH_GAP_FACTOR, 1.0)
    #: 同一行里两截文字相隔多远还算「接着写」。见 INLINE_GAP_FACTOR 的说明。
    inline_limit = line_height * INLINE_GAP_FACTOR
    #: 纵向差多少还算同一行 —— 同一行的 span 在 y 上会有一两个点的抖动。
    row_tolerance = max(line_height * 0.5, 1.0)

    page_width = float(getattr(page_rect, "width", 0.0) or 0.0)
    multi_column = detect_multi_column([line for _, line in lines], page_width=page_width)
    #: 这一页正文的左右文字边。对齐方式只能相对它来判断 —— 见 derive_alignment。
    #: 分栏页**不判断对齐**：一栏里的文字相对整页偏右，但在栏内其实是左对齐的，
    #: 与其编一个大概率是错的值，不如留空用 Word 的默认左对齐。
    margins = (
        None if multi_column else text_margins([line for _, line in lines], page_width)
    )
    if multi_column:
        inline_limit = line_height * MULTI_COLUMN_INLINE_FACTOR

    ordered: list[object] = []
    pending = list(floaters)
    runs: list[TextRun] = []
    first_bbox = None
    last_bbox = None
    last_range = None
    last_signature: tuple | None = None

    for block_index, line in lines:
        bbox = _line_bounds(line)

        # 表格和图片按纵向位置插进文字流，而不是统统堆到页尾
        while pending and pending[0][0][1] <= bbox[1]:
            if runs:
                ordered.append(_make_paragraph(runs, first_bbox, body_size, page_rect, margins))
                runs, first_bbox = [], None
            ordered.append(pending.pop(0)[1])

        signature = _line_signature(line)
        same_row = last_bbox is not None and abs(bbox[1] - last_bbox[1]) <= row_tolerance
        # 行间距。同一行的两截文字之间是负的，没有意义，所以只在换行时算。
        gap = None if (last_bbox is None or same_row) else bbox[1] - last_bbox[3]

        if runs:
            if same_row:
                # 同一行、横向却隔着一大段空白 —— 这是左右两栏，不是同一句话。
                # 反过来，间距很小就说明是同一句话被拆成了多个 span（粗体切换、
                # 中英文字体切换），必须接着写而不是另起一段。
                broken = bbox[0] - last_range[1] > inline_limit
            else:
                broken = (
                    signature != last_signature
                    or (gap is not None and gap > gap_limit)
                    # 换行了、但两行的横向范围完全不挨着 —— 这是分栏，不是换行
                    or not _overlaps(last_range, bbox)
                )
            if broken:
                ordered.append(_make_paragraph(runs, first_bbox, body_size, page_rect, margins))
                runs, first_bbox = [], None

        line_runs = [
            span_style(span)
            for span in sorted(line.get("spans") or [], key=_span_x)
            if (span.get("text") or "").strip()
        ]
        if line_runs:
            if runs:
                # 同一行的两截是接着写的，补空格反而会把词切开；
                # 换行处的拼接才需要 _join_text 判断补不补。
                prefix = "" if same_row else _join_text(runs[-1].text, line_runs[0].text)
                if prefix:
                    runs[-1].text += prefix
            for run in line_runs:
                _append_run(runs, run)
            if first_bbox is None:
                first_bbox = bbox

        last_bbox = bbox
        last_range = (bbox[0], bbox[2])
        last_signature = signature

    while pending:
        if runs:
            ordered.append(_make_paragraph(runs, first_bbox, body_size, page_rect, margins))
            runs, first_bbox = [], None
        ordered.append(pending.pop(0)[1])
    if runs:
        ordered.append(_make_paragraph(runs, first_bbox, body_size, page_rect, margins))

    return PageAnalysis(
        PageContent(blocks=ordered, kind=PAGE_KIND_TEXT),
        multi_column=multi_column,
        dropped_tables=dropped,
        image_bytes=image_bytes,
        skipped_images=skipped_images,
    )


def span_style(span: dict) -> TextRun:
    """把 PyMuPDF 的一个 span 转成 :class:`TextRun`。

    粗体斜体只认 ``flags`` 位。**不靠字体名里有没有 Bold 字样去猜**：
    这些位来自字体自身的属性，而合成粗体（画两遍、或者描边）的 PDF
    本来就没有这个信号，猜只会猜错。
    """
    flags = int(span.get("flags") or 0)
    return TextRun(
        text=span.get("text") or "",
        bold=bool(flags & TEXT_FONT_BOLD),
        italic=bool(flags & TEXT_FONT_ITALIC),
        size_pt=_clamp_size(span.get("size")),
        font=span.get("font") or None,
    )


def body_font_size(blocks) -> float:
    """正文字号：按**字符数加权**取众数。

    不取平均值 —— 一份文档里标题、脚注、页眉字号各不相同，
    平均值会落在两个字号之间，谁也不是。
    """
    weights: dict[float, int] = {}
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                count = len((span.get("text") or "").strip())
                if not count:
                    continue
                size = round(_clamp_size(span.get("size")), 1)
                weights[size] = weights.get(size, 0) + count
    if not weights:
        return DEFAULT_BODY_SIZE_PT
    return max(weights.items(), key=lambda item: item[1])[0]


def heading_level(size_pt: float | None, body_size: float) -> int | None:
    """按字号相对正文的倍数判断标题级别；不是标题就返回 None。"""
    if not size_pt or body_size <= 0:
        return None
    ratio = size_pt / body_size
    for threshold, level in HEADING_RATIOS:
        if ratio >= threshold:
            return level
    return None


def derive_alignment(bbox, page_rect, *, margins: tuple[float, float] | None = None) -> str | None:
    """按这一行相对**页面文字区**的位置推断对齐方式。

    这就是「基本位置」在 Word 里唯一能落地的形式：python-docx 做不到把
    文字放到任意坐标（那要手搓文本框 XML），但左 / 中 / 右对齐是段落属性，
    能保下来。返回 None 表示左对齐 —— Word 的默认值，不必显式设置。

    ``margins`` 是这一页正文的左右文字边（由 :func:`text_margins` 量出来）。
    **拿页面边缘当基准是错的**：左对齐的正文本来就顶在左边距上，拿页面边缘
    去比的话，一段「排满整行」的正文左右两边都空着差不多宽，会被判成居中 ——
    而排满整行恰恰是文档里最常见的样子，所以这个错误是成片出现的。

    ``margins`` 传 None 表示**不判断**，一律返回 None。分栏页就是这么用的：
    一栏里的文字相对整页看起来偏右，但它在栏内其实是左对齐的，
    这种页面我们已经如实说明「分栏可能有差异」，不该再编一个对齐方式出来。
    """
    if bbox is None or page_rect is None or margins is None:
        return None
    width = float(getattr(page_rect, "width", 0.0) or 0.0)
    if width <= 0:
        return None

    left_edge, right_edge = margins
    span = right_edge - left_edge
    if span <= 0:
        return None

    # 相对文字区左右边距的缩进量：左对齐是 (0, 余量)，居中两边差不多，右对齐反过来
    left = max(float(bbox[0]) - left_edge, 0.0)
    right = max(right_edge - float(bbox[2]), 0.0)

    tolerance = span * CENTER_TOLERANCE_RATIO
    if left > span * MIN_INDENT_RATIO and right > span * MIN_INDENT_RATIO:
        if abs(left - right) <= tolerance:
            return "center"
    if left > right + tolerance and left > span * MIN_INDENT_RATIO:
        return "right"
    return None


def text_margins(lines, page_width: float) -> tuple[float, float]:
    """量出这一页正文的左右文字边。

    左边的取法是「所有行里最靠左的那个起点」：正文里总有至少一行是顶到
    左边距的，所以最小值就是左边距本身。

    **右边的取法有前提**：同样要有一行排到右边距。正文正常换行、两端对齐、
    或者表格撑满时都成立，所以对常规文档没问题；但一页只有一行居中标题
    （比如封面）时量不到真正的右边距，那一行会被判成右对齐而不是居中。
    单行居中还是单行右对齐在纸面上无法区分，这里接受这个限制，
    不去猜一个更复杂的模型。
    """
    lefts = [float(line["bbox"][0]) for line in lines if line.get("bbox")]
    rights = [float(line["bbox"][2]) for line in lines if line.get("bbox")]
    if not lefts or not rights:
        return (0.0, page_width)
    return (min(lefts), max(rights))


def detect_multi_column(lines, *, page_width: float) -> bool:
    """页面上是不是存在真正的分栏。

    判据：**同一排**里相邻的两段文字之间隔着一条像样的空白，且不止一排这样。

    必须按**行**判断，不能按块判断。实测 PyMuPDF 会把同一行的左右两栏
    放进同一个块里（``LEFT-1`` 和 ``RIGHT-1`` 同属 block0，只是两条 line），
    所以看块永远看不出来 —— 块级 bbox 横跨整页，两栏反而是「挨着」的。

    也必须只比**横向相邻**的两段。一行里的第三段和第一段之间当然隔着
    整个第二段的宽度，拿它们比会把普通的「一行三种字体」误判成双栏。

    ``sort=True`` 会把双栏页面读成「左0 右0 左1 右1」，所以查出分栏时
    结果说明里要如实写一句，不能让用户以为是转坏了。
    """
    gutter = max(page_width, 0.0) * MIN_GUTTER_RATIO
    if gutter <= 0:
        return False

    # 先把纵向重叠的行归成一排
    bands: list[list[tuple[float, float, float, float]]] = []
    for box in sorted((_line_bounds(line) for line in lines), key=lambda item: (item[1], item[0])):
        for band in bands:
            top = min(item[1] for item in band)
            bottom = max(item[3] for item in band)
            shorter = min(bottom - top, box[3] - box[1])
            if shorter > 0 and min(bottom, box[3]) - max(top, box[1]) > shorter * 0.5:
                band.append(box)
                break
        else:
            bands.append([box])

    hits = 0
    for band in bands:
        row = sorted(band, key=lambda item: item[0])
        if any(second[0] - first[2] >= gutter for first, second in zip(row, row[1:])):
            hits += 1
    return hits >= MIN_COLUMN_ROWS


# ----------------------------------------------------------------------
# 内部实现
# ----------------------------------------------------------------------


def _clamp_size(value) -> float:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return DEFAULT_BODY_SIZE_PT
    if not math.isfinite(size) or size <= 0:
        return DEFAULT_BODY_SIZE_PT
    return min(max(size, MIN_FONT_SIZE_PT), MAX_FONT_SIZE_PT)


def _line_bounds(line) -> tuple[float, float, float, float]:
    bbox = line.get("bbox") or (0.0, 0.0, 0.0, 0.0)
    return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))


def _line_signature(line) -> tuple:
    """这一行的样式指纹，用来判断「下一行还是同一段吗」。"""
    spans = [span for span in line.get("spans") or [] if (span.get("text") or "").strip()]
    if not spans:
        return ()
    size = max(_clamp_size(span.get("size")) for span in spans)
    flags = int(spans[0].get("flags") or 0)
    return (
        round(size, 1),
        bool(flags & TEXT_FONT_BOLD),
        bool(flags & TEXT_FONT_ITALIC),
    )


def _join_text(previous: str, following: str) -> str:
    """把两行接起来时中间补什么。

    中文换行处不补空格（补了每个换行都会多一个空格），
    英文换行处必须补，否则 ``FileTools PDF`` 会粘成 ``FileToolsPDF``。
    """
    if not previous or not following:
        return ""
    if _CJK.search(previous[-1]) or _CJK.search(following[0]):
        return ""
    if previous.endswith("-"):
        # 断词连字符，接上就行
        return ""
    return " "


def _append_run(runs: list[TextRun], run: TextRun) -> None:
    """同格式的相邻 run 合并成一个，别让一个段落里塞满碎 run。"""
    if runs:
        last = runs[-1]
        if (
            last.bold == run.bold
            and last.italic == run.italic
            and last.size_pt == run.size_pt
        ):
            last.text += run.text
            return
    runs.append(run)


def _line_height(line) -> float:
    bbox = _line_bounds(line)
    return max(bbox[3] - bbox[1], 0.0)


def _overlaps(first, second) -> bool:
    """两条线的横向范围有没有交集 —— 有交集就是同一栏。"""
    if first is None or second is None:
        return True
    return first[0] < second[1] and second[0] < first[1]


def _span_x(span) -> float:
    bbox = span.get("bbox") or (0.0, 0.0, 0.0, 0.0)
    return float(bbox[0])


def _make_paragraph(runs, first_bbox, body_size: float, page_rect, margins=None) -> Paragraph:
    sizes = [run.size_pt for run in runs if run.size_pt]
    level = heading_level(max(sizes), body_size) if sizes else None
    return Paragraph(
        runs=runs,
        level=level,
        align=derive_alignment(first_bbox, page_rect, margins=margins),
    )


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _inside_any(bbox, boxes) -> bool:
    if not boxes:
        return False
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    for box in boxes:
        if box[0] - 1 <= center_x <= box[2] + 1 and box[1] - 1 <= center_y <= box[3] + 1:
            return True
    return False


def _extract_tables(page, text_blocks) -> tuple[list[tuple[tuple, TableBlock]], int]:
    """识别表格，返回 (按纵坐标排好的 [(bbox, 表格)], 识别到但不敢用的数量)。

    ``find_tables`` 默认策略是 ``lines``：**只认有框线的表格**。
    无框线的表格识别不到，那种情况会退回按文字顺序输出 —— 这是刻意的取舍，
    生成一个错位的表格比不生成表格糟糕得多。
    """
    if not text_blocks:
        return [], 0
    try:
        if len(page.get_drawings()) < MIN_TABLE_DRAWINGS:
            return [], 0
    except Exception:  # noqa: BLE001 - 取不到矢量图就当没有表格
        return [], 0

    try:
        finder = page.find_tables()
    except Exception:  # noqa: BLE001 - 表格识别失败不影响正文抽取
        logger.warning("第 %s 页表格识别失败", page.number + 1, exc_info=True)
        return [], 0

    found: list[tuple[tuple, TableBlock]] = []
    dropped = 0
    for table in getattr(finder, "tables", None) or []:
        rows = _clean_rows(table)
        if rows is None:
            dropped += 1
            continue
        bbox = getattr(table, "bbox", None)
        if bbox is None:
            dropped += 1
            continue
        found.append(((float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])), TableBlock(rows=rows)))
    found.sort(key=lambda item: item[0][1])
    return found, dropped


def _clean_rows(table) -> list[list[str]] | None:
    """把识别结果收拾成能直接写进 Word 的二维字符串表。

    任何一种「不干净」都返回 None，让调用方退回按文字输出：

    - 有单元格是 ``None``（合并单元格会这样，实测 ``extract()`` 会把 None
      塞在行列表**里面**，不是整行 None）；
    - 行宽不一致；
    - 行数或列数退化；
    - 整列为空（``find_tables`` 偶尔会多切出一列空的）。
    """
    try:
        grid = table.extract()
    except Exception:  # noqa: BLE001
        return None
    if not grid:
        return None

    rows: list[list[str]] = []
    for raw_row in grid:
        if raw_row is None:
            return None
        cells = []
        for cell in raw_row:
            if cell is None:
                return None
            cells.append(" ".join(str(cell).split()))
        rows.append(cells)

    if len(rows) < MIN_TABLE_ROWS:
        return None
    widths = {len(row) for row in rows}
    if len(widths) != 1:
        return None

    rows = _drop_empty_columns(rows)
    if len(rows) < MIN_TABLE_ROWS or len(rows[0]) < MIN_TABLE_COLS:
        return None
    return rows


def _drop_empty_columns(rows: list[list[str]]) -> list[list[str]]:
    """删掉每一行都是空的那种整列。"""
    if not rows:
        return rows
    keep = [
        index
        for index in range(len(rows[0]))
        if any(row[index].strip() for row in rows)
    ]
    if not keep:
        return rows
    return [[row[index] for index in keep] for row in rows]
