"""HTML / Markdown 的安全边界。

**这个文件是先写的，实现后补。** HTML→PDF 是本阶段唯一一个「用户提供的
内容会被排版引擎拿去解析」的入口，PyMuPDF 的 ``Story`` 默认具备解析外部
资源的能力 —— 不把这条路堵死，一个 ``<img src="file:///C:/...">``
就可能把服务器上的文件读进产物里。所以先把攻击面钉成测试，再写实现。

分七组：

    A 段  危险标签的**内容**不许变成正文（script / style / head）
    B 段  活动标签整体丢弃（iframe / object / embed / form / link / meta …）
    C 段  资源引用只认 ``data:`` —— 外链、``file:``、``srcset`` 一律剥离
    D 段  超链接地址按协议过滤，文字保留
    E 段  字数和节点数上限
    F 段  渲染产物里不许出现任何外部引用、不许出现原样的属性
    G 段  端到端：喂 ``file:///etc/passwd``，产物里不许有泄漏
"""

from __future__ import annotations

import base64

import pymupdf
import pytest

from config import settings
from office import markup_parse, markup_render
from office.html_to_pdf import build_pdf_from_blocks
from office.txt_to_pdf import TxtOptions
from utils.errors import ValidationError

#: 产物里绝不能出现的东西（与前几个阶段同一份精神：不泄漏、不联网）
FORBIDDEN_IN_OUTPUT = (
    "http://",
    "https://",
    "file://",
    "<script",
    "<iframe",
    "<object",
    "<embed",
    "<link",
    "@import",
    "javascript:",
    "onerror",
    "onclick",
    "srcset",
)

#: ``file:///etc/passwd`` 的真实内容特征。产物里出现它就说明文件被读进去了。
PASSWD_FRAGMENT = "root:"


def data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def png_data_uri(width: int = 40, height: int = 30) -> str:
    from PIL import Image

    import io

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return data_uri(buffer.getvalue())


def html_document(body: str) -> str:
    return f"<!doctype html><html><head><title>t</title></head><body>{body}</body></html>"


def rendered_html(text: str, source_format: str = markup_parse.SOURCE_HTML) -> str:
    parsed = markup_parse.parse_markup(text, source_format)
    return markup_render.build_document(parsed.blocks)


def assert_no_forbidden(output: str) -> None:
    for fragment in FORBIDDEN_IN_OUTPUT:
        assert fragment not in output, f"产物里出现了 {fragment!r}"


# ----------------------------------------------------------------------
# A 段：危险标签的内容不许变成正文
# ----------------------------------------------------------------------

def test_script_content_never_becomes_text() -> None:
    parsed = markup_parse.parse_html(
        html_document("<p>正文</p><script>var secret = 42;</script>")
    )
    text = "".join(block.text for block in parsed.blocks if hasattr(block, "text"))
    assert "正文" in text
    assert "secret" not in text
    assert "42" not in text


def test_style_content_never_becomes_text() -> None:
    parsed = markup_parse.parse_html(
        html_document(
            "<style>@import url('http://evil.example/x.css'); p { color: red; }</style>"
            "<p>正文</p>"
        )
    )
    text = "".join(block.text for block in parsed.blocks if hasattr(block, "text"))
    assert "正文" in text
    assert "evil.example" not in text
    assert "color" not in text
    assert "@import" not in text


def test_head_content_never_becomes_text() -> None:
    parsed = markup_parse.parse_html(
        "<html><head><title>标题不该出现</title>"
        "<meta http-equiv='refresh' content='0;url=http://evil.example'>"
        "</head><body><p>正文</p></body></html>"
    )
    text = "".join(block.text for block in parsed.blocks if hasattr(block, "text"))
    assert "正文" in text
    assert "标题不该出现" not in text
    assert "evil.example" not in text


# ----------------------------------------------------------------------
# B 段：活动标签整体丢弃
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "snippet",
    [
        "<iframe src='http://evil.example/x'></iframe>",
        "<object data='http://evil.example/x'></object>",
        "<embed src='http://evil.example/x'>",
        "<applet code='x'></applet>",
        "<form action='http://evil.example'><input name='a'></form>",
        "<button onclick='x()'>点我</button>",
        "<link rel='stylesheet' href='http://evil.example/x.css'>",
        "<base href='http://evil.example/'>",
        "<svg><image href='http://evil.example/x.png'/></svg>",
        "<video src='http://evil.example/x.mp4'></video>",
        "<audio src='http://evil.example/x.mp3'></audio>",
        "<noscript><img src='http://evil.example/x.png'></noscript>",
    ],
)
def test_active_tags_leave_nothing_behind(snippet: str) -> None:
    output = rendered_html(html_document(f"<p>正文</p>{snippet}"))
    assert "正文" in output
    assert_no_forbidden(output)


def test_iframe_content_is_not_kept_as_text() -> None:
    parsed = markup_parse.parse_html(
        html_document("<iframe>里面的字</iframe><p>正文</p>")
    )
    text = "".join(block.text for block in parsed.blocks if hasattr(block, "text"))
    assert "正文" in text
    assert "里面的字" not in text


