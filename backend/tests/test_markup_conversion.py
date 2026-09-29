"""HTML / Markdown → PDF / HTML / Markdown / 纯文本的功能测试。

安全边界在 ``test_markup_security.py`` 里，这里管的是「转换结果对不对」：
结构有没有认出来、产物能不能真打开、内容是不是原样在里面、说明是不是实话。

每一处判定都落在**真产物**上：PDF 用 PyMuPDF 打开抽文字、数图片，
HTML 检查结构标签，Markdown 再解析一次做往返。只看「函数没抛异常」
不算验过。
"""

from __future__ import annotations

import base64
import io

import pymupdf
import pytest
from PIL import Image

from config import settings
from office import markup_parse, markup_render
from office.document_ir import (
    ImageBlock,
    Paragraph,
    PARA_KIND_CODE,
    PARA_KIND_LIST,
    PARA_KIND_QUOTE,
    PARA_KIND_RULE,
    TableBlock,
    TextRun,
)
from office.html_to_pdf import STYLE_SUBSET_NOTE, build_pdf_from_blocks
from office.txt_to_pdf import TxtOptions

SAMPLE_HTML = """<!doctype html>
<html><head><title>样张</title></head>
<body>
<h1>一级标题</h1>
<h2>二级标题</h2>
<p>正文一段，含 <b>粗体</b>、<i>斜体</i>、<code>行内代码</code>
和 <a href="https://example.com/x">一个链接</a>。</p>
<ul><li>无序甲</li><li>无序乙<ul><li>嵌套丙</li></ul></li></ul>
<ol><li>有序一</li><li>有序二</li></ol>
<blockquote>引用一行<br>引用第二行</blockquote>
<pre>def f():
    return 1</pre>
<hr>
<table><tr><th>姓名</th><th>分数</th></tr>
<tr><td>甲</td><td>90</td></tr><tr><td>乙</td><td>85</td></tr></table>
</body></html>"""

SAMPLE_MARKDOWN = """# 一级标题

## 二级标题

正文一段，含 **粗体**、*斜体*、`行内代码` 和 [一个链接](https://example.com/x)。

- 无序甲
- 无序乙
    - 嵌套丙

1. 有序一
2. 有序二

> 引用一行
> 引用第二行

```
def f():
    return 1
```

---

| 姓名 | 分数 |
| --- | --- |
| 甲 | 90 |
| 乙 | 85 |
"""


def png_data_uri(width: int = 60, height: int = 40) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 40, 40)).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def paragraphs(blocks) -> list[Paragraph]:
    return [b for b in blocks if isinstance(b, Paragraph)]


def texts(blocks) -> list[str]:
    return [b.text for b in paragraphs(blocks)]


def open_pdf(data: bytes) -> pymupdf.Document:
    return pymupdf.open(stream=data, filetype="pdf")


def pdf_text(data: bytes) -> str:
    doc = open_pdf(data)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


# ----------------------------------------------------------------------
# A 段：结构认得对不对
# ----------------------------------------------------------------------

def test_html_structure_is_recognised() -> None:
    parsed = markup_parse.parse_html(SAMPLE_HTML)
    blocks = parsed.blocks

    levels = [b.level for b in paragraphs(blocks) if b.level]
    assert levels == [1, 2], "两个标题的级别要认出来"

    first = paragraphs(blocks)[2]
    assert "粗体" in first.text and "斜体" in first.text
    assert any(run.bold for run in first.runs)
    assert any(run.italic for run in first.runs)
    assert any(run.code for run in first.runs)
    assert any(run.link == "https://example.com/x" for run in first.runs)


def test_nested_lists_keep_their_depth_and_numbering() -> None:
    parsed = markup_parse.parse_html(SAMPLE_HTML)
    items = [b for b in paragraphs(parsed.blocks) if b.kind == PARA_KIND_LIST]

    assert [i.text for i in items] == ["无序甲", "无序乙", "嵌套丙", "有序一", "有序二"]
    assert [i.indent for i in items] == [0, 0, 1, 0, 0]
    assert [i.marker for i in items] == [
        markup_parse.BULLET_MARKER,
        markup_parse.BULLET_MARKER,
        markup_parse.BULLET_MARKER,
        "1.",
        "2.",
    ]


