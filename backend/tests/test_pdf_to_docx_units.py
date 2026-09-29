"""PDF → Word 的纯函数单元测试（第六阶段 A）。

这一层**不经过 HTTP、也不碰 OCR 栈**：喂进去的是 PDF 页对象和 IR，
拿出来的是判定结果和 DOCX 字节。这样「逐页判定的阈值对不对」
「行内混排会不会被断成两段」这类问题能单独钉住，
不用先起一个服务、传一个文件、再从一个 zip 里把答案刨出来。

页对象的构造统一走 ``tests.conftest`` 里那几个 ``build_*_pdf`` ——
它们生成的是真 PDF（真字体、真排版），只是不落盘。
"""

from __future__ import annotations

import io
import zipfile

import pymupdf
import pytest

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
from office.docx_writer import build_docx
from pdf.analyzer import analyze_text_page, detect_page_kind
from services.ocr_service import order_boxes
from services.progress import ProgressStore, valid_progress_id
from tests.conftest import (
    build_wrapped_pdf,
    build_inline_pdf,
    build_mixed_pdf,
    build_scanned_pdf,
    build_text_pdf,
)


def open_pages(data: bytes) -> list:
    """把一份 PDF 字节解成页对象列表（调用方负责关掉 doc）。"""
    doc = pymupdf.open(stream=data, filetype="pdf")
    return [doc[index] for index in range(doc.page_count)]


# ----------------------------------------------------------------------
# 逐页判定：文字页还是扫描页
# ----------------------------------------------------------------------


def test_text_page_is_detected_as_text() -> None:
    """有文字层、字数够的页面判成文字页。"""
    pages = open_pages(build_text_pdf(["第一章 总则", "本合同用于测试逐页判定。"]))
    assert detect_page_kind(pages[0]) == PAGE_KIND_TEXT


def test_scanned_page_is_detected_as_scan() -> None:
    """整页就是一张图、没有文字层，必须判成扫描页。

    这一条同时钉住「扫描件样张真的没有文字层」——
    样张如果偷偷带了一层文字，后面所有 OCR 用例测的都是别的东西。
    """
    pages = open_pages(build_scanned_pdf(["扫描页一"]))
    assert pages[0].get_text().strip() == "", "样张不该带文字层"
    assert detect_page_kind(pages[0]) == PAGE_KIND_SCAN


def test_a_bare_page_number_is_not_enough_to_count_as_text() -> None:
    """只认出页码的残缺文字层不算「有文字」。

    扫描件经常带一层由别的 OCR 软件塞进去的、只认出页眉页码的残字。
    阈值是 0 的话这些页会被当成文字页直接抽，抽出来只有个「1」，
    用户拿到手就是一份几乎空白的 Word —— 比老老实实去 OCR 还差。
    """
    pages = open_pages(build_text_pdf(["1"], filler=""))
    assert len(pages[0].get_text().strip()) < settings.PDF_TO_WORD_MIN_TEXT_CHARS
    assert detect_page_kind(pages[0]) == PAGE_KIND_SCAN


def test_a_page_with_no_text_layer_at_all_is_a_scan() -> None:
    """一个字都没有的页面当然也是扫描页，而不是「空文字页」。"""
    pages = open_pages(build_text_pdf([""], filler=""))
    assert detect_page_kind(pages[0]) == PAGE_KIND_SCAN


def test_the_threshold_is_honoured_exactly() -> None:
    """阈值是「大于等于」，边界上不能含糊。

    差一个字就从文字页变成扫描页，用户看到的结果天差地别
    （一边是原文，一边是要等 OCR），所以边界必须钉死。
    """
    limit = settings.PDF_TO_WORD_MIN_TEXT_CHARS
    pages = open_pages(build_text_pdf(["x" * (limit - 1)], filler=""))
    assert detect_page_kind(pages[0]) == PAGE_KIND_SCAN

    pages = open_pages(build_text_pdf(["x" * limit], filler=""))
    assert detect_page_kind(pages[0]) == PAGE_KIND_TEXT


def test_the_threshold_can_be_overridden_per_call() -> None:
    """``min_chars`` 是给测试和调参用的，传了就得按传的算。"""
    pages = open_pages(build_text_pdf(["只有六个字"], filler=""))
    assert detect_page_kind(pages[0], min_chars=1) == PAGE_KIND_TEXT
    assert detect_page_kind(pages[0], min_chars=1000) == PAGE_KIND_SCAN


