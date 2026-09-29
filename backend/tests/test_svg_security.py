"""SVG 的安全边界（第十阶段 A §九–§十五）。

SVG 与 PDF、HTML 一样是**活动内容**而不是图片：一份 SVG 里可以写
``<script>``、可以 ``<image href="file:///C:/...">``、可以
``<image href="http://内网地址/">``。后端一旦把它交给渲染器，
上传者就同时得到了「读服务器本地文件」与「让服务器替他发请求」两个入口。

所以那个文件按与 ``tests/test_markup_security.py`` 同一份精神组织：先钉攻击面，
再实现。八组：

    A 段  危险标签**连内容一起**消失（script / foreignObject / iframe / object / embed）
    B 段  事件属性与危险属性一律删除
    C 段  引用只认 ``#片段`` 与安全的内联栅格图 —— 网络、``file:``、协议相对地址全部剥离
    D 段  ``<style>`` 与 ``style=""`` 里的 ``@import`` / ``url()`` / ``expression()``
    E 段  根元素必须是 ``svg``（HTML 改名成 ``.svg`` 照渲染器不误，这层是唯一识破它的地方）
    F 段  DTD / 实体 / 空文件 / 体积上限 / 节点数上限
    G 段  **真机证据**：被引用的本地文件内容既没进产物字节、也没进任何一个像素
    H 段  **真机证据**：被引用的网络地址没有产生任何一次真实请求
"""

from __future__ import annotations

import http.server
import io
import threading
from pathlib import Path

import pytest
from PIL import Image

from compressors import svg as svg_codec
from config import settings
from utils.errors import CorruptedFileError, UnsupportedTypeError, ValidationError

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"

#: 产物里绝不能出现的东西。``secret.png`` / ``evil.example`` 这些名字是
#: 下面各条用例自己放进去的「探针」，出现即说明引用没被真的切断。
FORBIDDEN_IN_OUTPUT = (
    "http://",
    "https://",
    "file://",
    "javascript:",
    "vbscript:",
    "expression(",
    "-moz-binding",
    "@import",
    "<script",
    "foreignObject",
    "<iframe",
    "<object",
    "<embed",
    "onload",
    "onclick",
)


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------

def svg_document(body: str, *, attrs: str = 'width="100" height="100"') -> bytes:
    """一份最小的、结构合法的 SVG。"""
    return (
        f'<svg xmlns="{SVG_NAMESPACE}" xmlns:xlink="{XLINK_NAMESPACE}" '
        f"{attrs}>{body}</svg>"
    ).encode("utf-8")


def sanitize(text: bytes) -> tuple[bytes, list[str]]:
    return svg_codec.sanitize_svg(text)


def sanitized(text: bytes) -> str:
    return sanitize(text)[0].decode("utf-8")


#: 命名空间声明本身**就应该**含 ``http://`` —— 那是 W3C 的固定标识符，
#: 不是一次引用。扫描前先把它抠掉，否则每一份正常 SVG 都会被判为不合格。
_NAMESPACE_DECLS = (
    f'xmlns="{SVG_NAMESPACE}"',
    f'xmlns:xlink="{XLINK_NAMESPACE}"',
)


def assert_no_forbidden(output: str) -> None:
    scrubbed = output
    for decl in _NAMESPACE_DECLS:
        scrubbed = scrubbed.replace(decl, "")
    for fragment in FORBIDDEN_IN_OUTPUT:
        assert fragment not in scrubbed, f"净化后的 SVG 里还留着 {fragment!r}"


def marker_png(color: tuple[int, int, int] = (255, 0, 255)) -> bytes:
    """一张颜色极其扎眼的小图 —— 一旦被读进去画出来，逐像素能看出来。"""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


# ----------------------------------------------------------------------
# A 段：危险标签连内容一起消失
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [
        '<script>alert("xss")</script>',
        '<script type="text/javascript" href="evil.js"/>',
        "<foreignObject><body><p>内嵌文档</p></body></foreignObject>",
        '<iframe src="http://evil.example/"></iframe>',
        '<object data="http://evil.example/"></object>',
        '<embed src="http://evil.example/"/>',
        '<canvas width="10" height="10"></canvas>',
        '<video src="http://evil.example/v.mp4"></video>',
        '<audio src="http://evil.example/a.mp3"></audio>',
        '<annotation-xml encoding="text/html"><p>x</p></annotation-xml>',
        '<handler event="load">alert(1)</handler>',
    ],
)
def test_forbidden_tags_leave_nothing_behind(body: str) -> None:
    output = sanitized(svg_document(f'<rect width="10" height="10"/>{body}'))
    assert_no_forbidden(output)
    for fragment in ("xss", "内嵌文档", "evil.example", "alert"):
        assert fragment not in output, f"被删标签的内容泄漏了：{fragment!r}"


