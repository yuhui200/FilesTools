"""TXT → DOCX / HTML / Markdown 的功能测试（第九阶段第 10 步）。

每一处判定都落在**真产物**上：DOCX 用 ``python-docx`` 打开读段落与字体，
HTML 用标准库的 ``html.parser`` 解析回来数标签，Markdown 再走一遍
``markup_parse.parse_markdown`` 做往返。「函数没抛异常」不算验过。

安全那一半同样验在产物上：一份内容是 ``<script>`` 的 .txt 转成 HTML，
读回来的必须是**转义后的文本**，不是一个真的 ``<script>`` 元素；
一份内容以 ``#`` 开头的 .txt 转成 Markdown，再解析回来必须还是正文段落。
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from html.parser import HTMLParser

import pytest
from docx import Document

from conversion import registry
from office import markup_parse, markup_render
from office.document_ir import PARA_KIND_BODY, blocks_from_text
from office.txt_to_pdf import TOO_LONG_MESSAGE
from services import text_service
from services.conversion_types import ConversionOptions, ConversionRequest
from utils.errors import ValidationError

SAMPLE = "\n".join(
    [
        "季度总结",
        "本季度收入 1234 万元，同比增长 12%。",
        "  这一行前面有两个空格，表示它是上一行的补充说明。",
        "",
        "下一步计划",
        "1. 扩产",
        "2. 招人",
    ]
)


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------

class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.counts: dict[str, int] = {}
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.counts[tag] = self.counts.get(tag, 0) + 1

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    @property
    def body_text(self) -> str:
        return "".join(self.text)


def _request(tmp_path, target: str, text: str = SAMPLE, **options) -> ConversionRequest:
    source = tmp_path / "in" / "001.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(text, encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    return ConversionRequest(
        source=source,
        filename="季度总结.txt",
        source_type=registry.SOURCE_TXT,
        target_type=target,
        out_dir=out_dir,
        options=ConversionOptions(**options),
    )


def _convert(tmp_path, target: str, text: str = SAMPLE, **options):
    """跑一次转换。

    ``asyncio.run``：这两个叶子是 ``async`` 的（内部要 ``await run_in_pool``
    把纯计算丢进线程池），但测试里没有别的事件循环要共享 ——
    起一个、跑完、关掉，比给整个测试套件引入一个 async 插件干净。
    """
    request = _request(tmp_path, target, text, **options)
    if target == registry.TARGET_DOCX:
        return asyncio.run(text_service.text_to_docx(request, "季度总结"))
    return asyncio.run(text_service.text_convert(request, "季度总结"))


# ----------------------------------------------------------------------
# A. 拆段：一行一段，空行丢掉，行尾空白去掉
# ----------------------------------------------------------------------

def test_one_line_becomes_one_paragraph() -> None:
    blocks = blocks_from_text("第一行\n第二行\n第三行")
    assert [block.runs[0].text for block in blocks] == ["第一行", "第二行", "第三行"]
    assert all(block.kind == PARA_KIND_BODY for block in blocks)


def test_blank_lines_are_dropped() -> None:
    """.txt 里的空行只是分隔符，不该变成一段真空白。"""
    blocks = blocks_from_text("甲\n\n\n乙\n   \n丙")
    assert [block.runs[0].text for block in blocks] == ["甲", "乙", "丙"]


def test_trailing_whitespace_goes_but_indent_stays() -> None:
    """行尾的 ``\\r`` / 空格去掉；**行首的缩进保留** —— 那是内容。"""
    blocks = blocks_from_text("正文   \r\n  缩进的补充说明\t\r\n")
    assert [block.runs[0].text for block in blocks] == ["正文", "  缩进的补充说明"]


def test_empty_text_yields_nothing() -> None:
    assert blocks_from_text("") == []
    assert blocks_from_text("\n\n  \n") == []


def test_blocks_are_plain_runs_with_no_decoration() -> None:
    """纯文本里没有粗体、没有链接 —— IR 里也不该凭空多出来。"""
    (block,) = blocks_from_text("**这不是粗体**")
    (run,) = block.runs
    assert run.bold is False and run.italic is False
    assert run.link is None and run.code is False
    assert run.text == "**这不是粗体**"


# ----------------------------------------------------------------------
# B. TXT → DOCX：真的用 python-docx 打开
# ----------------------------------------------------------------------

def test_docx_really_opens_and_has_the_paragraphs(tmp_path) -> None:
    result = _convert(tmp_path, registry.TARGET_DOCX)

    assert result.path.exists()
    # 不是 zipfile 看一眼就算数：真用 python-docx 打开
    document = Document(str(result.path))
    texts = [p.text for p in document.paragraphs if p.text]
    assert texts == [
        "季度总结",
        "本季度收入 1234 万元，同比增长 12%。",
        "  这一行前面有两个空格，表示它是上一行的补充说明。",
        "下一步计划",
        "1. 扩产",
        "2. 招人",
    ]


def test_docx_is_a_valid_ooxml_package(tmp_path) -> None:
    result = _convert(tmp_path, registry.TARGET_DOCX)
    with zipfile.ZipFile(io.BytesIO(result.path.read_bytes())) as archive:
        names = set(archive.namelist())
    assert "word/document.xml" in names
    assert "[Content_Types].xml" in names


def test_docx_writes_the_east_asian_font(tmp_path) -> None:
    """字体选项要真的落到文档里，不能只是收下不用。"""
    result = _convert(
        tmp_path, registry.TARGET_DOCX, txt_options=None
    )
    raw = result.path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        xml = archive.read("word/styles.xml").decode("utf-8")
    # 默认字体是探测到的第一个（本机是宋体）；只断言它非空且是个中文字体名
    assert "eastAsia" in xml
    label = text_service._font_label(ConversionOptions())
    assert label in xml


def test_docx_notes_say_what_it_did_and_what_it_did_not_do(tmp_path) -> None:
    result = _convert(tmp_path, registry.TARGET_DOCX)
    joined = "".join(result.notes)
    assert "6 段" in joined, result.notes
    assert text_service.NO_MARKUP_NOTE in result.notes


def test_docx_does_not_fake_a_page_count(tmp_path) -> None:
    """``.txt`` 里没有分页，DOCX 的页是 Word 打开时算的 —— 不许估一个数。"""
    result = _convert(tmp_path, registry.TARGET_DOCX)
    assert result.page_count is None


# ----------------------------------------------------------------------
# C. TXT → HTML：解析回来数标签，并且验证转义
# ----------------------------------------------------------------------

def test_html_is_a_standalone_document(tmp_path) -> None:
    result = _convert(tmp_path, registry.TARGET_HTML)
    html = result.path.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert 'charset="utf-8"' in html
    assert html.rstrip().endswith("</html>")


def test_html_has_one_paragraph_per_line(tmp_path) -> None:
    result = _convert(tmp_path, registry.TARGET_HTML)
    parser = _Tags()
    parser.feed(result.path.read_text(encoding="utf-8"))
    assert parser.counts.get("p", 0) == 6
    for fragment in ("季度总结", "1234 万元", "下一步计划", "2. 招人"):
        assert fragment in parser.body_text


def test_html_has_no_invented_structure(tmp_path) -> None:
    """纯文本里没有标题、列表，产物里就不该有 ``<h1>`` / ``<ul>``。"""
    result = _convert(tmp_path, registry.TARGET_HTML)
    parser = _Tags()
    parser.feed(result.path.read_text(encoding="utf-8"))
    for tag in ("h1", "h2", "ul", "ol", "li", "table", "blockquote"):
        assert parser.counts.get(tag, 0) == 0, tag


def test_html_escapes_a_script_line_instead_of_injecting_it(tmp_path) -> None:
    """这是**安全**断言，不是格式断言。

    一份内容是 ``<script>`` 的 .txt 转成 HTML，产物里那个标签必须是
    **文本**（``&lt;script&gt;``），不能是一个真的 script 元素 ——
    否则用户上传一个 .txt 就能让结果页跑自己的脚本。
    """
    payload = "<script>alert('x')</script>\n<b>加粗标签也只是文字</b>"
    result = _convert(tmp_path, registry.TARGET_HTML, payload)

    html = result.path.read_text(encoding="utf-8")
    parser = _Tags()
    parser.feed(html)
    assert parser.counts.get("script", 0) == 0
    assert parser.counts.get("b", 0) == 0
    assert "<script>alert('x')</script>" in parser.body_text
    assert "&lt;script&gt;" in html


def test_html_notes_include_the_no_markup_line(tmp_path) -> None:
    result = _convert(tmp_path, registry.TARGET_HTML)
    assert text_service.NO_MARKUP_NOTE in result.notes


# ----------------------------------------------------------------------
# D. TXT → Markdown：往返解析，正文必须还是正文
# ----------------------------------------------------------------------

def test_markdown_round_trips_to_the_same_paragraphs(tmp_path) -> None:
    """六段正文原样往返 —— 逐字相等，不是「长得差不多」。"""
    result = _convert(tmp_path, registry.TARGET_MD)
    text = result.path.read_text(encoding="utf-8")

    reparsed = markup_parse.parse_markdown(text)
    assert [block.runs[0].text for block in reparsed.blocks] == [
        "季度总结",
        "本季度收入 1234 万元，同比增长 12%。",
        # Markdown 段落不保留行首空白（见下一条），这里如实写出去掉缩进的样子
        "这一行前面有两个空格，表示它是上一行的补充说明。",
        "下一步计划",
        "1. 扩产",
        "2. 招人",
    ]
    assert all(block.kind == PARA_KIND_BODY for block in reparsed.blocks)


def test_only_markdown_loses_a_paragraphs_leading_indent(tmp_path) -> None:
    """行首缩进在 HTML / DOCX 里留得住，在 Markdown 里留不住。

    这不是我们少做了一步：Markdown 的段落**本来就不保留行首空白**
    （CommonMark 把 1~3 个前导空格直接丢掉，4 个以上则变成代码块），
    所以 .md 这个格式装不下「一行以两个空格开头」这件事。
    """
    indented = "  这一行前面有两个空格。"

    docx_result = _convert(tmp_path, registry.TARGET_DOCX, indented)
    texts = [p.text for p in Document(str(docx_result.path)).paragraphs if p.text]
    assert texts == [indented]

    html_result = _convert(tmp_path, registry.TARGET_HTML, indented)
    assert "  这一行前面有两个空格。" in html_result.path.read_text(encoding="utf-8")

    md_result = _convert(tmp_path, registry.TARGET_MD, indented)
    reparsed = markup_parse.parse_markdown(md_result.path.read_text(encoding="utf-8"))
    assert reparsed.blocks[0].runs[0].text == indented.strip()


@pytest.mark.parametrize(
    "line",
    [
        "# 这不是标题",
        "- 这不是列表项",
        "1. 这也不是有序列表",
        "> 这不是引用",
        "* 星号开头",
        "2) 括号序号",
        # 分隔线：一行横杠在纯文本里就是一行横杠，不是一条 <hr>
        "---",
        "****",
    ],
)
def test_markdown_keeps_line_leading_markers_as_text(tmp_path, line: str) -> None:
    """纯文本里以 Markdown 记号开头的行，转出来还得是**正文**。

    否则用户拿到的 .md 里凭空多出标题和列表，而界面上写的是
    「结果只有正文段落」—— 那句话就成了假话。
    """
    result = _convert(tmp_path, registry.TARGET_MD, line)
    text = result.path.read_text(encoding="utf-8")

    reparsed = markup_parse.parse_markdown(text)
    assert len(reparsed.blocks) == 1, reparsed.blocks
    block = reparsed.blocks[0]
    assert block.kind == PARA_KIND_BODY
    assert block.level is None
    assert block.marker is None
    # 转义切出来的碎片要合回去，不能一句话在 IR 里碎成几段
    assert len(block.runs) == 1, block.runs
    assert block.runs[0].text == line


def test_markdown_escapes_specials_but_not_ordinary_hash(tmp_path) -> None:
    """``C#`` 里的 ``#`` 在句子中间，没有任何语义，不该被转义弄脏。"""
    result = _convert(tmp_path, registry.TARGET_MD, "C# 与 F# 都是语言")
    text = result.path.read_text(encoding="utf-8")
    assert "C#" in text and "C\\#" not in text