def test_the_decision_is_made_page_by_page() -> None:
    """**这是逐页判定的核心用例。**

    封面是图、正文是字的 PDF 非常常见。整份判成扫描件的话，
    几十页本来清清楚楚的正文也要被送去 OCR —— 慢、还更不准。
    """
    pages = open_pages(
        build_mixed_pdf(
            [
                ("text", ["第一章 总则", "本合同用于测试逐页判定。"]),
                ("scan", "盖章页"),
                ("text", ["第二章 细则", "继续测试后面的文字页。"]),
            ]
        )
    )
    assert [detect_page_kind(page) for page in pages] == [
        PAGE_KIND_TEXT,
        PAGE_KIND_SCAN,
        PAGE_KIND_TEXT,
    ]


def test_a_page_whose_text_layer_cannot_be_read_is_treated_as_a_scan() -> None:
    """读文字层抛异常时按扫描页处理，而不是让整份文档失败。

    坏页面在真实 PDF 里不罕见；单页读不出来就整份报错，
    用户拿不到任何东西。当扫描页处理至少还有 OCR 这条路可走。
    """

    class BrokenPage:
        number = 0

        def get_text(self, *args, **kwargs):
            raise RuntimeError("页面内容流损坏")

    assert detect_page_kind(BrokenPage()) == PAGE_KIND_SCAN


# ----------------------------------------------------------------------
# 抽取文字层
# ----------------------------------------------------------------------


def test_chinese_lines_stay_separate_paragraphs() -> None:
    """中文一行一段：行距正常，不该被合并成一个段落。"""
    pages = open_pages(
        build_text_pdf(["采购合同", "甲方：北京示例科技有限公司", "乙方：上海样例贸易有限公司"])
    )
    analysis = analyze_text_page(pages[0])
    texts = [block.text for block in analysis.content.blocks if isinstance(block, Paragraph)]

    assert texts == [
        "采购合同",
        "甲方：北京示例科技有限公司",
        "乙方：上海样例贸易有限公司",
        "本页正文内容用于测试，长度足以让这一页被判定为文字页。",
    ]


def test_english_words_are_not_run_together() -> None:
    """英文单词之间的空格要保住 —— 这是拉丁字体和 CJK 字体最大的差别。"""
    from tests.conftest import build_english_pdf

    pages = open_pages(build_english_pdf(["Annual Summary", "Revenue grew by 18 percent."]))
    analysis = analyze_text_page(pages[0])
    texts = [block.text for block in analysis.content.blocks if isinstance(block, Paragraph)]

    assert "Revenue grew by 18 percent." in texts


def test_inline_styles_stay_in_one_paragraph() -> None:
    """同一行里普通 / 粗体 / 斜体混排，必须留在**一个**段落里。

    字距是用真实字宽算出来的，所以被判成三段只能是分段逻辑的错。
    粗体斜体丢了用户要逐句重排，比排版差异严重得多。
    """
    pages = open_pages(
        build_inline_pdf(
            [
                ("Revenue grew ", "helv"),
                ("18 percent", "hebo"),
                (" while costs stayed ", "helv"),
                ("flat", "heit"),
                (".", "helv"),
            ]
        )
    )
    analysis = analyze_text_page(pages[0])
    paragraphs = [block for block in analysis.content.blocks if isinstance(block, Paragraph)]

    assert len(paragraphs) == 1, f"一行被断成了 {len(paragraphs)} 段"
    paragraph = paragraphs[0]
    assert paragraph.text == "Revenue grew 18 percent while costs stayed flat."
    assert [(run.text, run.bold, run.italic) for run in paragraph.runs] == [
        ("Revenue grew ", False, False),
        ("18 percent", True, False),
        (" while costs stayed ", False, False),
        ("flat", False, True),
        (".", False, False),
    ]