def test_script_removal_is_reported() -> None:
    _, notes = sanitize(svg_document('<script>alert(1)</script>'))
    assert svg_codec.SOCKET_NOTE in notes


def test_foreign_object_removal_has_its_own_note() -> None:
    _, notes = sanitize(svg_document("<foreignObject><p>x</p></foreignObject>"))
    assert svg_codec.FOREIGN_NOTE in notes


def test_a_clean_document_produces_no_notes() -> None:
    """没有被动过就说没有 —— 报一堆「已移除」会让用户以为文件被削过。"""
    clean, notes = sanitize(svg_document('<rect width="10" height="10" fill="#f00"/>'))
    assert notes == []
    assert b"#f00" in clean


# ----------------------------------------------------------------------
# B 段：事件属性与危险属性
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "attr",
    [
        'onload="alert(1)"',
        'onclick="alert(1)"',
        'onbegin="alert(1)"',
        'onmouseover="alert(1)"',
        'ONLOAD="alert(1)"',
    ],
)
def test_event_handlers_are_removed(attr: str) -> None:
    output = sanitized(svg_document(f'<rect width="10" height="10" {attr}/>'))
    assert "alert" not in output
    assert_no_forbidden(output)


@pytest.mark.parametrize(
    ("attr", "name"),
    [
        ('src="http://evil.example/x.png"', "src"),
        ('background="http://evil.example/x.png"', "background"),
        ('externalResourcesRequired="true"', "externalResourcesRequired"),
        # XML 属性值里不允许裸 ``<``，所以探针得写成实体形式 —— 否则文件
        # 根本解析不了，测的就变成「XML 语法」而不是「属性被删掉」了。
        ('srcdoc="&lt;script&gt;alert(1)&lt;/script&gt;"', "srcdoc"),
        ('data="http://evil.example/x.svg"', "data"),
    ],
)
def test_forbidden_attributes_are_removed(attr: str, name: str) -> None:
    output = sanitized(svg_document(f'<rect width="10" height="10" {attr}/>'))
    assert f"{name}=" not in output
    assert "evil.example" not in output
    assert_no_forbidden(output)


def test_an_unrelated_attribute_survives() -> None:
    """净化不能变成「见到属性就删」——那样会把正常文件也毁掉。"""
    clean, notes = sanitize(
        svg_document('<rect width="10" height="10" fill="#0f0"/>', attrs='width="40" height="30"')
    )
    assert b'fill="#0f0"' in clean
    assert notes == []


# ----------------------------------------------------------------------
# C 段：引用
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "target",
    [
        "http://evil.example/x.png",
        "https://evil.example/x.png",
        "//evil.example/x.png",
        "file:///etc/passwd",
        "file:///C:/Windows/win.ini",
        "ftp://evil.example/x.png",
        "javascript:alert(1)",
        "vbscript:msgbox(1)",
        "data:image/svg+xml;base64,PHN2Zy8+",
        "data:text/html;base64,PHNjcmlwdD4=",
    ],
)
def test_unsafe_references_are_stripped(target: str) -> None:
    output = sanitized(
        svg_document(f'<image x="0" y="0" width="10" height="10" href="{target}"/>')
    )
    assert_no_forbidden(output)
    for fragment in ("evil.example", "passwd", "win.ini", "PHN2Zy8", "PHNjcmlwdD4"):
        assert fragment not in output


def test_xlink_href_is_checked_too() -> None:
    """``xlink:href`` 是另一条通道，只看 ``href`` 会漏掉它。"""
    output = sanitized(
        svg_document('<use xlink:href="http://evil.example/x.svg#a"/>')
    )
    assert "evil.example" not in output
    assert_no_forbidden(output)


@pytest.mark.parametrize("attr", ["fill", "stroke", "filter", "clip-path", "mask", "marker-end"])
def test_css_url_references_are_checked_in_presentation_attributes(attr: str) -> None:
    output = sanitized(
        svg_document(f'<rect width="10" height="10" {attr}="url(http://evil.example/x)"/>')
    )
    assert "evil.example" not in output
    assert_no_forbidden(output)


