"""IR → HTML / Markdown / 纯文本。

:mod:`office.markup_parse` 的对称面。两者合起来才是完整的安全保证：
**解析时只认白名单里的东西，渲染时不复制任何用户写的东西。**
用户 HTML 里的属性、注释、没认出来的标签，走到这里已经全没了 ——
所以这个模块没有「要不要过滤」的判断，它只负责把 IR 里的字段拼出来。
反过来说，**往这个模块里加「原样输出某段用户输入」的功能，就等于把
上游辛苦建起来的白名单拆掉**。

三种目标共用一套遍历，差别只在每个块怎么写：

* ``html``     —— 一份能独立打开的完整网页（带内联样式，不引用任何外部资源）
* ``markdown`` —— 自写子集，与解析端对称
* ``text``     —— 纯文本，给「HTML/MD → TXT」用

HTML 输出里的图片是 ``data:`` 内联的：这样一份 .html 文件拷到哪儿都能看，
也不会因为「引用了外部地址」而在别的场景下变成一次网络请求。
"""

from __future__ import annotations

import base64
import io
import re
from html import escape as _escape

from PIL import Image

from office.document_ir import (
    Block,
    ImageBlock,
    Paragraph,
    PARA_KIND_BODY,
    PARA_KIND_CODE,
    PARA_KIND_LIST,
    PARA_KIND_QUOTE,
    PARA_KIND_RULE,
    TableBlock,
    TextRun,
)
from office.markup_parse import BULLET_MARKER

__all__ = [
    "DEFAULT_CSS",
    "IMAGE_NOT_IN_TEXT_NOTE",
    "OUTPUT_HTML",
    "OUTPUT_MARKDOWN",
    "OUTPUT_TEXT",
    "build_css",
    "build_document",
    "image_count",
    "notes_for_target",
    "render_html",
    "render_markdown",
    "render_markup",
    "render_text",
]

#: 三种渲染目标。这份词表由**本模块**拥有 —— 它是唯一按目标分派的地方
#: （:func:`render_markup`）。放在解析器里就会变成「解析器也要认识输出格式」，
#: 而解析器只管输入。
OUTPUT_HTML = "html"
OUTPUT_MARKDOWN = "markdown"
OUTPUT_TEXT = "text"

#: 纯文本里放不下图片，要说一声 —— 不然用户会以为图片丢了是 bug。
IMAGE_NOT_IN_TEXT_NOTE = "文档里有 {count} 张图片，纯文本格式无法包含图片，图片没有导出。"

#: 分隔线在纯文本里的写法。ASCII 而不是制表符画线：纯文本的读者可能
#: 用的是不支持制表符画线的编辑器或终端。
TEXT_RULE = "-" * 20

#: 缩进和引用在纯文本里的前缀
TEXT_INDENT = "    "
TEXT_QUOTE = "> "

#: 默认样式。**字体栈里全是本机通用族，没有任何 @font-face 或远程字体** ——
#: 这份 CSS 会被原样交给排版引擎，里面出现一个 url() 就是一次外部请求。
#: ``__FONT_STACK__`` / ``__FONT_SIZE__`` 由调用方替换（PDF 那条路要换成
#: 真实的系统字体）。
DEFAULT_CSS = """
body {
  font-family: __FONT_STACK__;
  font-size: __FONT_SIZE__pt;
  line-height: 1.6;
  color: #1a1a1a;
  margin: 0;
}
h1, h2, h3, h4, h5, h6 { font-weight: bold; line-height: 1.3; margin: 0.8em 0 0.4em; }
h1 { font-size: 1.9em; }
h2 { font-size: 1.6em; }
h3 { font-size: 1.35em; }
h4 { font-size: 1.15em; }
h5, h6 { font-size: 1em; }
p { margin: 0 0 0.7em; white-space: pre-wrap; }
blockquote {
  margin: 0 0 0.7em 0;
  padding-left: 0.9em;
  border-left: 3px solid #cfcfcf;
  color: #555;
  white-space: pre-wrap;
}
pre {
  font-family: Consolas, "Courier New", monospace;
  background: #f5f5f5;
  padding: 0.6em;
  margin: 0 0 0.7em;
  white-space: pre-wrap;
}
code { font-family: Consolas, "Courier New", monospace; }
table { border-collapse: collapse; margin: 0 0 0.7em; width: 100%; }
td, th { border: 1px solid #bbb; padding: 0.3em 0.5em; text-align: left; }
hr { border: none; border-top: 1px solid #ccc; margin: 1em 0; }
img { max-width: 100%; }
a { color: #1a5fb4; }
.list-item { margin: 0 0 0.3em; }
""".strip()