def test_two_columns_are_not_glued_into_one_line() -> None:
    """分栏页面里左右两栏是两段，不能被拼成「左栏右栏」。

    这一条曾经真的坏过：PyMuPDF 把同一行的两截文字放进**同一个 block**
    的不同 line 里，只在 block 这一层做判断是怎么也分不开的。
    """
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        for index, text in enumerate(["LEFT-1", "LEFT-2", "LEFT-3"]):
            page.insert_text((72, 100 + index * 20), text, fontsize=12)
        for index, text in enumerate(["RIGHT-1", "RIGHT-2", "RIGHT-3"]):
            page.insert_text((340, 100 + index * 20), text, fontsize=12)
        analysis = analyze_text_page(page)
    finally:
        doc.close()

    texts = [block.text for block in analysis.content.blocks if isinstance(block, Paragraph)]
    assert analysis.multi_column is True
    assert "LEFT-1" in texts and "RIGHT-1" in texts
    assert not any("LEFT-1" in text and "RIGHT-1" in text for text in texts), (
        f"左右两栏被拼成了一行：{texts}"
    )


def test_a_wrapped_paragraph_stays_one_paragraph() -> None:
    """**自动换行的正文必须是一段，不能每行一段。**

    这是 :func:`test_chinese_lines_stay_separate_paragraphs` 的另一半：
    行距紧凑、每行排到右边界的连续文字是一段正文；排成一行一段的话，
    用户打开 Word 看到的是一首诗，连重新排版都做不到。

    真实正文就是这个形态，所以这一条比「多段」那一条更常被走到。
    """
    paragraph = (
        "The revenue grew by 18 percent compared with the same period last year, "
        "driven mainly by strong demand in the enterprise segment. Operating costs "
        "remained flat across all three regions, so the margin improved."
    )
    pages = open_pages(build_wrapped_pdf(paragraph))
    analysis = analyze_text_page(pages[0])
    paragraphs = [block for block in analysis.content.blocks if isinstance(block, Paragraph)]

    assert len(paragraphs) == 1, f"一段正文被切成了 {len(paragraphs)} 段"
    assert paragraphs[0].text == paragraph


def test_lines_with_visible_extra_space_become_separate_paragraphs() -> None:
    """行与行之间留着明显空白时，每一行自成一段。

    合同条款、清单条目就是这个形态 —— 和上一条放在一起看，
    才说明分段靠的是行距这个信号，而不是「中文就分行、英文就合并」。
    """
    pages = open_pages(
        build_text_pdf(["第一条 标的为办公设备一批。", "第二条 交付时间为三十个工作日。"])
    )
    analysis = analyze_text_page(pages[0])
    texts = [block.text for block in analysis.content.blocks if isinstance(block, Paragraph)]

    assert texts[0] == "第一条 标的为办公设备一批。"
    assert texts[1] == "第二条 交付时间为三十个工作日。"


def test_left_aligned_text_is_not_called_centred() -> None:
    """左对齐的正文不许被判成居中。

    这一条真出过问题：判断对齐时拿**页面边缘**当基准，于是一段
    「最长的那行没排满」的左对齐正文，左右两边都空着一大截，
    看起来就像居中了 —— 合同条款几乎条条如此，整篇会变成居中的。

    基准必须是这一页正文自己的左右文字边（顶到边距的那一行）。
    """
    pages = open_pages(
        build_text_pdf(["采购合同", "第一条 本合同的标的为办公设备一批。"])
    )
    analysis = analyze_text_page(pages[0])

    aligns = {
        block.text: block.align
        for block in analysis.content.blocks
        if isinstance(block, Paragraph)
    }
    assert aligns["第一条 本合同的标的为办公设备一批。"] is None, "左对齐被判成了居中"


def test_a_left_aligned_english_paragraph_is_not_called_centred() -> None:
    """英文同理 —— 这一条是按页面边缘判断时**最先坏掉**的形态。"""
    from tests.conftest import build_english_pdf

    pages = open_pages(
        build_english_pdf(
            [
                "Annual Summary",
                "The revenue grew by 18 percent compared with the same period.",
                "Operating costs remained flat across all three regions.",
            ]
        )
    )
    analysis = analyze_text_page(pages[0])

    for block in analysis.content.blocks:
        if isinstance(block, Paragraph) and block.text.startswith("The revenue"):
            assert block.align is None, "左对齐被判成了居中或右对齐"