def test_quote_code_rule_and_table_are_recognised() -> None:
    parsed = markup_parse.parse_html(SAMPLE_HTML)
    kinds = [b.kind for b in paragraphs(parsed.blocks)]
    assert PARA_KIND_QUOTE in kinds
    assert PARA_KIND_CODE in kinds
    assert PARA_KIND_RULE in kinds
    # <br> 在引用里是**换行**，不是空格
    quote = next(b for b in paragraphs(parsed.blocks) if b.kind == PARA_KIND_QUOTE)
    assert quote.text == "引用一行\n引用第二行"

    tables = [b for b in parsed.blocks if isinstance(b, TableBlock)]
    assert len(tables) == 1
    assert tables[0].rows == [["姓名", "分数"], ["甲", "90"], ["乙", "85"]]


def test_markdown_structure_matches_the_html_one() -> None:
    """同一份内容，两种源格式要认出同一套结构 —— 否则两条路会各长各的。"""
    from_html = markup_parse.parse_html(SAMPLE_HTML)
    from_markdown = markup_parse.parse_markdown(SAMPLE_MARKDOWN)

    assert texts(from_markdown.blocks) == texts(from_html.blocks)
    assert [b.kind for b in paragraphs(from_markdown.blocks)] == [
        b.kind for b in paragraphs(from_html.blocks)
    ]
    assert [b.level for b in paragraphs(from_markdown.blocks)] == [
        b.level for b in paragraphs(from_html.blocks)
    ]


def test_unsupported_markdown_is_kept_as_plain_text() -> None:
    """子集之外的东西**不许消失** —— 认不出来就当普通文字，这是底线。"""
    parsed = markup_parse.parse_markdown("正文\n\n~~删除线~~ 和 $E=mc^2$ 和 <!-- 注释 -->\n")
    body = "\n".join(texts(parsed.blocks))
    assert "~~删除线~~" in body
    assert "$E=mc^2$" in body
    assert "注释" in body


# ----------------------------------------------------------------------
# B 段：HTML / Markdown 目标
# ----------------------------------------------------------------------

def test_html_output_is_a_standalone_document() -> None:
    parsed = markup_parse.parse_html(SAMPLE_HTML)
    document = markup_render.build_document(parsed.blocks)

    assert document.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in document
    for tag in ("<h1>", "<h2>", "<blockquote", "<table>", "<hr>", '<p class="list-item"'):
        assert tag in document, f"产出的网页里没有 {tag}"
    # 标明了「重新排版」这件事，不是把原文件换个壳
    assert "font-family" in document


def test_markdown_output_uses_markdown_bullets_and_round_trips() -> None:
    parsed = markup_parse.parse_html(SAMPLE_HTML)
    markdown = markup_render.render_markdown(parsed.blocks)

    assert "- 无序甲" in markdown, "无序列表必须用 Markdown 的 - 而不是圆点字符"
    assert "1. 有序一" in markdown
    assert "# 一级标题" in markdown
    assert "> 引用一行" in markdown

    again = markup_parse.parse_markdown(markdown)
    assert texts(again.blocks) == texts(parsed.blocks)
    assert [b.kind for b in paragraphs(again.blocks)] == [
        b.kind for b in paragraphs(parsed.blocks)
    ]


def test_text_output_keeps_the_structure_visible() -> None:
    parsed = markup_parse.parse_markdown(SAMPLE_MARKDOWN)
    text = markup_render.render_text(parsed.blocks)

    assert "一级标题" in text
    assert "• 无序甲" in text
    assert "> 引用一行" in text
    assert "-" * 20 in text
    assert "姓名 | 分数" in text, "表格按行铺开，不能整张丢掉"


# ----------------------------------------------------------------------
# C 段：PDF —— 真打开、真抽文字、真数图片
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "source, kind",
    [(SAMPLE_HTML, markup_parse.SOURCE_HTML), (SAMPLE_MARKDOWN, markup_parse.SOURCE_MARKDOWN)],
)
def test_pdf_really_contains_the_document(source: str, kind: str) -> None:
    parsed = markup_parse.parse_markup(source, kind)
    result = build_pdf_from_blocks(parsed.blocks, TxtOptions())

    assert result.page_count >= 1
    text = pdf_text(result.data)

    for fragment in (
        "一级标题",
        "二级标题",
        "正文一段",
        "粗体",
        "斜体",
        "行内代码",
        "一个链接",
        "无序甲",
        "嵌套丙",
        "有序一",
        "引用一行",
        "def f():",
        "姓名",
        "90",
    ):
        assert fragment in text, f"PDF 里没有 {fragment!r}"


def test_pdf_lists_and_quotes_carry_their_markers() -> None:
    parsed = markup_parse.parse_markdown("- 甲\n- 乙\n\n> 引用\n")
    result = build_pdf_from_blocks(parsed.blocks, TxtOptions())
    text = pdf_text(result.data)

    assert "• 甲" in text and "• 乙" in text
    assert "引用" in text


