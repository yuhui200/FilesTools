"""PDF → Word 接口的测试（第六阶段 A）。

重点是**产物本身**，而不是接口返回 200：

* DOCX 必须真的能打开 —— 分三层验：只用标准库解包（能抓到
  「python-docx 读得回来、Word 读不回来」这类故障）、按分页符验页序、
  再用 python-docx 回读验样式；
* 页序只能靠**分页符**验，DOCX 里没有「页」这个东西；
* 出错响应里不能出现堆栈、服务器路径、临时目录名、OCR 命令（§十三）。
"""

from __future__ import annotations

import io
import re
import tempfile
import zipfile
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

from config import settings
from services import ocr_service
from services.pdf_to_docx import docx_available
from services.progress import progress_store
from utils.errors import ErrorCode
from tests.conftest import (
    build_empty_pdf,
    build_english_pdf,
    build_inline_pdf,
    build_mixed_pdf,
    build_scanned_pdf,
    build_text_pdf,
    collapse,
    docx_media,
    docx_pages,
    docx_runs,
    download_docx,
    normalize_ocr_text,
    pdf_lines,
    pdf_to_word_files,
    requires_ocr,
    squash,
)

PDF_TO_WORD = "/api/office/pdf-to-word"
PROGRESS = "/api/office/pdf-to-word/progress"
WORD_TO_PDF = "/api/office/word-to-pdf"

#: 出错时绝不能出现在响应里的东西（比第五阶段多三个 OCR / 组件相关的）
FORBIDDEN_FRAGMENTS = (
    "Traceback",
    "File \"",
    "\\",
    "/tmp/",
    "AppData",
    "site-packages",
    "filetools_",
    "rapidocr",
    "onnxruntime",
    "soffice",
)

#: 本机没有 python-docx 时这个文件里几乎全是必然失败，统一跳过
requires_docx = pytest.mark.skipif(
    not docx_available(), reason="本机没有 python-docx，跳过 PDF → Word 测试"
)


def assert_clean_error(response, *, status: int, code: str, contains: str = "") -> dict:
    """统一的错误响应断言：状态码、错误码，以及不泄露内部细节。"""
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    if contains:
        assert contains in error["message"]

    raw = response.text
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in raw, f"错误响应里泄露了 {fragment!r}：{raw[:300]}"
    return error