def test_internal_fragment_reference_survives_without_a_note() -> None:
    """``url(#grad)`` 是最常见的正常写法，对它说「已忽略外部资源」就是骗用户。"""
    clean, notes = sanitize(
        svg_document(
            '<defs><linearGradient id="g"><stop offset="0"/></linearGradient></defs>'
            '<rect width="10" height="10" fill="url(#g)"/>'
        )
    )
    assert b"url(#g)" in clean
    assert notes == []


def test_data_uri_raster_image_is_kept() -> None:
    """内联栅格图的字节就在文件里，没有 I/O，应当保留。"""
    import base64

    uri = "data:image/png;base64," + base64.b64encode(marker_png((0, 128, 0))).decode("ascii")
    clean, notes = sanitize(
        svg_document(f'<image x="0" y="0" width="100" height="100" href="{uri}"/>')
    )
    assert b"data:image/png;base64," in clean
    assert notes == [] or svg_codec.EXTERNAL_NOTE not in notes


def test_data_uri_raster_image_is_actually_drawn() -> None:
    """保留不只是「没删」——它得真的被画出来。"""
    import base64

    uri = "data:image/png;base64," + base64.b64encode(marker_png((0, 128, 0))).decode("ascii")
    clean, _ = sanitize(
        svg_document(f'<image x="0" y="0" width="100" height="100" href="{uri}"/>')
    )
    image = svg_codec.render_svg(clean).image.convert("RGB")
    colors = {color for _, color in (image.getcolors(maxcolors=1 << 20) or [])}
    assert (0, 128, 0) in colors


def test_external_reference_removal_is_reported() -> None:
    _, notes = sanitize(
        svg_document('<image x="0" y="0" width="10" height="10" href="http://evil.example/x.png"/>')
    )
    assert svg_codec.EXTERNAL_NOTE in notes


# ----------------------------------------------------------------------
# D 段：CSS
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "css",
    [
        "@import url('http://evil.example/x.css');",
        "@import 'http://evil.example/x.css';",
        "rect { fill: url(http://evil.example/x.png); }",
        "rect { background: javascript:alert(1); }",
        "rect { width: expression(alert(1)); }",
        "rect { behavior: url(#default#time2); }",
        "rect { -moz-binding: url(http://evil.example/x.xml); }",
    ],
)
def test_style_element_is_cleaned(css: str) -> None:
    output = sanitized(svg_document(f"<style>{css}</style>"))
    assert_no_forbidden(output)
    assert "evil.example" not in output


@pytest.mark.parametrize(
    "declaration",
    [
        "background:javascript:alert(1)",
        "width:expression(alert(1))",
        "-moz-binding:url(http://evil.example/x.xml)",
        "fill:url(http://evil.example/x.png)",
    ],
)
def test_style_attribute_is_cleaned(declaration: str) -> None:
    output = sanitized(
        svg_document(f'<rect width="10" height="10" style="{declaration}"/>')
    )
    assert_no_forbidden(output)
    assert "evil.example" not in output


def test_harmless_style_survives() -> None:
    clean, notes = sanitize(
        svg_document('<rect width="10" height="10" style="fill:#00f;stroke:#000"/>')
    )
    assert b"#00f" in clean
    assert b"#000" in clean
    assert notes == []


def test_a_style_emptied_by_cleaning_leaves_no_stub() -> None:
    """只删 URL 会留下 ``fill:;`` 这种残句，渲染器对残句的处理不可预期。"""
    output = sanitized(
        svg_document('<rect width="10" height="10" style="fill:url(http://evil.example/x)"/>')
    )
    assert ":;" not in output
    assert "fill:" not in output


# ----------------------------------------------------------------------
# E 段：根元素
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "document",
    [
        b"<html><body><p>hi</p></body></html>",
        b'<?xml version="1.0"?><html><body><p>hi</p></body></html>',
        b"<root><svg/></root>",
        b'<svgx xmlns="http://www.w3.org/2000/svg"/>',
        b'<svg xmlns="http://evil.example/ns"/>',
    ],
)
def test_a_non_svg_root_is_rejected(document: bytes) -> None:
    """渲染器会**照收**一份改名成 ``.svg`` 的 HTML，所以这一层不能省。"""
    assert svg_codec.looks_like_svg(document[:64]) is True  # 以 ``<`` 开头，解码器会被选中
    with pytest.raises(UnsupportedTypeError):
        sanitize(document)