def test_pdf_embeds_inline_images_for_real() -> None:
    parsed = markup_parse.parse_html(
        f"<p>配图</p><img src='{png_data_uri(120, 90)}'>"
    )
    assert [b for b in parsed.blocks if isinstance(b, ImageBlock)]
    result = build_pdf_from_blocks(parsed.blocks, TxtOptions())

    doc = open_pdf(result.data)
    try:
        images = doc[0].get_images()
        assert images, "PDF 里必须真的嵌了一张图，不能只有空段落"
        # 60x40 之外的尺寸要能对上：120x90 的图不该被写成别的尺寸
        info = doc.extract_image(images[0][0])
        with Image.open(io.BytesIO(info["image"])) as rendered:
            assert rendered.size == (120, 90)
    finally:
        doc.close()


def test_pdf_is_subset_not_a_whole_font_dump() -> None:
    """``subset_fonts()`` 是必须的：一页中文不子集化 **18.3 MB**。

    这条是那条要求的守门员 —— 不子集化的话产物会大三个数量级，
    而功能测试全都还会通过（打开没问题、文字也在）。
    """
    parsed = markup_parse.parse_html(SAMPLE_HTML)
    result = build_pdf_from_blocks(parsed.blocks, TxtOptions())
    assert len(result.data) < 300_000, (
        f"PDF 有 {len(result.data)} 字节 —— 字体没子集化（不子集化约 18 MB）"
    )


def test_pdf_notes_say_what_was_preserved() -> None:
    parsed = markup_parse.parse_html(SAMPLE_HTML)
    result = build_pdf_from_blocks(parsed.blocks, TxtOptions())

    assert result.notes, "结果说明不能是空的"
    joined = "\n".join(result.notes)
    assert STYLE_SUBSET_NOTE in joined
    assert "页" in joined
    # 说明里不许出现路径、内部模块名这类东西
    for fragment in ("Traceback", "site-packages", "C:\\", "/tmp/"):
        assert fragment not in joined


def test_pdf_paginates_a_long_document() -> None:
    body = "".join(f"<p>第 {index} 段正文，用来把文档撑到多页。</p>" for index in range(200))
    parsed = markup_parse.parse_html(f"<html><body>{body}</body></html>")
    result = build_pdf_from_blocks(parsed.blocks, TxtOptions())

    assert result.page_count > 1
    text = pdf_text(result.data)
    assert "第 0 段" in text
    assert "第 199 段" in text, "最后一页的内容也得在"


def test_pdf_honours_page_size_and_orientation() -> None:
    parsed = markup_parse.parse_markdown("正文\n")
    landscape = build_pdf_from_blocks(
        parsed.blocks, TxtOptions(page_size="a5", orientation="landscape")
    )
    doc = open_pdf(landscape.data)
    try:
        rect = doc[0].rect
        assert rect.width > rect.height, "横向 A5 的宽必须大于高"
        assert abs(rect.width - settings.PDF_PAGE_SIZES["a5"][1]) < 1
    finally:
        doc.close()


# ----------------------------------------------------------------------
# D 段：说明是实话
# ----------------------------------------------------------------------

def test_dropped_images_are_disclosed_with_a_count() -> None:
    parsed = markup_parse.parse_html(
        "<p>正文</p>"
        "<img src='http://evil.example/a.png'>"
        "<img src='http://evil.example/b.png'>"
        "<img src='file:///etc/passwd'>"
    )
    assert len(parsed.notes) == 1
    assert "3 张图片" in parsed.notes[0]
    assert "内嵌" in parsed.notes[0]


def test_plain_text_target_discloses_that_images_are_missing() -> None:
    parsed = markup_parse.parse_html(f"<p>正文</p><img src='{png_data_uri()}'>")
    notes = markup_render.notes_for_target(parsed.blocks, "text")
    assert notes and "1 张图片" in notes[0]

    # 但 HTML / Markdown 带得走图片，就不该说这句假话
    assert markup_render.notes_for_target(parsed.blocks, "html") == []
    assert markup_render.notes_for_target(parsed.blocks, "markdown") == []


def test_no_notes_when_nothing_was_dropped() -> None:
    parsed = markup_parse.parse_html(f"<p>正文</p><img src='{png_data_uri()}'>")
    assert parsed.notes == []


# ----------------------------------------------------------------------
# E 段：坏输入不炸
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "source",
    [
        "",
        "   ",
        "<html>",
        "</p></div>",
        "<p>未闭合",
        "<table><tr><td>没有结束",
        "<b><i>交错</b></i>",
        "<p>只有标签没有文字</p>",
        "<!-- 只有注释 -->",
    ],
)
def test_malformed_input_never_crashes(source: str) -> None:
    parsed = markup_parse.parse_html(source)
    # 不抛异常是底线；产物要么空要么是能渲染的东西
    markup_render.build_document(parsed.blocks)
    markup_render.render_markdown(parsed.blocks)
    markup_render.render_text(parsed.blocks)