_FONT_STACK_TOKEN = "__FONT_STACK__"
_FONT_SIZE_TOKEN = "__FONT_SIZE__"

#: 默认字体栈：全是**通用族名与本机字体名**，浏览器/排版器自己去找，
#: 不会产生任何请求。PDF 那条路会替换成真实探测到的字体。
DEFAULT_FONT_STACK = (
    '-apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", '
    '"Hiragino Sans GB", "Source Han Sans SC", sans-serif'
)
DEFAULT_FONT_SIZE_PT = 11

#: Pillow 报的格式名 → MIME。渲染回 ``data:`` 时要用，
#: 写错 MIME 会让部分浏览器/排版器拒绝显示。
_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "WEBP": "image/webp",
    "TIFF": "image/tiff",
}


def image_count(blocks: list[Block]) -> int:
    return sum(1 for block in blocks if isinstance(block, ImageBlock))


def notes_for_target(blocks: list[Block], target_format: str) -> list[str]:
    """这个目标格式带不走的东西，如实说一声。

    只有纯文本会丢图片：Markdown 和 HTML 都能内联 ``data:`` 图片。
    """
    if target_format != OUTPUT_TEXT:
        return []
    count = image_count(blocks)
    if count:
        return [IMAGE_NOT_IN_TEXT_NOTE.format(count=count)]
    return []


def render_markup(blocks: list[Block], target_format: str) -> str:
    """按目标格式渲染。``html`` / ``markdown`` / ``text`` 之外的取值抛错。"""
    if target_format == OUTPUT_HTML:
        return build_document(blocks)
    if target_format == OUTPUT_MARKDOWN:
        return render_markdown(blocks)
    if target_format == OUTPUT_TEXT:
        return render_text(blocks)
    raise ValueError(f"未知的渲染目标：{target_format}")


# ----------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------

def build_css(
    *,
    font_stack: str | None = None,
    font_size_pt: float | None = None,
    extra_css: str = "",
) -> str:
    """拼出这份文档要用的 CSS。

    ``extra_css`` 追加在默认样式之后（PDF 那条路用它注入 ``@font-face``）。
    **调用方必须保证 extra_css 里没有外部引用** —— 这里不做检查，
    因为唯一的调用方是 :mod:`office.html_to_pdf`，它的 CSS 是常量拼出来的。
    """
    css = DEFAULT_CSS.replace(
        _FONT_STACK_TOKEN, font_stack or DEFAULT_FONT_STACK
    ).replace(_FONT_SIZE_TOKEN, _plain_number(font_size_pt or DEFAULT_FONT_SIZE_PT))
    if extra_css:
        css = f"{css}\n{extra_css}"
    return css


def build_document(
    blocks: list[Block],
    *,
    font_stack: str | None = None,
    font_size_pt: float | None = None,
    extra_css: str = "",
    title: str = "转换结果",
) -> str:
    """渲染成一份可以独立打开的完整网页。"""
    css = build_css(
        font_stack=font_stack, font_size_pt=font_size_pt, extra_css=extra_css
    )
    body = render_html(blocks)
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{_escape(title)}</title>\n"
        f"<style>\n{css}\n</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>\n"
    )


def render_html(blocks: list[Block]) -> str:
    """只要 ``<body>`` 里的那一段（拼进别处时用）。"""
    return "\n".join(_html_block(block) for block in blocks)


def _html_block(block: Block) -> str:
    if isinstance(block, ImageBlock):
        return _html_image(block)
    if isinstance(block, TableBlock):
        return _html_table(block)
    if isinstance(block, Paragraph):
        return _html_paragraph(block)
    return ""  # pragma: no cover - Block 目前只有这三种


def _html_paragraph(block: Paragraph) -> str:
    if block.kind == PARA_KIND_RULE:
        return "<hr>"

    # 代码块的正文**原样输出**，与 Markdown / 纯文本两条路一致
    # （见 ``_markdown_code``）：代码里的 ``*`` 就是乘号，不该变成斜体。
    # 这也顺手消掉了 ``<pre><code>`` 里再套一层 ``<code>`` 的来源 ——
    # 块本身已经说明了「这里是代码」，里面再包一个是多余的，而源 HTML
    # 写成 ``<pre><code>x</code></pre>`` 时正好会触发。
    if block.kind == PARA_KIND_CODE:
        literal = _escape("".join(run.text for run in block.runs))
        return f"<pre><code>{literal}</code></pre>"

    inner = _html_runs(block.runs)
    if block.level:
        level = max(1, min(6, block.level))
        return f"<h{level}>{inner}</h{level}>"

    style = ""
    if block.indent:
        style = f' style="margin-left: {block.indent * 1.6:.1f}em"'
    if block.kind == PARA_KIND_QUOTE:
        return f"<blockquote{style}>{inner}</blockquote>"
    if block.kind == PARA_KIND_LIST:
        marker = f"{_escape(block.marker)} " if block.marker else ""
        return f'<p class="list-item"{style}>{marker}{inner}</p>'
    return f"<p{style}>{inner}</p>"