def assert_docx_structure(data: bytes) -> str:
    """第一层：只用标准库确认这是一份完好的 DOCX，返回 document.xml。

    刻意不 import python-docx：用同一个库去验它自己写出来的文件，
    等于让被告当法官。
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None, "DOCX 压缩包本身是坏的"
        names = set(archive.namelist())
        for part in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
            assert part in names, f"缺少必需条目 {part}"
        return archive.read("word/document.xml").decode("utf-8")


def temp_dirs() -> set[Path]:
    """系统临时目录下本服务的临时目录。"""
    root = Path(settings.TEMP_ROOT or tempfile.gettempdir())
    if not root.is_dir():
        return set()
    return {item for item in root.iterdir() if item.name.startswith("filetools_")}


# ----------------------------------------------------------------------
# 普通文字 PDF → DOCX
# ----------------------------------------------------------------------


@requires_docx
def test_converts_text_pdf_to_openable_docx(client: TestClient) -> None:
    """一份普通文字 PDF 能转成真的打得开的 Word。"""
    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files(
            "报告.pdf", build_text_pdf(["季度报告", "第一段正文内容。"])
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "报告.docx"
    assert payload["media_type"].endswith("wordprocessingml.document")
    assert payload["archived"] is False
    assert payload["size"] > 0

    data = download_docx(client, payload)

    # 第一层：标准库能解开
    assert_docx_structure(data)
    # 第三层：python-docx 能回读，而且文字真的在里面
    text = "".join(item[0] for item in docx_runs(data))
    for expected in ("季度报告", "第一段正文内容。"):
        assert expected in text


@requires_docx
def test_result_reports_how_the_text_was_obtained(client: TestClient) -> None:
    """结果页要显示的信息：文件类型、页数、识别方式。"""
    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files("报告.pdf", build_text_pdf(["标题"], ["第二页标题"])),
    )
    payload = response.json()

    assert payload["document_kind"] == "text"
    assert payload["document_kind_label"] == "文字 PDF"
    assert payload["extraction_method"] == "text"
    assert payload["extraction_label"] == "直接提取文字层"
    assert payload["page_count"] == 2
    assert payload["original_pages"] == 2
    assert payload["text_pages"] == 2
    assert payload["ocr_pages"] == 0
    assert payload["page_kinds"] == ["text", "text"]

    # DOCX 通常比源 PDF 大，「省了多少」在这里没有意义 ——
    # 与其在结果页显示一个负数，不如不显示
    assert payload["saved_bytes"] is None
    assert payload["saved_percent"] is None


@requires_docx
def test_notes_tell_the_user_what_may_differ(client: TestClient) -> None:
    """字体这类做不到的事要提前说清楚，而不是等用户自己发现。"""
    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("报告.pdf", build_text_pdf(["标题"]))
    )
    notes = " ".join(response.json()["notes"])

    assert "字体" in notes


@requires_docx
def test_page_order_follows_the_source_pdf(client: TestClient) -> None:
    """多页 PDF 的页序：用分页符切成段，第 N 段必须是第 N 页的内容。"""
    labels = [f"PAGE{i}" for i in range(1, 5)]
    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files(
            "多页.pdf", build_text_pdf(*[[label] for label in labels])
        ),
    )
    payload = response.json()
    assert payload["page_count"] == 4

    segments = docx_pages(download_docx(client, payload))

    # 段数 == 页数，这一条同时钉住「分页符数量」和「页序」
    assert len(segments) == 4
    for index, label in enumerate(labels):
        assert label in segments[index], f"第 {index + 1} 页的内容跑到别处去了：{segments}"
        for other in labels:
            if other != label:
                assert other not in segments[index]


@requires_docx
def test_single_page_has_no_page_break(client: TestClient) -> None:
    """一页的文档不该有分页符 —— 否则 Word 里会多出一张空白页。"""
    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("单页.pdf", build_text_pdf(["只有一页"]))
    )
    xml = assert_docx_structure(download_docx(client, response.json()))
    assert 'w:type="page"' not in xml


@requires_docx
def test_preserves_bold_italic_and_font_size(client: TestClient) -> None:
    """字号和标题级别要真的落到 DOCX 里，而不是只有文字对。"""
    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("样式.pdf", build_text_pdf(["样式测试"]))
    )
    paragraphs = docx_runs(download_docx(client, response.json()))

    title = next(item for item in paragraphs if item[0] == "样式测试")
    assert title[1] == "Heading 1", "20 磅的标题应该落成一级标题"
    assert title[2][0][3] == pytest.approx(20.0), "字号要按原件还原"

    body = next(item for item in paragraphs if item[0].startswith("本页正文内容"))
    assert body[1] == "Normal"
    assert body[2][0][3] == pytest.approx(12.0)


@requires_docx
def test_docx_is_written_with_an_east_asian_font(client: TestClient) -> None:
    """中文必须带 w:eastAsia。

    只设 ``font.name`` 落在 ``w:ascii`` 上，管不到中文；缺了 eastAsia，
    中文会掉进阅读器的兜底字体，同一份文件在不同电脑上长得不一样。
    """
    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("中文.pdf", build_text_pdf(["采购合同"]))
    )
    data = download_docx(client, response.json())
    assert "w:eastAsia" in assert_docx_structure(data)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        styles = archive.read("word/styles.xml").decode("utf-8")
    assert "w:eastAsia" in styles, "Normal 样式也要设，否则用户新输入的正文会掉字体"


# ----------------------------------------------------------------------
# 中文 PDF / 英文 PDF / 中英混合 PDF
# ----------------------------------------------------------------------

#: 一份像真合同的中文样张。刻意混进全角标点、书名号、拉丁缩写和数字 ——
#: 这些是「字都在不在」最容易出问题的地方。
CHINESE_PAGE = [
    "采购合同",
    "甲方：北京示例科技有限公司",
    "乙方：上海样例贸易有限公司",
    "第一条 本合同的标的为办公设备一批，具体型号见附件一。",
    "第二条 交付时间为合同生效之日起三十个工作日内。",
    "第三条 验收标准依照国家标准 GB/T 12345-2020 执行。",
    "第四条 未尽事宜，双方另行协商解决。",
]


@requires_docx
def test_chinese_pdf_keeps_every_character(client: TestClient) -> None:
    """中文 PDF：源文件里的**每一个字**都要出现在 Word 里。

    不是抽查几个词，而是去掉空白后整篇做子串比对 —— 少一个字就红。
    中文 PDF 的坑不在编码（PyMuPDF 给的是 str），而在分段逻辑会不会
    把某一行吃掉，所以这里必须整篇比。
    """
    source = build_text_pdf(CHINESE_PAGE)
    response = client.post(PDF_TO_WORD, files=pdf_to_word_files("采购合同.pdf", source))
    assert response.status_code == 200

    text = squash("".join(item[0] for item in docx_runs(download_docx(client, response.json()))))

    for line in pdf_lines(source):
        assert squash(line) in text, f"源文件里的这一行在 Word 里找不到：{line!r}"


@requires_docx
def test_chinese_pdf_keeps_full_width_punctuation(client: TestClient) -> None:
    """全角标点要原样保留，不能被折成半角。

    标点看似小事，但合同、公文里一个「，」变「,」是要返工的；
    这一条钉住「我们没做任何标点归一化」。
    """
    source = build_text_pdf(CHINESE_PAGE)
    response = client.post(PDF_TO_WORD, files=pdf_to_word_files("采购合同.pdf", source))
    text = squash("".join(item[0] for item in docx_runs(download_docx(client, response.json()))))

    for mark in ("：", "，", "。", "/"):
        assert mark in text, f"全角标点 {mark!r} 没有保留下来"
    assert "，" in text and "," not in text, "全角逗号被折成了半角"


@requires_docx
def test_chinese_pdf_headings_and_body_keep_their_sizes(client: TestClient) -> None:
    """中文标题落成一级标题，正文落成正文，字号按原件还原。

    这一条和 :func:`test_preserves_bold_italic_and_font_size` 的区别是
    样张更接近真实文档（七行、多个段落），用来确认段落多了以后
    标题级别的判定不会漂。
    """
    source = build_text_pdf(CHINESE_PAGE)
    response = client.post(PDF_TO_WORD, files=pdf_to_word_files("采购合同.pdf", source))
    paragraphs = docx_runs(download_docx(client, response.json()))

    title = next(item for item in paragraphs if item[0] == "采购合同")
    assert title[1] == "Heading 1"
    assert title[2][0][3] == pytest.approx(20.0)

    body = next(item for item in paragraphs if item[0].startswith("第一条"))
    assert body[1] == "Normal"
    assert body[2][0][3] == pytest.approx(12.0)


@requires_docx
def test_english_pdf_keeps_words_and_spaces(client: TestClient) -> None:
    """英文 PDF：整句完好，单词之间的空格还在。

    英文和中文的断言方式不一样 —— 英文**不能**去掉空白比对，
    那样 ``the same period`` 变成 ``thesameperiod`` 也会通过，
    而连在一起的单词在 Word 里是要重打的。
    """
    source = build_english_pdf(
        [
            "Annual Summary",
            "The revenue grew by 18 percent compared with the same period.",
            "Operating costs remained flat across all three regions.",
        ]
    )
    response = client.post(PDF_TO_WORD, files=pdf_to_word_files("summary.pdf", source))
    assert response.status_code == 200

    text = collapse(
        "".join(item[0] for item in docx_runs(download_docx(client, response.json())))
    )
    for line in pdf_lines(source):
        assert collapse(line) in text, f"源文件里的这一行在 Word 里找不到：{line!r}"


@requires_docx
def test_english_inline_styles_survive_in_one_paragraph(client: TestClient) -> None:
    """一行里混排普通 / 粗体 / 斜体，要留在**同一个段落**里。

    这行字是用真实字宽排出来的，字距正常，所以判成三段就是分段逻辑的错，
    不是样张的问题。粗体和斜体是 PDF → Word 里最容易丢的信息 ——
    丢了用户得逐句重排，比排版差异严重得多。
    """
    source = build_inline_pdf(
        [
            ("Revenue grew ", "helv"),
            ("18 percent", "hebo"),
            (" while costs stayed ", "helv"),
            ("flat", "heit"),
            (".", "helv"),
        ]
    )
    response = client.post(PDF_TO_WORD, files=pdf_to_word_files("inline.pdf", source))
    paragraphs = docx_runs(download_docx(client, response.json()))

    body = next(item for item in paragraphs if item[0].startswith("Revenue grew"))
    assert body[0] == "Revenue grew 18 percent while costs stayed flat."
    assert [run[0] for run in body[2]] == [
        "Revenue grew ",
        "18 percent",
        " while costs stayed ",
        "flat",
        ".",
    ]
    # ``None`` 和 ``False`` 一样都表示「不粗」，区别只是写不写 w:b 这个标签 ——
    # 这里关心的是哪一段被标粗了，而不是标签怎么写
    assert [bool(run[1]) for run in body[2]] == [
        False,
        True,
        False,
        False,
        False,
    ], "粗体丢了"
    assert [bool(run[2]) for run in body[2]] == [
        False,
        False,
        False,
        True,
        False,
    ], "斜体丢了"


@requires_docx
def test_mixed_chinese_english_pdf_is_kept_whole(client: TestClient) -> None:
    """中英混排：中文句子夹拉丁词，两者都不能丢。

    这是中文技术文档和合同里最常见的形态，也是字体回退最容易吃掉东西的地方。
    """
    source = build_text_pdf(
        [
            "产品说明",
            "本产品支持 PDF 与 Word 两种格式的相互转换。",
            "接口地址为 /api/office/pdf-to-word，返回 JSON。",
        ]
    )
    response = client.post(PDF_TO_WORD, files=pdf_to_word_files("说明.pdf", source))
    assert response.status_code == 200

    text = squash("".join(item[0] for item in docx_runs(download_docx(client, response.json()))))
    for line in pdf_lines(source):
        assert squash(line) in text, f"源文件里的这一行在 Word 里找不到：{line!r}"
    assert "pdf-to-word" in text


# ----------------------------------------------------------------------
# 多页 PDF
# ----------------------------------------------------------------------


@requires_docx
def test_multi_page_chinese_pdf_keeps_page_order(client: TestClient) -> None:
    """中文多页文档：页序对，每页的字都落在自己那一页里。

    DOCX 没有「页」这个概念，分页符是页序唯一的真实表示 ——
    所以这里既数分页符的个数，也验每段里的内容。
    """
    pages = [
        [f"第{index}章 测试章节", f"这是第 {index} 章的第一段正文内容。"]
        for index in range(1, 6)
    ]
    source = build_text_pdf(*pages)
    response = client.post(PDF_TO_WORD, files=pdf_to_word_files("手册.pdf", source))
    payload = response.json()
    assert payload["page_count"] == 5

    # 下载令牌是**一次性的**，所以只取一次，两个断言共用同一份字节
    data = download_docx(client, payload)
    xml = assert_docx_structure(data)
    # 5 页之间正好 4 个分页符：多一个会多出空白页，少一个会把两页并成一页
    assert xml.count('<w:br w:type="page"/>') == 4

    segments = docx_pages(data)
    assert len(segments) == 5
    for index, segment in enumerate(segments, start=1):
        assert f"第{index}章" in segment, f"第 {index} 章跑到别页去了：{segments}"
        for other in range(1, 6):
            if other != index:
                assert f"第{other}章" not in segment, f"第 {index} 页里混进了第 {other} 章"


@requires_docx
def test_every_source_page_survives_the_conversion(client: TestClient) -> None:
    """源文件有几页，Word 就该有几页的内容 —— 一页都不能少。

    页数不等是「悄悄丢页」的典型症状，比排版差异严重得多，
    所以拿源 PDF 的真实页数去核对，而不是只信返回值。
    """
    pages = [[f"PAGE-{index} 的内容"] for index in range(1, 8)]
    source = build_text_pdf(*pages)
    response = client.post(PDF_TO_WORD, files=pdf_to_word_files("七页.pdf", source))

    import pymupdf

    doc = pymupdf.open(stream=source, filetype="pdf")
    try:
        assert doc.page_count == 7
    finally:
        doc.close()

    segments = docx_pages(download_docx(client, response.json()))
    assert len(segments) == 7
    assert all(segment.strip() for segment in segments), f"有整页是空的：{segments}"


# ----------------------------------------------------------------------
# 空 / 损坏 / 加密 / 类型不对
# ----------------------------------------------------------------------


@requires_docx
def test_empty_pdf_reports_pdf_empty(client: TestClient) -> None:
    """一页都没有的 PDF 给 PDF_EMPTY，而不是笼统的「文件损坏」。"""
    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("空.pdf", build_empty_pdf())
    )
    assert_clean_error(response, status=400, code="PDF_EMPTY")


@requires_docx
def test_corrupted_pdf_reports_corrupted_file(client: TestClient) -> None:
    """坏文件仍旧走通用的 CORRUPTED_FILE，这个码没有被这一阶段改掉。"""
    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("坏.pdf", b"%PDF-1.7\n" + b"\x00" * 400)
    )
    assert_clean_error(response, status=400, code="CORRUPTED_FILE")


@requires_docx
def test_encrypted_pdf_reports_pdf_encrypted(client: TestClient) -> None:
    """加密 PDF 给 PDF_ENCRYPTED —— 用户唯一能做的事就是换一份文件。"""
    import pymupdf

    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 100), "locked document", fontsize=12)
        locked = doc.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret"
        )
    finally:
        doc.close()

    response = client.post(PDF_TO_WORD, files=pdf_to_word_files("加密.pdf", locked))
    assert_clean_error(response, status=400, code="PDF_ENCRYPTED")


@requires_docx
def test_rejects_non_pdf_upload(client: TestClient) -> None:
    """拿别的格式过来，要在「是不是 PDF」这一步就被挡住。

    注意后端**不看 MIME**、只看内容：所以这里给的 Content-Type 仍是
    ``application/pdf``，靠内容不是 PDF 来触发拒绝。
    """
    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("笔记.txt", b"just plain text, not a pdf")
    )
    assert_clean_error(response, status=415, code="INVALID_FILE_TYPE")


@requires_docx
def test_too_many_pages_is_refused_with_a_way_out(client: TestClient, monkeypatch) -> None:
    """页数超限沿用第三阶段的 PDF_TOO_MANY_PAGES，而且必须告诉用户怎么绕过。

    这个码和「文件坏了」的区别就在这里：文件没问题，拆开就能转，
    所以文案里要出现「拆分」—— 只说「页数太多」等于把问题丢回给用户。
    """
    monkeypatch.setattr(settings, "MAX_PDF_PAGES", 1)

    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files(
            "三页.pdf", build_text_pdf(["第一页"], ["第二页"], ["第三页"])
        ),
    )
    error = assert_clean_error(response, status=400, code="PDF_TOO_MANY_PAGES")
    assert "拆分" in error["message"]


@requires_docx
def test_oversized_upload_is_refused_before_any_work(
    client: TestClient, monkeypatch
) -> None:
    """体积超限要在读文件之前就挡住，返回 413。

    这是资源限制里最靠前的一条：一份 2 GB 的 PDF 不该先落盘、再打开、
    再渲染到一半才被发现太大。
    """
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)

    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files("大文件.pdf", build_text_pdf(["体积测试"]) * 40),
    )
    assert_clean_error(response, status=413, code="FILE_TOO_LARGE")


# ----------------------------------------------------------------------
# 错误处理：六个码各自的触发路径
# ----------------------------------------------------------------------


@requires_docx
def test_a_document_with_nothing_extractable_reports_pdf_no_text(
    client: TestClient, monkeypatch
) -> None:
    """抽不出任何内容时给 PDF_NO_TEXT，而不是交出一份空白的 Word。

    触发条件写死是「文字层抽不出可用文字，**且** OCR 也没能产出文字」。
    这里用「抽取器对这一页什么也没抽出来」来构造 —— 那正是这个兜底要防的
    真实故障（文字层读得出来、内容却解析不出来）。宁可报错，
    也不给用户一份打开之后什么都没有、还不知道哪里出错的文档。
    """
    from office.document_ir import PAGE_KIND_TEXT, PageContent
    from pdf.analyzer import PageAnalysis
    from services import pdf_to_docx

    monkeypatch.setattr(
        pdf_to_docx,
        "analyze_text_page",
        lambda page, **kwargs: PageAnalysis(PageContent(kind=PAGE_KIND_TEXT)),
    )

    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("空内容.pdf", build_text_pdf(["标题"]))
    )
    error = assert_clean_error(response, status=422, code="PDF_NO_TEXT")
    assert "文字" in error["message"]
    assert "download_url" not in response.json()


@requires_docx
@requires_ocr
def test_a_failed_recognition_reports_ocr_failed(client: TestClient, monkeypatch) -> None:
    """组件在、但这一页认不出来 → OCR_FAILED（422），而不是 500。

    和 OCR_UNAVAILABLE 的区别是「该做什么」：那个是找管理员装组件，
    这个是稍后重试。所以两者必须是不同的码。
    """
    from utils.errors import OcrFailedError

    def explode(image, *, dpi):
        raise OcrFailedError("这一页的 OCR 识别没有成功，请稍后重试。")

    monkeypatch.setattr(ocr_service, "recognize", explode)

    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("扫描件.pdf", build_scanned_pdf(["扫描页-1"]))
    )
    assert_clean_error(response, status=422, code="OCR_FAILED")
    assert "download_url" not in response.json()


@requires_docx
def test_a_broken_docx_is_never_handed_over(client: TestClient, monkeypatch) -> None:
    """生成的 Word 打不开时**绝不能交给用户**，给 DOCX_GENERATION_FAILED。

    这条测的是最后那道复验。用户拿到一份打不开的文件时，
    已经不知道是哪一步出的问题，连该找谁都不知道 ——
    所以宁可在这里失败：转换没成功，但用户清楚。
    """
    from office.docx_writer import DocxBuild

    monkeypatch.setattr(
        "office.docx_writer.build_docx",
        lambda pages, *, east_asian_font: DocxBuild(b"PK\x03\x04 not a readable docx"),
    )

    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("报告.pdf", build_text_pdf(["季度报告"]))
    )
    assert_clean_error(response, status=422, code="DOCX_GENERATION_FAILED")
    assert "download_url" not in response.json()


@requires_docx
def test_running_out_of_time_reports_its_own_code(
    client: TestClient, monkeypatch
) -> None:
    """超时给 PDF_CONVERSION_TIMEOUT（504），并且带上「先拆分」这条出路。

    单独成码而不是复用 PROCESSING_TIMEOUT：这里用户该做的是**减少页数**，
    不是降低清晰度，两者给的建议不一样。
    """
    monkeypatch.setattr(settings, "PDF_TO_WORD_WORKER_BUDGET_SECONDS", -1)

    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("大件.pdf", build_text_pdf(["很长的文档"]))
    )
    error = assert_clean_error(response, status=504, code="PDF_CONVERSION_TIMEOUT")
    assert "拆分" in error["message"]


@requires_docx
def test_missing_docx_component_is_a_503_not_a_crash(
    client: TestClient, monkeypatch
) -> None:
    """服务器上少了 python-docx：503 + 明确文案，而不是 500。

    前端靠 ``/api/config`` 的 ``pdf_to_word_available`` 提前禁用入口，
    但接口本身也必须挡得住 —— 直接调接口的人（和竞态）都绕不过去。
    """
    import sys

    # sys.modules 里放 None 会让之后的 import 抛 ImportError，
    # 这就是「服务器上没装这个包」在运行时的真实表现。
    monkeypatch.setitem(sys.modules, "office.docx_writer", None)

    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("报告.pdf", build_text_pdf(["季度报告"]))
    )
    error = assert_clean_error(response, status=503, code="CONVERTER_UNAVAILABLE")
    assert "管理员" in error["message"]


# ----------------------------------------------------------------------
# 缺 OCR 组件
# ----------------------------------------------------------------------


@requires_docx
def test_scanned_pdf_reports_ocr_unavailable(client: TestClient, monkeypatch) -> None:
    """没有 OCR 组件时，扫描 PDF 给 OCR_UNAVAILABLE（503），**不是 500**。

    这是规格里点名的要求：文件本身没毛病，重传多少次都一样，
    用户的下一步是找管理员。
    """
    monkeypatch.setattr(ocr_service, "is_available", lambda: False)

    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("扫描件.pdf", build_scanned_pdf(["扫描页一"]))
    )
    error = assert_clean_error(
        response, status=503, code="OCR_UNAVAILABLE", contains="当前服务器未安装 OCR 组件"
    )
    assert "扫描" in error["message"]


@requires_docx
def test_text_pdf_still_works_without_ocr(client: TestClient, monkeypatch) -> None:
    """缺 OCR 只影响扫描件，文字版 PDF **必须照常可用**。

    这条是那个降级设计的全部意义所在：如果哪天有人把 OCR 可用性检查
    挪到入口处，文字版会跟着一起坏掉，而用户完全没有办法绕过。
    """
    monkeypatch.setattr(ocr_service, "is_available", lambda: False)

    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("文字版.pdf", build_text_pdf(["文字版文档"]))
    )
    assert response.status_code == 200
    text = "".join(item[0] for item in docx_runs(download_docx(client, response.json())))
    assert "文字版文档" in text


@requires_docx
def test_mixed_document_sends_only_the_scanned_page_to_ocr(
    client: TestClient, monkeypatch
) -> None:
    """一份「正文是字、封面是图」的 PDF：只有图那一页该走 OCR。

    封面是图、正文是字的 PDF 非常常见。整份判成扫描件会把几十页本来
    清清楚楚的正文也送去 OCR —— 又慢又更不准。所以判定必须逐页做。
    """
    monkeypatch.setattr(ocr_service, "is_available", lambda: False)

    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files(
            "混合.pdf",
            build_mixed_pdf(
                [
                    ("text", ["第一章 总则", "本合同用于测试逐页判定。"]),
                    ("scan", "盖章页"),
                    ("text", ["第二章 细则", "继续测试后面的文字页。"]),
                ]
            ),
        ),
    )
    # 真去 OCR 的是中间那一页；缺组件时如实报错，而不是把整份判成扫描件
    assert_clean_error(
        response, status=503, code="OCR_UNAVAILABLE", contains="当前服务器未安装 OCR 组件"
    )


@requires_docx
def test_a_scanned_page_at_the_end_is_not_silently_dropped(
    client: TestClient, monkeypatch
) -> None:
    """**扫描页在最后一页时，绝不能吐出一份少一页的 Word。**

    这是最危险的一种坏法：前三页转得好好的，用户拿到文件也打得开，
    只是最后那页没了 —— 不看页数根本发现不了。所以哪怕只有一页要 OCR、
    哪怕它排在最后，也必须整体报错，而不是交出一份悄悄缺页的文档。
    """
    monkeypatch.setattr(ocr_service, "is_available", lambda: False)

    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files(
            "缺尾页.pdf",
            build_mixed_pdf(
                [
                    ("text", ["第一章 总则", "本合同用于测试逐页判定。"]),
                    ("text", ["第二章 细则", "继续测试后面的文字页。"]),
                    ("scan", "附件扫描件"),
                ]
            ),
        ),
    )
    assert_clean_error(response, status=503, code="OCR_UNAVAILABLE")
    # 没有下载链接 = 没有交出任何文件
    assert "download_url" not in response.json()


@requires_docx
def test_a_wholly_scanned_document_is_reported_as_a_scan(
    client: TestClient, monkeypatch
) -> None:
    """整份都是扫描件时，报的是同一件事，文案要指向「扫描 PDF」。"""
    monkeypatch.setattr(ocr_service, "is_available", lambda: False)

    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files("整份扫描.pdf", build_scanned_pdf(["第一页", "第二页"])),
    )
    error = assert_clean_error(response, status=503, code="OCR_UNAVAILABLE")
    assert "扫描" in error["message"]


# ----------------------------------------------------------------------
# 扫描 PDF → DOCX（原图 + 可编辑文字）
# ----------------------------------------------------------------------


@requires_docx
@requires_ocr
def test_a_scanned_pdf_becomes_images_plus_editable_text(client: TestClient) -> None:
    """扫描 PDF → Word 的形态是「原图 + 可编辑文字」。

    规格里两条要求在这里合流：图片尽量保留、OCR 结果要是**可编辑文字**。
    所以这条同时钉三件事：

    * 原图真的进了 DOCX —— 没有原图，用户就没法核对 OCR 认得对不对；
    * 文字是货真价实的段落，用 python-docx 回读得出来，不是一张画着字的图；
    * 每页都带一句「这是 OCR 认的」的提示（随页走，不能只在结果页说一次）。
    """
    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files("扫描件.pdf", build_scanned_pdf(["文件转换测试系统"])),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["document_kind"] == "scan"
    assert payload["extraction_method"] == "ocr"
    assert payload["ocr_pages"] == 1
    assert payload["text_pages"] == 0

    data = download_docx(client, payload)
    assert_docx_structure(data)
    assert docx_media(data), "扫描页的原图没有嵌进 DOCX"

    paragraphs = docx_runs(data)
    text = "".join(item[0] for item in paragraphs)
    assert normalize_ocr_text("文件转换测试系统") in normalize_ocr_text(text)
    assert any("OCR" in item[0] for item in paragraphs), "每页都该说明文字是 OCR 认的"

    assert any("OCR" in note for note in payload["notes"])


@requires_docx
@requires_ocr
def test_ocr_pages_land_in_the_right_position(client: TestClient) -> None:
    """混排文档里，OCR 那一页的文字必须落在**它自己那一页**。

    扫描页的文字是另一条路径产出再拼回去的，最容易出的错不是「认错字」，
    而是「内容对但整体错位一页」—— 那种错对用户来说更致命，
    照着一份错位的合同改，后果比认错几个字严重得多。
    """
    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files(
            "混排.pdf",
            build_mixed_pdf(
                [
                    ("text", ["第一章 总则", "本合同用于测试逐页判定。"]),
                    ("scan", "文件转换测试系统"),
                    ("text", ["第二章 细则", "继续测试后面的文字页。"]),
                ]
            ),
        ),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["page_kinds"] == ["text", "scan", "text"]

    pages = docx_pages(download_docx(client, payload))
    assert len(pages) == 3
    assert "第一章" in squash(pages[0])
    assert normalize_ocr_text("文件转换测试系统") in normalize_ocr_text(pages[1])
    assert "第二章" in squash(pages[2])


@requires_docx
@requires_ocr
def test_the_ocr_page_limit_keeps_the_rest_as_images(
    client: TestClient, monkeypatch
) -> None:
    """超出 OCR 页数上限的页：**不静默丢弃**，原图照样进，只是没有文字。

    页数上限是硬的（不设的话一份 500 页的扫描件要跑十几分钟），
    但「不让 OCR」不等于「这页不要了」—— 用户至少得拿到那张原图，
    并且知道这一页为什么没有可编辑文字。
    """
    monkeypatch.setattr(settings, "PDF_TO_WORD_MAX_OCR_PAGES", 1)

    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files(
            "超限.pdf", build_scanned_pdf(["文件转换测试系统", "扫描页-1"])
        ),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ocr_pages"] == 2, "两页都是扫描页"

    data = download_docx(client, payload)
    pages = docx_pages(data)
    assert len(pages) == 2, "超限的页也要在，一页都不能少"
    assert normalize_ocr_text("文件转换测试系统") in normalize_ocr_text(pages[0])
    assert normalize_ocr_text("扫描页-1") not in normalize_ocr_text(pages[1])
    assert "页数上限" in squash(pages[1])
    assert len(docx_media(data)) == 2, "两页的原图都该在"
    assert any("上限" in note for note in payload["notes"])


@requires_docx
@requires_ocr
def test_the_image_budget_never_takes_the_text_with_it(
    client: TestClient, monkeypatch
) -> None:
    """图片预算用光时，**文字必须照常输出**。

    规格给的优先级是「文字正确 > 页面顺序 > 图片不丢失」，
    所以预算不够时该牺牲的是图，不是文字。这条钉的就是这个取舍。
    """
    monkeypatch.setattr(settings, "PDF_TO_WORD_MAX_EMBED_BYTES", 0)

    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files("无图.pdf", build_scanned_pdf(["文件转换测试系统"])),
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    data = download_docx(client, payload)
    assert_docx_structure(data)
    assert docx_media(data) == [], "预算为 0 就不该有图进去"
    assert normalize_ocr_text("文件转换测试系统") in normalize_ocr_text(
        "".join(docx_pages(data))
    )
    assert any("没有嵌入" in note for note in payload["notes"])


@requires_docx
@requires_ocr
def test_a_blank_scanned_page_is_not_a_failure(client: TestClient) -> None:
    """空白扫描页不是错误。

    真实扫描件里分隔页、背页都是白的。把「一个字都没认出来」当失败的话，
    一份 20 页的扫描件只要夹一张白页就整份转换失败 —— 用户完全没法绕过。
    """
    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("白页.pdf", build_scanned_pdf([""]))
    )
    assert response.status_code == 200, response.text

    pages = docx_pages(download_docx(client, response.json()))
    assert len(pages) == 1
    assert "没有识别出文字" in squash(pages[0])


@requires_docx
@requires_ocr
def test_a_successful_ocr_conversion_leaks_nothing(client: TestClient) -> None:
    """成功响应里也不能出现服务器内部信息 —— 尤其是走 OCR 这一路。

    OCR 引擎的 stdout 里带着模型路径，一旦有人图省事把日志塞进 notes，
    泄露出去的就是整台服务器的目录结构。所以这条查的是 200 的响应。
    """
    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files("扫描件.pdf", build_scanned_pdf(["文件转换测试系统"])),
    )
    assert response.status_code == 200, response.text

    raw = response.text
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in raw, f"成功响应里泄露了 {fragment!r}"


def test_config_reports_component_availability(client: TestClient) -> None:
    """/api/config 要如实报告两个组件的可用性，前端据此在上传前禁用入口。"""
    config = client.get("/api/config").json()

    assert isinstance(config["pdf_to_word_available"], bool)
    assert isinstance(config["ocr_available"], bool)
    assert config["ocr_languages"] == ["chi_sim", "eng"]
    assert config["pdf_to_word_max_ocr_pages"] > 0
    for code in (
        "PDF_EMPTY",
        "PDF_NO_TEXT",
        "OCR_UNAVAILABLE",
        "OCR_FAILED",
        "DOCX_GENERATION_FAILED",
        "PDF_CONVERSION_TIMEOUT",
    ):
        assert code in config["error_codes"]


# ----------------------------------------------------------------------
# 进度接口
# ----------------------------------------------------------------------


def test_progress_endpoint_reports_registered_stage(client: TestClient) -> None:
    """进度接口返回的是真实阶段，没有百分比。

    只有 OCR 期间能诚实算出「第几页 / 共几页」，其余阶段只给阶段名 ——
    与其编一个 50%、70%，不如老实显示「正在分析 PDF…」。
    """
    progress_store.start("manual-progress-01", page_count=12)
    try:
        body = client.get(f"{PROGRESS}/manual-progress-01").json()
        assert body == {"stage": "analyzing", "page": None, "page_count": 12}

        progress_store.update("manual-progress-01", "ocr", page=3)
        body = client.get(f"{PROGRESS}/manual-progress-01").json()
        assert body == {"stage": "ocr", "page": 3, "page_count": 12}
    finally:
        progress_store.finish("manual-progress-01")

    # 转换结束后条目立刻消失，前端不用再多轮询一次
    assert client.get(f"{PROGRESS}/manual-progress-01").status_code == 404


@requires_docx
def test_conversion_clears_its_progress_entry(client: TestClient) -> None:
    """转换自己走完之后，进度条目要清掉，不能一直挂在内存里。"""
    progress_id = "conversion-progress-01"
    response = client.post(
        PDF_TO_WORD,
        files=pdf_to_word_files("进度.pdf", build_text_pdf(["标题"])),
        data={"progress_id": progress_id},
    )
    assert response.status_code == 200
    assert client.get(f"{PROGRESS}/{progress_id}").status_code == 404


def test_progress_for_unknown_id_is_not_found(client: TestClient) -> None:
    """没登记过的 id 就是 404，不用参数错误去打扰用户。"""
    response = client.get(f"{PROGRESS}/never-existed-0001")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


@pytest.mark.parametrize(
    "bad_id",
    ["short", "有中文的标识符", "a" * 200, "with space", "dots.not.allowed"],
)
def test_progress_rejects_malformed_ids(client: TestClient, bad_id: str) -> None:
    """id 形状不合法一律当作「不存在」。

    它是前端生成的、直接摆在 URL 里的值，必须当成不可信输入：
    不校验的话，一个超长字符串就能把内存撑起来。

    这里**不测带斜杠的 id**：斜杠会在路由那一层就把路径切成两段，
    根本到不了这个视图函数（会被前端的静态资源回退接走）。真正的
    ``_ID_PATTERN`` 允许的字符集里不含斜杠，两道关是重合的。
    """
    assert client.get(f"{PROGRESS}/{bad_id}").status_code == 404


# ----------------------------------------------------------------------
# 下载、清理、真实消费者
# ----------------------------------------------------------------------


@requires_docx
def test_source_pdf_is_deleted_after_conversion(client: TestClient) -> None:
    """服务器不留用户的原始文档：转换完源 PDF 立刻从临时目录消失。"""
    before = temp_dirs()
    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("清理.pdf", build_text_pdf(["标题"]))
    )
    assert response.status_code == 200

    created = temp_dirs() - before
    assert created, "没有观察到本次的临时目录"
    uploads = [item for work_dir in created for item in work_dir.glob("*.upload")]
    assert not uploads, f"源 PDF 没有被删掉：{uploads}"


@requires_docx
def test_download_uses_the_existing_token_mechanism(client: TestClient) -> None:
    """下载走的是既有的令牌机制，没有为这一阶段另起一套。"""
    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("下载.pdf", build_text_pdf(["标题"]))
    )
    payload = response.json()
    assert payload["download_url"].startswith("/api/download/")

    download = client.get(payload["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )
    # 名字里带 .docx，浏览器才会存成 Word 文档
    assert ".docx" in download.headers.get("content-disposition", "")


@requires_docx
def test_produced_docx_opens_in_a_real_consumer(client: TestClient) -> None:
    """把下载到的 DOCX 回灌进第五阶段的「Word → PDF」，读出 PDF 文字。

    LibreOffice 能打开并读出内容，比十条 XML 断言都硬 ——
    它验的是「别的软件真的认这份文件」，而不只是「我们自己的库认得」。
    """
    from services.office_converter import find_soffice
    from tests.conftest import office_files, open_downloaded_pdf

    if find_soffice() is None:
        pytest.skip("本机没有 LibreOffice，跳过真实消费者验证")

    created = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("回灌.pdf", build_text_pdf(["回灌验证"]))
    )
    docx_data = download_docx(client, created.json())

    converted = client.post(WORD_TO_PDF, files=office_files(("回灌.docx", docx_data)))
    assert converted.status_code == 200

    with open_downloaded_pdf(client, converted.json()) as doc:
        text = "".join(page.get_text() for page in doc)
    assert "回灌验证" in text


@requires_docx
def test_docx_actually_opens_with_python_docx(client: TestClient) -> None:
    """第三方库也认这份文件（和标准库、LibreOffice 是三个不同视角）。"""
    response = client.post(
        PDF_TO_WORD, files=pdf_to_word_files("打开.pdf", build_text_pdf(["标题"]))
    )
    Document(io.BytesIO(download_docx(client, response.json())))


# ----------------------------------------------------------------------
# 错误码文案：后端报得出的码，前端都得有一句中文
# ----------------------------------------------------------------------


def test_every_error_code_has_frontend_copy() -> None:
    """``ErrorCode`` 里的每个码，前端 ``errorMessages.ts`` 里都有文案。

    ``/api/config`` 把全部错误码交给前端，本意就是让前端能自检有没有漏配；
    这条断言把那个自检真的跑起来（比 ``verify_phase4.py`` 里那条严：
    它只要求码在文件里出现过，这里要求它是一条真正的文案条目）。
    漏配的后果不是崩溃，而是用户看到「处理失败」这种什么都没说的标题 ——
    而且往往要等到线上才被发现。

    比对的是 ``ErrorCode`` 而不是 ``STATUS_BY_CODE``：后者由异常类生成，
    而兜底的 500 是 catch-all 处理器直接构造的，同样会到达客户端。

    扫的是源码文本而不是跑前端：断言要能在没装 node 的机器上跑。
    """
    path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "utils" / "errorMessages.ts"
    if not path.exists():  # pragma: no cover - 只装后端时跳过，不是失败
        pytest.skip("没有前端源码，跳过文案覆盖检查")

    declared = {
        name: value
        for name, value in vars(ErrorCode).items()
        if not name.startswith("_") and isinstance(value, str)
    }

    text = path.read_text(encoding="utf-8")
    # 后端错误码在文件里都是顶格两格缩进的裸键；前端本地码走
    # ``[CLIENT_ERROR_CODES.xxx]`` 这种计算键，因此不会被这个正则捞进来。
    frontend_codes = set(re.findall(r"^  ([A-Z][A-Z0-9_]+):\s*\{", text, re.MULTILINE))

    missing = sorted(set(declared.values()) - frontend_codes)
    assert not missing, f"这些错误码在前端没有中文文案：{missing}"

    extra = sorted(frontend_codes - set(declared.values()))
    assert not extra, (
        f"这些码后端没有声明，前端写了也不会有人返回：{extra}；"
        "前端自己的错误码请登记到 CLIENT_ERROR_CODES，用计算键写。"
    )

    # 有键但没有标题等于没写：兜底会退回服务端原话，用户就看不懂了
    for code in sorted(declared.values()):
        entry = re.search(
            rf"^  {code}:\s*\{{(.*?)^  \}},", text, re.MULTILINE | re.DOTALL
        )
        assert entry and "title:" in entry.group(1), f"{code} 的文案没有 title"