@pytest.mark.parametrize(
    "document",
    [
        b'<svg width="10" height="10"/>',
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>',
        b'\xef\xbb\xbf<svg width="10" height="10"/>',
    ],
)
def test_real_svg_roots_are_accepted(document: bytes) -> None:
    """命名空间为空是合法的 —— 挡的是「不是 svg」，不是「写法不规范」。"""
    clean, _ = sanitize(document)
    assert b"svg" in clean


# ----------------------------------------------------------------------
# F 段：DTD / 实体 / 上限
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "document",
    [
        b'<!DOCTYPE svg [<!ENTITY x "y">]><svg width="10" height="10"/>',
        b'<!DOCTYPE svg SYSTEM "http://evil.example/x.dtd"><svg width="10" height="10"/>',
        b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/x.dtd">'
        b'<svg width="10" height="10"/>',
    ],
)
def test_doctype_is_rejected(document: bytes) -> None:
    """DTD 是实体膨胀的载体，而 SVG 根本不需要它 —— 拒绝，不去猜。"""
    with pytest.raises(ValidationError):
        sanitize(document)


def test_entity_declaration_is_rejected_even_without_doctype() -> None:
    with pytest.raises(ValidationError):
        sanitize(b'<!ENTITY x "y"><svg width="10" height="10"/>')


def test_html_with_a_doctype_is_caught_by_the_doctype_rule_first() -> None:
    """``<!DOCTYPE html>`` 这份「改名成 .svg 的网页」在**更早**的一层就被拦下。

    拒绝的理由与 §E 段那条不同（DTD 而非根元素），但结果一样是拒绝 ——
    这里如实记下是哪一层动的手，免得日后 DTD 那条被改动时没人发现
    这份文件改由根元素检查兜底。
    """
    with pytest.raises(ValidationError):
        sanitize(b"<!DOCTYPE html><html><body><p>hi</p></body></html>")


def test_an_empty_file_is_rejected() -> None:
    with pytest.raises(CorruptedFileError):
        sanitize(b"")
    with pytest.raises(CorruptedFileError):
        sanitize(b"   \n  ")


def test_broken_xml_is_rejected() -> None:
    with pytest.raises(CorruptedFileError):
        sanitize(b'<svg width="10"><rect></svg>')


def test_too_many_nodes_is_rejected() -> None:
    body = '<rect width="1" height="1"/>' * (settings.MAX_SVG_NODES + 1)
    with pytest.raises(ValidationError) as excinfo:
        sanitize(svg_document(body))
    assert str(settings.MAX_SVG_NODES) in excinfo.value.message


def test_a_document_just_under_the_node_limit_is_accepted() -> None:
    """上限是「超过才拒」，不是「接近就拒」。"""
    body = '<rect width="1" height="1"/>' * (settings.MAX_SVG_NODES - 2)
    clean, _ = sanitize(svg_document(body))
    assert b"<rect" in clean


def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "big.svg"
    path.write_bytes(svg_document("<!--" + "x" * (settings.MAX_SVG_BYTES + 1024) + "-->"))
    with pytest.raises(ValidationError):
        svg_codec.read_svg(path)


def test_a_file_just_under_the_byte_limit_is_accepted(tmp_path: Path) -> None:
    padding = settings.MAX_SVG_BYTES - 4096
    path = tmp_path / "ok.svg"
    path.write_bytes(svg_document("<!--" + "x" * padding + "-->"))
    assert svg_codec.read_svg(path).startswith(b"<svg")


def test_declared_size_reads_the_files_own_declaration() -> None:
    assert svg_codec.declared_size(svg_document("")) == (100.0, 100.0)
    assert svg_codec.declared_size(svg_document("", attrs='viewBox="0 0 33 44"')) == (33.0, 44.0)


def test_declared_size_admits_it_does_not_know() -> None:
    """``width="100%"`` 在没有外层容器时算不出确定的数 —— 承认不知道，不编。"""
    assert svg_codec.declared_size(svg_document("", attrs='width="100%" height="50%"')) is None
    assert svg_codec.declared_size(svg_document("", attrs="")) is None


# ----------------------------------------------------------------------
# G 段：真机证据 —— 本地文件够不着（逐像素）
# ----------------------------------------------------------------------