# ----------------------------------------------------------------------
# C 段：资源引用只认 data:
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "src",
    [
        "file:///etc/passwd",
        "file:///C:/Windows/win.ini",
        "http://evil.example/x.png",
        "https://evil.example/x.png",
        "//evil.example/x.png",
        "../../../etc/passwd",
        "C:\\Windows\\win.ini",
        "ftp://evil.example/x.png",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    ],
)
def test_image_references_other_than_data_are_dropped(src: str) -> None:
    parsed = markup_parse.parse_html(html_document(f"<img src='{src}'>"))
    assert not [b for b in parsed.blocks if isinstance(b, markup_parse.ImageBlock)]
    assert parsed.notes, "剥离了图片就必须如实说明，不能悄悄丢"
    assert_no_forbidden(rendered_html(html_document(f"<img src='{src}'>")))


def test_data_uri_image_is_kept() -> None:
    parsed = markup_parse.parse_html(
        html_document(f"<p>配图</p><img src='{png_data_uri(40, 30)}'>")
    )
    images = [b for b in parsed.blocks if isinstance(b, markup_parse.ImageBlock)]
    assert len(images) == 1
    assert images[0].data.startswith(b"\x89PNG")
    assert not parsed.notes


def test_srcset_is_ignored_entirely() -> None:
    parsed = markup_parse.parse_html(
        html_document(
            f"<img src='{png_data_uri()}' "
            "srcset='http://evil.example/big.png 2x, http://evil.example/small.png 1x'>"
        )
    )
    images = [b for b in parsed.blocks if isinstance(b, markup_parse.ImageBlock)]
    assert len(images) == 1
    assert_no_forbidden(rendered_html(
        html_document(f"<img src='{png_data_uri()}' srcset='http://evil.example/big.png 2x'>")
    ))


def test_oversized_data_uri_is_dropped() -> None:
    import io
    import os

    from PIL import Image

    # 随机像素不可压缩，PNG 出来就是原始大小 —— 这样素材体积可控。
    # （用渐变/噪点图会踩坑：它们压完比上限还小，测试会因为「素材不够大」
    #   而失败，而不是因为逻辑不对。）
    size = 300
    buffer = io.BytesIO()
    Image.frombytes("RGB", (size, size), os.urandom(size * size * 3)).save(
        buffer, format="PNG"
    )
    payload = buffer.getvalue()

    # 两个边界都要卡住，缺一这条测试就测错了东西：
    # 图必须**超过单张上限**，同时整份文档的 base64 必须**还在长度上限之内** ——
    # 否则拦住它的是长度上限，单张上限是不是生效根本没被验到。
    assert len(payload) > settings.MAX_MARKUP_IMAGE_BYTES
    uri = data_uri(payload)
    assert len(uri) < settings.MAX_MARKUP_CHARS

    parsed = markup_parse.parse_html(html_document(f"<img src='{uri}'><p>正文</p>"))
    assert not [b for b in parsed.blocks if isinstance(b, markup_parse.ImageBlock)]
    assert parsed.notes, "丢了一张图就必须说明"
    text = "".join(block.text for block in parsed.blocks if hasattr(block, "text"))
    assert "正文" in text, "一张图太大不该把整份文档一起废掉"


def test_too_many_images_is_rejected() -> None:
    uri = png_data_uri(4, 4)
    body = "".join(f"<img src='{uri}'>" for _ in range(settings.MAX_MARKUP_IMAGES + 1))
    with pytest.raises(ValidationError):
        markup_parse.parse_html(html_document(body))


def test_declared_dimensions_from_the_file_win_over_attributes() -> None:
    """``width``/``height`` 属性不可信 —— 真实尺寸只能由解码结果决定。"""
    parsed = markup_parse.parse_html(
        html_document(
            f"<img src='{png_data_uri(40, 30)}' width='99999' height='99999'>"
        )
    )
    images = [b for b in parsed.blocks if isinstance(b, markup_parse.ImageBlock)]
    assert len(images) == 1
    assert images[0].width_pt > 0
    # 40x30 的图按 96 DPI 折算是 30x22.5 磅上下，绝不该是 99999
    assert images[0].width_pt < 100


# ----------------------------------------------------------------------
# D 段：超链接按协议过滤
# ----------------------------------------------------------------------

def test_local_and_script_links_are_stripped_but_text_survives() -> None:
    parsed = markup_parse.parse_html(
        html_document(
            "<p><a href='file:///etc/passwd'>点这里</a>"
            "<a href='javascript:alert(1)'>还有这里</a></p>"
        )
    )
    text = "".join(block.text for block in parsed.blocks if hasattr(block, "text"))
    assert "点这里" in text
    assert "还有这里" in text
    for run in parsed.blocks[0].runs:
        assert run.link is None
    assert parsed.notes