def test_markdown_notes_include_the_no_markup_line(tmp_path) -> None:
    result = _convert(tmp_path, registry.TARGET_MD)
    assert text_service.NO_MARKUP_NOTE in result.notes


# ----------------------------------------------------------------------
# E. 三个目标的共同约定
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "target,extension,media_type",
    [
        (registry.TARGET_DOCX, ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        (registry.TARGET_HTML, ".html", "text/html"),
        (registry.TARGET_MD, ".md", "text/markdown"),
    ],
)
def test_every_target_gets_its_own_extension_and_media_type(
    tmp_path, target: str, extension: str, media_type: str
) -> None:
    result = _convert(tmp_path, target)
    assert result.path.suffix == extension
    assert result.filename.endswith(extension)
    assert result.media_type == media_type
    assert result.size == result.path.stat().st_size
    assert result.job_id and result.download_url


def test_result_filename_is_sanitized(tmp_path) -> None:
    """原始文件名带路径分隔符时，结果名里不许出现它。"""
    source = tmp_path / "in" / "001.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(SAMPLE, encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    request = ConversionRequest(
        source=source,
        filename="../../evil.txt",
        source_type=registry.SOURCE_TXT,
        target_type=registry.TARGET_MD,
        out_dir=out_dir,
    )
    result = asyncio.run(text_service.text_convert(request, "evil"))

    assert "/" not in result.filename and "\\" not in result.filename
    assert result.path.parent == out_dir