def test_text_centred_between_the_margins_is_still_detected() -> None:
    """真的居中要认得出来 —— 修误判不能把功能一起修没。

    样张是真实文档的样子：正文撑满文字区（左右边距因此量得准），
    标题排在同一个文字区的正中间。标题该判成居中，正文该判成左对齐。

    正文必须撑满 —— 文字区的右边距是「最靠右那一行的终点」量出来的，
    一页只有一行居中标题的话量不到右边距，居中会被读成右对齐
    （见 :func:`pdf.analyzer.text_margins` 的说明）。
    """
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        # 正文用 insert_textbox 自动换行，行会排到右边距 523
        page.insert_textbox(
            pymupdf.Rect(72, 90, 523, 200),
            "正文顶在左边距上并一直排到右边距，这一段的作用是把这一页的"
            "文字区左右边界量出来。正文本身是左对齐的，不该被判成居中。",
            fontsize=12,
            fontname="china-s",
        )
        title = "居中标题"
        width = pymupdf.get_text_length(title, fontname="china-s", fontsize=20)
        page.insert_text(((72 + 523 - width) / 2, 240), title, fontname="china-s", fontsize=20)
        analysis = analyze_text_page(page)
    finally:
        doc.close()

    aligns = {
        block.text: block.align
        for block in analysis.content.blocks
        if isinstance(block, Paragraph)
    }
    assert aligns["居中标题"] == "center"
    body = next(text for text in aligns if text.startswith("正文顶在左边距上"))
    assert aligns[body] is None, "左对齐的正文被判成了居中"


def test_a_single_line_page_is_still_a_text_page() -> None:
    """只有一行字也照样抽得出来（判定归判定，抽取归抽取）。"""
    pages = open_pages(build_text_pdf(["只有一行"], filler=""))
    analysis = analyze_text_page(pages[0])
    assert analysis.content.kind == PAGE_KIND_TEXT
    assert "只有一行" in analysis.content.text


# ----------------------------------------------------------------------
# OCR 识别框的阅读顺序（喂合成框，不依赖 OCR 栈）
# ----------------------------------------------------------------------


def box(left: float, top: float, right: float, bottom: float, text: str, confidence=0.9):
    """造一个 rapidocr 形状的识别框：``[四点坐标, 文字, 置信度]``。"""
    return [
        [[left, top], [right, top], [right, bottom], [left, bottom]],
        text,
        confidence,
    ]


def test_boxes_come_back_in_reading_order() -> None:
    """引擎返回的顺序不是阅读顺序，必须自己排。

    这一条是拿真实引擎的输出钉下来的：天真按返回顺序拼会得到
    ``FileTPDFoolstoWord``、``1234. 556`` 这种结果 —— 字都认对了，顺序是乱的。
    """
    boxes = [
        box(300, 100, 460, 140, "to"),
        box(72, 100, 285, 140, "FileTools PDF"),
        box(72, 200, 300, 240, "Word"),
    ]
    lines = order_boxes(boxes)
    assert [line.text for line in lines] == ["FileTools PDF to", "Word"]


def test_boxes_on_slightly_different_heights_are_one_line() -> None:
    """同一行的框在垂直方向上有几像素抖动，不能被拆成两行。

    排序的容差取「平均框高的一半」就是为了这个：按 y 严格排序的话，
    同一行会被拆开，读出来就是一句一行地错位。
    """
    boxes = [
        box(72, 100, 200, 140, "前面"),
        box(200, 104, 320, 146, "后面"),
    ]
    lines = order_boxes(boxes)
    assert len(lines) == 1
    assert lines[0].text == "前面后面"


def test_a_gap_between_boxes_becomes_a_space() -> None:
    """框之间空出半个字高就补空格，否则 ``OCR 2026`` 会粘成 ``OCR2026``。"""
    boxes = [
        box(72, 100, 300, 145, "扫描件识别测试"),
        box(350, 100, 480, 145, "OCR"),
        box(540, 100, 640, 145, "2026"),
    ]
    lines = order_boxes(boxes)
    assert lines[0].text == "扫描件识别测试 OCR 2026"


def test_touching_boxes_are_not_split_by_a_space() -> None:
    """紧挨着的框直接拼，不能在中文词中间插空格。"""
    boxes = [
        box(72, 100, 200, 145, "采购"),
        box(201, 100, 320, 145, "合同"),
    ]
    assert order_boxes(boxes)[0].text == "采购合同"


def test_blank_boxes_are_dropped() -> None:
    """引擎偶尔会给出空文字或纯空白的框，不能让它撑出一行空段落。"""
    boxes = [box(72, 100, 200, 140, "有效文字"), box(72, 200, 200, 240, "   ")]
    lines = order_boxes(boxes)
    assert [line.text for line in lines] == ["有效文字"]