def test_referenced_local_file_reaches_neither_bytes_nor_pixels(tmp_path: Path) -> None:
    """一张品红色的本地图片被 ``file://`` 引用。

    只看「字节里没有路径」是不够的 —— 渲染器完全可能读了文件、画上去，
    而路径本身不出现在产物里。所以这里**逐像素**确认：产物里一个品红像素都没有。
    """
    secret = tmp_path / "secret.png"
    secret.write_bytes(marker_png((255, 0, 255)))
    assert secret.exists()

    body = f'<image x="0" y="0" width="100" height="100" href="{secret.as_uri()}"/>'
    clean, notes = sanitize(svg_document(body))

    assert "secret.png" not in clean.decode("utf-8")
    assert str(secret) not in clean.decode("utf-8")
    assert svg_codec.EXTERNAL_NOTE in notes

    rendered = svg_codec.render_svg(clean)
    assert (rendered.width, rendered.height) == (100, 100)
    image = rendered.image.convert("RGB")
    colors = {color for _, color in (image.getcolors(maxcolors=1 << 20) or [])}
    assert (255, 0, 255) not in colors, "本地文件被读进去画出来了"


def test_a_local_file_cannot_be_smuggled_in_through_css(tmp_path: Path) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(marker_png((255, 0, 255)))

    clean, _ = sanitize(
        svg_document(
            f'<style>rect {{ fill: url({secret.as_uri()}); }}</style>'
            '<rect width="100" height="100"/>'
        )
    )
    image = svg_codec.render_svg(clean).image.convert("RGB")
    colors = {color for _, color in (image.getcolors(maxcolors=1 << 20) or [])}
    assert (255, 0, 255) not in colors


def test_svg_to_pdf_does_not_embed_a_local_file(tmp_path: Path) -> None:
    """PDF 那条路同样要够不着 —— 它用的是另一个导出函数。"""
    secret = tmp_path / "secret.png"
    secret.write_bytes(marker_png((255, 0, 255)))

    clean, _ = sanitize(
        svg_document(f'<image x="0" y="0" width="100" height="100" href="{secret.as_uri()}"/>')
    )
    pdf = svg_codec.svg_to_pdf(clean)

    assert b"secret.png" not in pdf
    assert b"file:" not in pdf
    # 内嵌图片会在 PDF 里留下图像对象；这里一个都不该有
    import pymupdf

    document = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        assert document[0].get_images() == []
    finally:
        document.close()


# ----------------------------------------------------------------------
# H 段：真机证据 —— 不产生任何一次真实请求
# ----------------------------------------------------------------------

class _Recorder(http.server.BaseHTTPRequestHandler):
    """一个只会记账的本地服务器。"""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        """默认实现会往 stderr 打字，测试输出不需要它。"""


@pytest.fixture
def http_probe():
    """一个真实监听 127.0.0.1 的 HTTP 服务器，记录每一个到达的请求。

    用真服务器而不是 mock：SSRF 的判断标准就是「有没有真的发出去一个请求」，
    mock 掉之后测的变成了「我们以为会不会发」。
    """
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.paths = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_probe_actually_records_requests(http_probe) -> None:
    """先证明这套监听本身是有效的 —— 否则下面那条「零请求」毫无意义。"""
    import urllib.error
    import urllib.request

    port = http_probe.server_address[1]
    with pytest.raises(urllib.error.HTTPError):  # 服务器回 404，但请求已经到达
        urllib.request.urlopen(f"http://127.0.0.1:{port}/probe.png", timeout=5)
    assert http_probe.paths == ["/probe.png"]


def test_a_remote_reference_never_reaches_the_network(http_probe) -> None:
    port = http_probe.server_address[1]
    url = f"http://127.0.0.1:{port}/x.png"

    clean, notes = sanitize(
        svg_document(f'<image x="0" y="0" width="100" height="100" href="{url}"/>')
    )
    assert url not in clean.decode("utf-8")
    assert svg_codec.EXTERNAL_NOTE in notes

    svg_codec.render_svg(clean)
    assert http_probe.paths == [], f"渲染器真的去取了这个地址：{http_probe.paths}"


def test_a_remote_stylesheet_never_reaches_the_network(http_probe) -> None:
    port = http_probe.server_address[1]
    url = f"http://127.0.0.1:{port}/x.css"

    clean, _ = sanitize(
        svg_document(
            f"<style>@import url('{url}');</style>"
            '<rect width="100" height="100" fill="#123456"/>'
        )
    )
    assert url not in clean.decode("utf-8")

    image = svg_codec.render_svg(clean).image.convert("RGB")
    assert http_probe.paths == [], f"渲染器真的去取了这个样式表：{http_probe.paths}"
    # 剥离样式表之后，SVG 自己那份 fill 仍然生效 —— 净化没有把文件毁掉
    colors = {color for _, color in (image.getcolors(maxcolors=1 << 20) or [])}
    assert (0x12, 0x34, 0x56) in colors