def test_empty_file_is_refused_with_a_readable_message(tmp_path) -> None:
    with pytest.raises(ValidationError) as caught:
        _convert(tmp_path, registry.TARGET_MD, "\n\n   \n")
    message = str(caught.value)
    assert message == "这个文本文件里没有可转换的内容。"
    assert "Traceback" not in message


def test_too_long_text_is_refused_with_the_txt_message(tmp_path) -> None:
    """同一个源文件不该在这里放行、在 TXT→PDF 那里被拒 —— 用同一句文案。"""
    from config import settings

    with pytest.raises(ValidationError) as caught:
        _convert(tmp_path, registry.TARGET_HTML, "字" * (settings.MAX_TXT_CHARS + 1))
    assert str(caught.value) == TOO_LONG_MESSAGE


def test_unknown_target_is_refused_not_silently_downgraded(tmp_path) -> None:
    """认不出的目标要报错，不能悄悄回退成纯文本。"""
    from utils.errors import UnsupportedConversionError

    request = _request(tmp_path, registry.TARGET_MD)
    object.__setattr__(request, "target_type", "exe")
    with pytest.raises(UnsupportedConversionError):
        asyncio.run(text_service.text_convert(request, "季度总结"))


# ----------------------------------------------------------------------
# F. 与注册表/分发表的对账
# ----------------------------------------------------------------------