@pytest.mark.parametrize("source", ["", "   ", "\n\n\n", "# ", "- ", "> ", "```\n```\n"])
def test_malformed_markdown_never_crashes(source: str) -> None:
    parsed = markup_parse.parse_markdown(source)
    markup_render.build_document(parsed.blocks)
    markup_render.render_markdown(parsed.blocks)
    markup_render.render_text(parsed.blocks)


def test_empty_document_still_produces_a_valid_pdf() -> None:
    result = build_pdf_from_blocks([], TxtOptions())
    doc = open_pdf(result.data)
    try:
        assert doc.page_count == 1
    finally:
        doc.close()


def test_unknown_source_format_is_rejected() -> None:
    from utils.errors import ValidationError

    with pytest.raises(ValidationError):
        markup_parse.parse_markup("<p>x</p>", "docx")


def test_unknown_render_target_is_rejected() -> None:
    with pytest.raises(ValueError):
        markup_render.render_markup([], "pdf")


# ----------------------------------------------------------------------
# F 段：run 的合并与样式
# ----------------------------------------------------------------------

def test_adjacent_runs_with_the_same_style_are_merged() -> None:
    parsed = markup_parse.parse_html("<p>a<span>b</span><em>c</em>d</p>")
    runs = paragraphs(parsed.blocks)[0].runs
    # a + b 同样式合成一段；c 是斜体；d 又回到普通
    assert [run.text for run in runs] == ["ab", "c", "d"]
    assert [run.italic for run in runs] == [False, True, False]


def test_preformatted_text_keeps_its_whitespace() -> None:
    parsed = markup_parse.parse_html("<pre>a  b\n  c</pre>")
    block = next(b for b in paragraphs(parsed.blocks) if b.kind == PARA_KIND_CODE)
    assert block.text == "a  b\n  c", "<pre> 里的空格和换行一个都不能动"


def test_code_block_is_not_wrapped_in_code_twice() -> None:
    """代码块只该有一层 ``<code>``。

    曾经出来的是 ``<pre><code><code>code block</code></code></pre>``：
    解析 Markdown 的围栏时把整块正文标成了**行内**代码，渲染时外层
    ``<pre><code>`` 又包了一次。源 HTML 写成 ``<pre><code>x</code></pre>``
    时同样会多一层 —— 块本身已经说明是代码，里面再包一个是多余的。
    """
    fenced = markup_parse.parse_markdown("```\ncode block\n```\n")
    html = markup_render.render_html(fenced.blocks)
    assert "<pre><code>code block</code></pre>" in html, html
    assert "<code><code>" not in html, f"代码块被包了两层：{html}"

    nested = markup_parse.parse_html("<pre><code>x = 1</code></pre>")
    html = markup_render.render_html(nested.blocks)
    assert "<pre><code>x = 1</code></pre>" in html, html
    assert "<code><code>" not in html, f"源里本来就有的 <code> 又叠了一层：{html}"


def test_code_block_content_is_literal() -> None:
    """代码块里的字符就是字符：``**`` 不是加粗，``<`` 要转义但不该变成标签。"""
    parsed = markup_parse.parse_markdown("```\na ** b < c\n```\n")
    html = markup_render.render_html(parsed.blocks)
    assert "a ** b &lt; c" in html, html
    assert "<strong>" not in html, f"代码块里出现了强调标签：{html}"


def test_whitespace_in_normal_text_is_collapsed() -> None:
    parsed = markup_parse.parse_html("<p>甲\n\n   乙\t丙</p>")
    assert paragraphs(parsed.blocks)[0].text == "甲 乙 丙"


def test_empty_paragraphs_are_dropped_but_rules_are_not() -> None:
    parsed = markup_parse.parse_html("<p> </p><p>\n</p><hr><p>正文</p>")
    kinds = [b.kind for b in paragraphs(parsed.blocks)]
    assert kinds == [PARA_KIND_RULE, "body"], "空段落是噪音，分隔线是内容"


def test_text_run_defaults_are_backward_compatible() -> None:
    """第七阶段那条路构造 run / 段落时只给老字段，新字段必须有默认值。"""
    run = TextRun(text="x", bold=True)
    assert run.link is None and run.code is False

    block = Paragraph(runs=[run], level=1, align="center")
    assert block.kind == "body"
    assert block.marker is None
    assert block.indent == 0
    assert block.text == "x"