def test_no_boxes_yields_no_lines() -> None:
    """空白页认不出东西是**事实**，不是错误。"""
    assert order_boxes([]) == []
    assert order_boxes(None) == []


def test_a_missing_confidence_does_not_break_a_line() -> None:
    """置信度缺失时按 0 处理，不能让整行丢掉。"""
    boxes = [
        [[[72, 100], [300, 100], [300, 140], [72, 140]], "文字", None],
    ]
    lines = order_boxes(boxes)
    assert lines[0].text == "文字"
    assert lines[0].confidence == 0.0


def test_line_confidence_is_the_average() -> None:
    """一行多个框时取平均，用来如实提示「这几页识别得不太有把握」。"""
    boxes = [
        box(72, 100, 200, 140, "AAA", 0.8),
        box(240, 100, 380, 140, "BBB", 0.6),
    ]
    assert order_boxes(boxes)[0].confidence == pytest.approx(0.7)


# ----------------------------------------------------------------------
# 写 DOCX
# ----------------------------------------------------------------------


def page_with(*blocks) -> PageContent:
    return PageContent(blocks=list(blocks), kind=PAGE_KIND_TEXT)


def document_xml(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        return archive.read("word/document.xml").decode("utf-8")


def test_page_count_determines_the_number_of_page_breaks() -> None:
    """N 页之间正好 N-1 个分页符。

    多一个会多出空白页，少一个会把两页并成一页 ——
    这是 DOCX 里唯一能表达页序的手段，数量必须精确。
    """
    for count in (1, 2, 5):
        pages = [page_with(Paragraph(runs=[TextRun(text=f"第{index}页")])) for index in range(count)]
        data = build_docx(pages, east_asian_font="宋体").data
        assert document_xml(data).count('<w:br w:type="page"/>') == count - 1


def test_a_document_with_no_pages_is_refused() -> None:
    """一页都没有就报错，而不是生成一份空文档糊弄过去。"""
    from utils.errors import DocxGenerationError

    with pytest.raises(DocxGenerationError):
        build_docx([], east_asian_font="宋体")


def test_headings_and_body_use_different_styles() -> None:
    """标题落成 Heading N，正文落成 Normal。"""
    from tests.conftest import docx_runs

    data = build_docx(
        [
            page_with(
                Paragraph(runs=[TextRun(text="标题", size_pt=20.0)], level=1),
                Paragraph(runs=[TextRun(text="正文", size_pt=12.0)]),
            )
        ],
        east_asian_font="宋体",
    ).data

    styles = {item[0]: item[1] for item in docx_runs(data)}
    assert styles["标题"] == "Heading 1"
    assert styles["正文"] == "Normal"


def test_heading_colour_is_forced_to_black() -> None:
    """标题要显式设成黑色。

    Word 默认的标题样式是主题蓝，直接沿用会让转出来的文档
    到处是蓝标题 —— 这和原件不一样，用户第一眼就会看出来。
    """
    data = build_docx(
        [page_with(Paragraph(runs=[TextRun(text="标题")], level=1))],
        east_asian_font="宋体",
    ).data
    assert "000000" in document_xml(data)


def test_every_run_carries_an_east_asian_font() -> None:
    """每个 run 都要带 w:eastAsia，否则中文会掉进阅读器的兜底字体。"""
    data = build_docx(
        [page_with(Paragraph(runs=[TextRun(text="中文内容")]))], east_asian_font="宋体"
    ).data
    assert "w:eastAsia" in document_xml(data)


def test_images_keep_the_size_the_caller_asked_for() -> None:
    """图片尺寸按调用方给的磅值写，不按图片自带的 DPI。

    一张 1653×2339 的扫描图自带 200 DPI，python-docx 会把它铺成
    宽 21 厘米还多 —— 页面上根本放不下。IR 里的宽高必须被尊重。
    """
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (1653, 2339), "white").save(stream, format="PNG")

    data = build_docx(
        [
            page_with(
                ImageBlock(data=stream.getvalue(), width_pt=451.0, height_pt=638.0),
                Paragraph(runs=[TextRun(text="图下面还有字")]),
            )
        ],
        east_asian_font="宋体",
    ).data

    from docx import Document
    from docx.shared import Pt

    document = Document(io.BytesIO(data))
    image = document.inline_shapes[0]
    assert image.width == Pt(451.0)
    assert image.height == Pt(638.0)