def test_registry_routes_every_txt_target_to_the_right_family() -> None:
    expected = {
        registry.TARGET_PDF: "text.to_pdf",
        registry.TARGET_DOCX: "text.to_docx",
        registry.TARGET_HTML: "text.convert",
        registry.TARGET_MD: "text.convert",
    }
    for target, family in expected.items():
        entry = registry.CAPABILITY_BY_PAIR[(registry.SOURCE_TXT, target)]
        assert entry.converter_key == family, target


def test_txt_to_docx_requires_python_docx_and_the_others_do_not() -> None:
    """逐格需求：TXT 的四个目标里只有 DOCX 那一格要 python-docx。"""
    requires = {
        target: registry.CAPABILITY_BY_PAIR[(registry.SOURCE_TXT, target)].requires
        for target in registry.TARGETS_BY_SOURCE[registry.SOURCE_TXT]
    }
    assert requires[registry.TARGET_DOCX] == registry.REQUIREMENT_DOCX
    for target in (registry.TARGET_PDF, registry.TARGET_HTML, registry.TARGET_MD):
        assert requires[target] == registry.REQUIREMENT_BUILTIN, target


def test_matrix_keeps_three_txt_targets_when_python_docx_is_missing() -> None:
    matrix = registry.available_matrix(
        office_ok=True, pdf_to_word_ok=False, heif_decode_ok=True, heif_encode_ok=True
    )
    assert matrix[registry.SOURCE_TXT] == ("pdf", "html", "md")