def _html_runs(runs: list[TextRun]) -> str:
    parts: list[str] = []
    for run in runs:
        text = _escape(run.text)
        if run.code:
            text = f"<code>{text}</code>"
        if run.bold:
            text = f"<strong>{text}</strong>"
        if run.italic:
            text = f"<em>{text}</em>"
        if run.link:
            # 地址已经在解析阶段过了协议白名单，这里只做属性转义
            text = f'<a href="{_escape(run.link, quote=True)}">{text}</a>'
        parts.append(text)
    return "".join(parts)


def _html_image(block: ImageBlock) -> str:
    return (
        f'<p><img src="{_data_uri(block.data)}" '
        f'style="width: {_plain_number(block.width_pt)}pt"></p>'
    )


def _html_table(block: TableBlock) -> str:
    rows: list[str] = []
    for index, row in enumerate(block.rows):
        cells = "".join(
            f"<{'th' if index == 0 else 'td'}>{_escape(cell)}</{'th' if index == 0 else 'td'}>"
            for cell in row
        )
        rows.append(f"<tr>{cells}</tr>")
    return "<table>" + "".join(rows) + "</table>"


def _data_uri(data: bytes) -> str:
    """把图片字节编回 ``data:``。MIME 由真实解码结果决定，不靠猜。"""
    mime = "image/png"
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = _MIME_BY_FORMAT.get(image.format or "", mime)
    except Exception:  # pragma: no cover - 解析阶段已经解码成功过一次
        pass
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


# ----------------------------------------------------------------------
# Markdown
# ----------------------------------------------------------------------

def render_markdown(blocks: list[Block]) -> str:
    """渲染成 Markdown 子集（与解析端对称）。"""
    chunks: list[str] = []
    for block in blocks:
        chunks.append(_markdown_block(block))
    return "\n\n".join(chunk for chunk in chunks if chunk != "") + "\n"


def _markdown_block(block: Block) -> str:
    if isinstance(block, ImageBlock):
        return f"![图片]({_data_uri(block.data)})"
    if isinstance(block, TableBlock):
        return _markdown_table(block)
    if not isinstance(block, Paragraph):
        return ""  # pragma: no cover

    if block.kind == PARA_KIND_RULE:
        return "---"

    inner = _markdown_runs(block.runs)
    if block.level:
        level = max(1, min(6, block.level))
        return f"{'#' * level} {inner}"
    if block.kind == PARA_KIND_CODE:
        return f"```\n{_markdown_code(block.runs)}\n```"
    if block.kind == PARA_KIND_QUOTE:
        prefix = "> " * (block.indent + 1)
        return "\n".join(f"{prefix}{line}" for line in inner.split("\n"))
    if block.kind == PARA_KIND_LIST:
        indent = TEXT_INDENT * block.indent
        return f"{indent}{_markdown_marker(block.marker)} {inner}"
    # 正文段落最后过一道行首转义：``inner`` 里的字符级转义管不了
    # 「这一行以什么开头」，而那一件事才决定它会不会变成标题 / 列表 / 引用。
    return _escape_line_starts(inner)


def _markdown_marker(marker: str | None) -> str:
    """列表项前缀换成 Markdown 的写法。

    IR 里的 ``•`` 是**给人和排版器看的字符**（PDF / 纯文本原样输出），
    但 Markdown 的无序列表要用 ``-`` —— 直接写 ``• 甲`` 的话，
    那份 .md 再被解析一次就不再是列表了，渲染出来是一堆以圆点开头的段落。
    有序列表的 ``1.`` 两种语境下写法相同，原样保留（序号也就不会重排）。
    """
    if not marker or marker == BULLET_MARKER:
        return "-"
    return marker


def _markdown_code(runs: list[TextRun]) -> str:
    """代码块里的文字**一个字符都不转义** —— 转了就不是原来的代码了。"""
    return "".join(run.text for run in runs)


def _markdown_runs(runs: list[TextRun]) -> str:
    parts: list[str] = []
    for run in runs:
        text = _escape_markdown(run.text)
        if run.code:
            text = f"`{text}`"
        if run.bold and run.italic:
            text = f"***{text}***"
        elif run.bold:
            text = f"**{text}**"
        elif run.italic:
            text = f"*{text}*"
        if run.link:
            # Markdown 的链接地址没有「属性转义」这一层可退，带空格或尖括号
            # 的地址只能按 CommonMark 的老办法用尖括号包起来。
            target = run.link
            if any(char in target for char in " ()<>"):
                target = f"<{target}>"
            text = f"[{text}]({target})"
        parts.append(text)
    return "".join(parts)