def test_an_image_that_cannot_be_converted_is_skipped_not_fatal() -> None:
    """转不了格式的图跳过并记一笔，绝不让整次转换失败。

    PDF 里抽出来的图常是 jpx / ccitt / jbig2，python-docx 不认。
    为了一张图让用户拿不到整份 Word，代价完全不划算。
    """
    build = build_docx(
        [
            page_with(
                ImageBlock(data=b"not an image at all", width_pt=100.0, height_pt=80.0),
                Paragraph(runs=[TextRun(text="文字还在")]),
            )
        ],
        east_asian_font="宋体",
    )

    assert build.skipped_images == 1
    assert build.total_images == 1
    from tests.conftest import docx_runs

    assert any("文字还在" in item[0] for item in docx_runs(build.data))


def test_a_table_becomes_a_real_word_table() -> None:
    """识别出来的表格要落成真正的 Word 表格，而不是一堆文字。"""
    from docx import Document

    data = build_docx(
        [
            page_with(
                TableBlock(rows=[["序号", "名称"], ["1", "办公桌"], ["2", "办公椅"]]),
                Paragraph(runs=[TextRun(text="表格下面的文字")]),
            )
        ],
        east_asian_font="宋体",
    ).data

    document = Document(io.BytesIO(data))
    assert len(document.tables) == 1
    table = document.tables[0]
    assert len(table.rows) == 3
    assert [cell.text for cell in table.rows[0].cells] == ["序号", "名称"]


def test_a_broken_table_does_not_take_the_document_with_it() -> None:
    """空表格或畸形表格只是不写，不该让整份文档生成失败。"""
    from tests.conftest import docx_runs

    build = build_docx(
        [page_with(TableBlock(rows=[]), Paragraph(runs=[TextRun(text="后面的文字")]))],
        east_asian_font="宋体",
    )
    assert any("后面的文字" in item[0] for item in docx_runs(build.data))


# ----------------------------------------------------------------------
# 进度表
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["abcdefgh", "a" * 64, "progress-01", "A_b-C9x_yz", "0123456789"]
)
def test_valid_progress_ids_are_accepted(value: str) -> None:
    assert valid_progress_id(value) is True


@pytest.mark.parametrize(
    "value",
    ["", "short", "a" * 65, "有中文的标识", "with space", "dots.not.allowed", "sl/ash", None],
)
def test_malformed_progress_ids_are_refused(value) -> None:
    """id 会直接出现在 URL 里，是不受信任的输入，形状必须卡死。"""
    assert valid_progress_id(value) is False


def test_progress_entries_expire() -> None:
    """过期的条目要能被清掉，否则进程活多久就漏多久。"""
    store = ProgressStore(ttl=0)
    store.start("expiring-entry-01", page_count=3)
    assert store.purge_expired() == 1
    assert store.get("expiring-entry-01") is None


def test_progress_entries_are_capped() -> None:
    """条目数有上限：一直没人来读也不能把内存吃光。"""
    store = ProgressStore(ttl=600, max_entries=4)
    for index in range(10):
        store.start(f"entry-{index:04d}", page_count=1)
    assert len(store) <= 4
    assert store.get("entry-0009") is not None, "最新的那条应该还在"


def test_progress_entries_hold_no_user_content() -> None:
    """进度表里只有阶段和页码 —— 猜中 id 也看不出这是谁的文件。"""
    store = ProgressStore(ttl=600)
    store.start("privacy-check-01", page_count=7)
    store.update("privacy-check-01", "ocr", page=2)

    assert store.get("privacy-check-01") == {
        "stage": "ocr",
        "page": 2,
        "page_count": 7,
    }


def test_finishing_removes_the_entry() -> None:
    """转换结束后条目立刻消失，前端不用再多轮询一次。"""
    store = ProgressStore(ttl=600)
    store.start("finishing-entry-01", page_count=1)
    store.finish("finishing-entry-01")
    assert store.get("finishing-entry-01") is None


def test_updating_an_unknown_entry_is_harmless() -> None:
    """没登记过就直接更新（比如转换已经结束）不该抛异常。"""
    store = ProgressStore(ttl=600)
    store.update("never-registered-01", "ocr", page=1)
    store.finish("never-registered-01")
    assert store.get("never-registered-01") is None