def test_missing_docx_reason_mentions_both_word_features() -> None:
    reasons = registry.unavailable_reasons(
        office_ok=True, pdf_to_word_ok=False, heif_decode_ok=True, heif_encode_ok=True
    )
    assert len(reasons) == 1
    assert "PDF 转 Word" in reasons[0] and "TXT 转 Word" in reasons[0]


def test_markup_render_target_map_covers_the_text_family() -> None:
    """两个家族共用一份「目标 -> 渲染目标」的表，不能各留一份。"""
    from services.markup_service import MARKUP_RENDER_TARGETS

    assert MARKUP_RENDER_TARGETS[registry.TARGET_MD] == markup_render.OUTPUT_MARKDOWN
    assert MARKUP_RENDER_TARGETS[registry.TARGET_HTML] == markup_render.OUTPUT_HTML
    assert MARKUP_RENDER_TARGETS[registry.TARGET_TXT] == markup_render.OUTPUT_TEXT


def test_text_docx_options_are_font_only() -> None:
    """``docx_writer`` 只设东亚字体，**字号没有实现路径** —— 不给假控件。"""
    entry = registry.CAPABILITY_BY_PAIR[(registry.SOURCE_TXT, registry.TARGET_DOCX)]
    assert [spec.key for spec in entry.options] == ["font"]


def test_text_html_and_md_have_no_options() -> None:
    """页面大小、方向、字体都是排版选项，HTML / Markdown 里没有「页」。"""
    for target in (registry.TARGET_HTML, registry.TARGET_MD):
        entry = registry.CAPABILITY_BY_PAIR[(registry.SOURCE_TXT, target)]
        assert entry.options == (), target


def test_docx_writer_stays_out_of_the_module_import_graph() -> None:
    """``office.docx_writer`` **不能**在 ``text_service`` 顶层被 import。

    少了 python-docx 的话，模块级 import 会让整个应用起不来，
    「缺组件时其它功能照常可用」就成了空话。
    """
    import subprocess
    import sys

    code = (
        "import sys; import services.text_service; "
        "assert 'office.docx_writer' not in sys.modules, '被顶层 import 了'; "
        "assert 'docx' not in sys.modules, 'python-docx 被顶层 import 了'"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(registry.__file__).rsplit("conversion", 1)[0],
    )
    assert done.returncode == 0, done.stderr


def test_render_markdown_is_reachable_only_through_txt_today() -> None:
    """今天只有 TXT→MD 产出 Markdown —— 上面那些转义断言的适用面。

    将来 MD→MD 或 HTML→MD 登记进来时，这条会红，提醒把往返测试
    扩到那条新路径上，而不是让新路径悄悄绕过行首转义。
    """
    producers = [
        entry.id
        for entry in registry.CAPABILITIES
        if entry.target_type == registry.TARGET_MD
    ]
    assert producers == ["document.txt-to-md"]


def test_no_markup_note_is_honest_about_what_is_missing() -> None:
    assert "正文段落" in text_service.NO_MARKUP_NOTE
    assert "标题" in text_service.NO_MARKUP_NOTE