def _markdown_table(block: TableBlock) -> str:
    if not block.rows:
        return ""
    width = max(len(row) for row in block.rows)
    lines: list[str] = []

    def row_line(cells: list[str]) -> str:
        padded = list(cells) + [""] * (width - len(cells))
        return "| " + " | ".join(_escape_markdown(cell) for cell in padded) + " |"

    lines.append(row_line(block.rows[0]))
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    for row in block.rows[1:]:
        lines.append(row_line(row))
    return "\n".join(lines)


#: Markdown 里会改变语义的字符。转义掉它们，输出才是**可逆**的 ——
#: 否则一段以 ``#`` 开头的正文再被解析一次就变成标题了。
_MARKDOWN_SPECIAL = ("\\", "`", "*", "_", "[", "]", "<", ">", "|")

#: 行首的记号。``#`` 出现在句子中间（``C#``）没有任何语义，
#: 出现在**行首**却会把整段变成标题；``-`` / ``1.`` / ``>`` 同理。
#: 所以这些字符只在行首转义 —— 全文替换 ``#`` 会把 ``C#`` 写成 ``C\#``，
#: 那是为了一个不存在的问题去弄脏每一个正常的句子。
#:
#: ``-{2,}`` 单独一支是给分隔线留的：``---`` 会被解析成 ``<hr>``，
#: 而它作为一行纯文本时只是一行横杠。``*`` / ``_`` 的分隔线不用管 ——
#: 它们在 :data:`_MARKDOWN_SPECIAL` 那一层就已经逐个转义过了。
_LINE_START_MARKER = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>#{1,6}|-{2,}|[-+*]|>|\d+[.)])(?=[ \t]|$)"
)


def _escape_line_starts(text: str) -> str:
    """把行首的 Markdown 记号转义掉，保住「正文还是正文」。

    这一条是给 **TXT → Markdown** 用的：纯文本里没有标记，所以
    「``# 注意事项``」是一段普通的话；照原样写进 .md，它再被解析一次
    就成了标题 —— 用户拿到的结果与他给的东西对不上，而界面上还写着
    「结果只有正文段落」。
    """
    def _escape(match: re.Match[str]) -> str:
        marker = match.group("marker")
        if len(marker) > 1 and marker[-1] in ".)":
            # ``1.`` / ``2)``：要转义的是那个点/括号，不是数字
            return f"{match.group('indent')}{marker[:-1]}\\{marker[-1]}"
        return f"{match.group('indent')}\\{marker}"

    return "\n".join(
        _LINE_START_MARKER.sub(_escape, line) for line in text.split("\n")
    )


def _escape_markdown(text: str) -> str:
    for char in _MARKDOWN_SPECIAL:
        text = text.replace(char, f"\\{char}")
    return text


# ----------------------------------------------------------------------
# 纯文本
# ----------------------------------------------------------------------

def render_text(blocks: list[Block]) -> str:
    """渲染成纯文本。

    图片不带过来（纯文本装不下），由 :func:`notes_for_target` 负责说明。
    """
    lines: list[str] = []
    for block in blocks:
        rendered = _text_block(block)
        if rendered:
            lines.append(rendered)
    return "\n".join(lines) + "\n"


def _text_block(block: Block) -> str:
    if isinstance(block, ImageBlock):
        return ""
    if isinstance(block, TableBlock):
        return "\n".join(" | ".join(row) for row in block.rows)
    if not isinstance(block, Paragraph):
        return ""  # pragma: no cover

    if block.kind == PARA_KIND_RULE:
        return TEXT_RULE

    text = "".join(run.text for run in block.runs)
    if block.level:
        return text
    if block.kind == PARA_KIND_CODE:
        return "\n".join(TEXT_INDENT + line for line in text.split("\n"))
    if block.kind == PARA_KIND_QUOTE:
        prefix = TEXT_QUOTE * (block.indent + 1)
        return "\n".join(prefix + line for line in text.split("\n"))
    if block.kind == PARA_KIND_LIST:
        indent = TEXT_INDENT * (block.indent + 1)
        lines = text.split("\n")
        head = f"{indent}{block.marker or '-'} {lines[0]}"
        rest = [f"{indent}{TEXT_INDENT}{line}" for line in lines[1:]]
        return "\n".join([head, *rest])
    return text


def _plain_number(value: float) -> str:
    """把数字写成 CSS 里好看的形状：``11.0`` → ``11``，``10.5`` → ``10.5``。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"