def test_ordinary_links_are_preserved() -> None:
    parsed = markup_parse.parse_html(
        html_document("<p><a href='https://example.com/a?b=1'>示例</a></p>")
    )
    runs = [run for run in parsed.blocks[0].runs if run.link]
    assert runs and runs[0].link == "https://example.com/a?b=1"
    # 链接是给人点的，不是给排版引擎抓的资源 —— 产物里保留它不算「外部引用」，
    # 但绝不能出现 <link> / url() / srcset 这类会真的去取的东西。
    output = markup_render.build_document(parsed.blocks)
    assert "<link" not in output
    assert "url(" not in output
    assert "srcset" not in output


# ----------------------------------------------------------------------
# E 段：上限
# ----------------------------------------------------------------------

def test_too_long_html_is_rejected() -> None:
    body = "<p>" + ("字" * (settings.MAX_MARKUP_CHARS + 10)) + "</p>"
    with pytest.raises(ValidationError):
        markup_parse.parse_html(body)


def test_too_long_markdown_is_rejected() -> None:
    text = "字" * (settings.MAX_MARKUP_CHARS + 10)
    with pytest.raises(ValidationError):
        markup_parse.parse_markdown(text)


def test_too_many_nodes_is_rejected() -> None:
    body = "<div>" * (settings.MAX_MARKUP_NODES + 10) + "x" + "</div>" * (
        settings.MAX_MARKUP_NODES + 10
    )
    with pytest.raises(ValidationError):
        markup_parse.parse_html(body)


def test_the_length_limit_counts_characters_not_bytes() -> None:
    """上限按字符数算：中文一个字三个字节，按字节算会平白少掉三分之二配额。"""
    limit = settings.MAX_MARKUP_CHARS
    just_under = "<p>" + ("字" * (limit - 10)) + "</p>"
    assert len(just_under.encode("utf-8")) > limit
    parsed = markup_parse.parse_html(just_under)
    assert parsed.blocks


# ----------------------------------------------------------------------
# F 段：渲染产物
# ----------------------------------------------------------------------

def test_rendered_document_never_copies_attributes() -> None:
    output = rendered_html(
        html_document(
            "<p class='x' id='y' style='background:url(http://evil.example/a.png)' "
            "onmouseover='steal()' data-whatever='1'>正文</p>"
        )
    )
    assert "正文" in output
    assert_no_forbidden(output)
    for attribute in ("class=", "id=", "data-whatever", "onmouseover", "style="):
        assert attribute not in output


def test_rendered_document_escapes_literal_markup_in_text() -> None:
    from office.document_ir import Paragraph, TextRun

    blocks = [Paragraph(runs=[TextRun(text="5 < 6 && 7 > 3 <b>不是粗体</b>")])]
    output = markup_render.build_document(blocks)
    assert "&lt;b&gt;" in output
    assert "<b>不是粗体" not in output


def test_markdown_script_tag_is_treated_as_plain_text() -> None:
    """Markdown 子集里没有「原样透传 HTML」这一条 —— 尖括号就是普通字符。"""
    parsed = markup_parse.parse_markdown("正文\n\n<script>alert(1)</script>\n")
    text = "\n".join(block.text for block in parsed.blocks if hasattr(block, "text"))
    assert "alert(1)" in text
    output = markup_render.build_document(parsed.blocks)
    assert "<script" not in output
    assert "&lt;script&gt;" in output


# ----------------------------------------------------------------------
# G 段：端到端 —— file:///etc/passwd 必须够不着
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "hostile",
    [
        "<img src='file:///etc/passwd'>",
        "<img src='file:///C:/Windows/win.ini'>",
        "<a href='file:///etc/passwd'>读我</a>",
        "<iframe src='file:///etc/passwd'></iframe>",
        "<object data='file:///etc/passwd'></object>",
        "<link rel='stylesheet' href='file:///etc/passwd'>",
        "<style>@import url('file:///etc/passwd');</style>",
        "<style>body{background:url(file:///etc/passwd)}</style>",
    ],
)
def test_pdf_never_pulls_in_local_files(hostile: str) -> None:
    parsed = markup_parse.parse_html(html_document(f"<p>正文</p>{hostile}"))
    result = build_pdf_from_blocks(parsed.blocks, TxtOptions())

    doc = pymupdf.open(stream=result.data, filetype="pdf")
    try:
        text = "".join(page.get_text() for page in doc)
        assert "正文" in text
        assert PASSWD_FRAGMENT not in text
        assert "win.ini" not in text
    finally:
        doc.close()


def test_pdf_output_carries_no_remote_reference() -> None:
    body = (
        "<p>正文</p>"
        "<img src='http://evil.example/x.png'>"
        "<a href='http://evil.example/page'>链接</a>"
        "<style>@import url('http://evil.example/x.css');</style>"
    )
    parsed = markup_parse.parse_html(html_document(body))
    result = build_pdf_from_blocks(parsed.blocks, TxtOptions())

    doc = pymupdf.open(stream=result.data, filetype="pdf")
    try:
        for page in doc:
            for link in page.get_links():
                uri = link.get("uri", "")
                assert "file:" not in uri
                assert "javascript:" not in uri
    finally:
        doc.close()
