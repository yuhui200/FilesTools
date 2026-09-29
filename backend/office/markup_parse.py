"""HTML / Markdown 子集 → IR。

**安全是这个模块的第一职责，其次才是「认得多少标签」。**

为什么要有这个中间层，而不是把用户上传的 HTML 直接交给排版引擎：
PyMuPDF 的 ``Story`` 是一个真正的 HTML/CSS 排版器，它默认**会去解析
外部资源** —— ``<img src>``、``@font-face``、``@import``、CSS 里的
``url()``。用户上传的 HTML 里写一句 ``<img src="file:///etc/passwd">``，
或者 ``<img src="http://内网地址/x.png">``，就分别变成一次本地文件读取和
一次服务端发起的网络请求（SSRF）。原样透传是不可能堵住的：HTML 的属性
语法有太多写法（大小写、实体编码、``srcset``、``xlink:href``…），
逐一黑名单永远会漏。

所以这里的做法是**白名单重建**：只把认得的标签解析成 IR，产物由
:mod:`office.markup_render` 从 IR **重新生成**。用户写的属性一个都不复制，
没认出来的标签一律当容器（保留文字、丢掉一切属性），危险标签连内容一起丢。
排版引擎拿到的永远是我们自己生成的 HTML，里面只可能有 ``data:`` 图片。

Markdown 走同一条路（决策 D：零依赖，自写子集，不做 CommonMark 兼容）。
Markdown 里没有「原样透传 HTML」这一条 —— 尖括号就是普通字符，
``<script>`` 只会被转义成文字。

子集范围（支持的就是这些，其余如实当纯文本处理）：

* 标题 ``#`` 到 ``######``
* 粗体 ``**`` / ``__``、斜体 ``*`` / ``_``
* 链接 ``[文字](地址)``、图片 ``![说明](地址)``
* 有序 / 无序列表（按缩进嵌套）
* 引用 ``>``
* 代码块（围栏 ``` 或 ~~~）与行内代码
* 分隔线 ``---`` / ``***`` / ``___``
* 表格（GFM 竖线表）

**内嵌图片的尺寸只认解码结果**，``width`` / ``height`` 属性一概不看 ——
一句 ``width="99999"`` 就能把版面撑爆。
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

from PIL import Image

from config import settings
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
from utils.errors import ValidationError

__all__ = [
    "BINARY_IMAGE_NOTE",
    "BULLET_MARKER",
    "ImageBlock",
    "MAX_MARKUP_CHARS",
    "MAX_MARKUP_IMAGES",
    "MAX_MARKUP_IMAGE_BYTES",
    "MAX_MARKUP_NODES",
    "ParsedMarkup",
    "REMOTE_IMAGE_NOTE",
    "SOURCE_HTML",
    "SOURCE_MARKDOWN",
    "TOO_LONG_MESSAGE",
    "TOO_MANY_IMAGES_MESSAGE",
    "TOO_MANY_NODES_MESSAGE",
    "UNSAFE_LINK_NOTE",
    "allowed_extensions",
    "check_length",
    "markup_source_for_extension",
    "parse_html",
    "parse_markdown",
    "parse_markup",
]

#: 无序列表项在 IR 里的前缀记号。它同时是**显示用的字符**（PDF / 纯文本里
#: 原样出现）和**判断有序无序的依据**，所以只有这一处定义 ——
#: Markdown 渲染要把它换回 ``-``，两处各写一个字面量迟早会漂移。
BULLET_MARKER = "•"

#: 输入种类（也就是「源格式」，与 registry 里的 type 词汇一致）
SOURCE_HTML = "html"
SOURCE_MARKDOWN = "markdown"

#: 渲染目标的词表在 :mod:`office.markup_render` —— 那是唯一按目标分派的地方。
#: 这里只认输入，不认识输出。

MAX_MARKUP_CHARS = settings.MAX_MARKUP_CHARS
MAX_MARKUP_NODES = settings.MAX_MARKUP_NODES
MAX_MARKUP_IMAGES = settings.MAX_MARKUP_IMAGES
MAX_MARKUP_IMAGE_BYTES = settings.MAX_MARKUP_IMAGE_BYTES

TOO_LONG_MESSAGE = f"这个文件太长了（超过 {MAX_MARKUP_CHARS} 字），请先拆分后再上传。"
TOO_MANY_NODES_MESSAGE = (
    f"这个文件的结构太复杂（超过 {MAX_MARKUP_NODES} 个标签），请精简后再上传。"
)
TOO_MANY_IMAGES_MESSAGE = (
    f"这个文件里的图片超过 {MAX_MARKUP_IMAGES} 张，请精简后再上传。"
)

#: 剥离了东西就必须说 —— 悄悄丢图比转换失败更难发现。
REMOTE_IMAGE_NOTE = (
    "文档里有 {count} 张图片不是内嵌的（外链或本地文件路径），"
    "本功能只支持内嵌图片，这些图片没有转换。"
)


def _human_bytes(value: int) -> str:
    """把字节数写成人看的样子。上限小于 1 MB 时不能按 MB 取整 ——
    256 KB 会写成「0 MB」，那句话就成了笑话。"""
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):g} MB"
    return f"{max(1, value // 1024)} KB"


BINARY_IMAGE_NOTE = (
    "文档里有 {count} 张内嵌图片无法识别，或超出单张 "
    f"{_human_bytes(MAX_MARKUP_IMAGE_BYTES)} 的限制，这些图片没有转换。"
)
UNSAFE_LINK_NOTE = (
    "文档里有 {count} 个链接的地址不是 http / https / mailto，已只保留链接文字。"
)

#: 超链接只放行这三种协议。``file:`` 会把服务器的目录结构交给点链接的人，
#: ``javascript:`` / ``data:`` 是脚本注入的常见载体 —— 一律只留文字。
_SAFE_LINK_SCHEMES = ("http://", "https://", "mailto:")

#: 内嵌图片只放行这几种 MIME。**认得出来还不够，还要真能解码** ——
#: 声明成 image/png 实则不是图片的负载靠下面 Pillow 那步挡住。
_IMAGE_MIMES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/bmp",
        "image/webp",
        "image/tiff",
    }
)

#: ``data:[<mime>][;参数],<数据>``
_DATA_URI = re.compile(r"^data:([^,;]*)((?:;[^,;]*)*),(.*)$", re.IGNORECASE | re.DOTALL)

#: 这些标签**连文字内容一起丢掉**。两类：
#: 1. 内容不是给人读的（script / style / head / title）；
#: 2. 会去取外部资源或承载交互的活动内容（iframe / object / embed /
#:    svg / video / audio / 表单控件…）。内联 SVG 也在其中：IR 里没有
#:    矢量块，与其假装支持再渲染成空白，不如如实丢掉并记一笔。
_SUPPRESS_TAGS = frozenset(
    {
        "script",
        "style",
        "head",
        "title",
        "iframe",
        "frame",
        "frameset",
        "object",
        "embed",
        "applet",
        "param",
        "svg",
        "math",
        "canvas",
        "template",
        "video",
        "audio",
        "source",
        "track",
        "map",
        "area",
        "button",
        "select",
        "option",
        "optgroup",
        "textarea",
        "noscript",
        "dialog",
        "slot",
    }
)

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

#: 遇到就断开当前段落的块级标签
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "figure",
        "figcaption",
        "address",
        "center",
        "dl",
        "dt",
        "dd",
        "ul",
        "ol",
        "li",
        "blockquote",
        "pre",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
    }
) | _HEADING_TAGS

_BOLD_TAGS = frozenset({"b", "strong"})
_ITALIC_TAGS = frozenset({"i", "em", "cite", "var", "dfn"})
_MONO_TAGS = frozenset({"code", "kbd", "samp", "tt"})

_WHITESPACE = re.compile(r"[ \t\r\n\f\v]+")


@dataclass(slots=True)
class ParsedMarkup:
    """解析结果。

    ``notes`` 是「我丢掉了什么」的如实交代 —— 调用方必须把它带进结果说明，
    不能只留 ``blocks``。
    """

    blocks: list[Block] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def markup_source_for_extension(suffix: str) -> str | None:
    """按扩展名判断是 HTML 还是 Markdown；不认识返回 None。"""
    lowered = suffix.lower()
    if lowered in {".html", ".htm"}:
        return SOURCE_HTML
    if lowered in {".md", ".markdown"}:
        return SOURCE_MARKDOWN
    return None


def allowed_extensions() -> set[str]:
    """允许上传的标记文档扩展名（给校验层与前端用）。"""
    return set(settings.ALLOWED_MARKUP_EXTENSIONS)


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def parse_markup(text: str, source_format: str) -> ParsedMarkup:
    """按源格式解析成 IR。长度上限在两种格式上是同一条。"""
    if source_format == SOURCE_MARKDOWN:
        return parse_markdown(text)
    if source_format == SOURCE_HTML:
        return parse_html(text)
    raise ValidationError("这个文件既不是 HTML 也不是 Markdown。")


def check_length(text: str) -> None:
    """长度上限。超了抛 :class:`ValidationError`，文案取自本模块。

    上传时（``loader.validate_markup_upload``）与解析时各查一次：前者在
    用户提交的那一刻就给反馈，不必等排到队里才失败；后者保证**任何**
    调用方都绕不过去 —— 包括将来某个直接调解析器的新入口。
    两处共用这一份界限与这一句文案，不会出现「上传放行、解析拒绝」
    却各说各话的情况。
    """
    if len(text) > MAX_MARKUP_CHARS:
        raise ValidationError(TOO_LONG_MESSAGE)


def parse_html(text: str) -> ParsedMarkup:
    """把 HTML 解析成 IR（白名单重建，见模块说明）。"""
    check_length(text)

    collector = _HtmlCollector()
    try:
        collector.feed(text)
        collector.close()
    except ValidationError:
        raise
    except Exception as exc:  # pragma: no cover - HTMLParser 极少抛别的
        # 解析器内部炸了就说文件读不出来，绝不把异常本身透给用户
        raise ValidationError("无法读取该 HTML 文件，请检查文件内容后重试。") from exc

    collector.flush()
    return ParsedMarkup(blocks=collector.blocks, notes=collector.notes())


class _HtmlCollector(HTMLParser):
    """把 HTML 收敛成 IR。

    ``convert_charrefs=True`` 让 ``&amp;`` / ``&#65;`` 在 ``handle_data``
    之前就还原好 —— 这样实体编码绕不过下面的协议白名单
    （``&#106;avascript:`` 还原完照样是 ``javascript:``，照样被拦）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.nodes = 0

        self._suppress: list[str] = []
        self._runs: list[TextRun] = []
        self._level: int | None = None
        self._kind = PARA_KIND_BODY
        self._marker: str | None = None
        self._indent = 0

        self._bold = 0
        self._italic = 0
        self._mono = 0
        self._links: list[str | None] = []
        self._pre = 0
        self._lists: list[dict] = []
        self._quotes = 0

        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

        self._images = 0
        self._remote_images = 0
        self._bad_images = 0
        self._unsafe_links = 0

    # -- 说明 ---------------------------------------------------------

    def notes(self) -> list[str]:
        return _notes_for(
            remote=self._remote_images,
            bad=self._bad_images,
            unsafe_links=self._unsafe_links,
        )

    # -- 段落累积 -----------------------------------------------------

    def _append(self, text: str) -> None:
        if not text:
            return
        link = self._links[-1] if self._links else None
        bold = self._bold > 0
        italic = self._italic > 0
        code = self._mono > 0
        # 相邻同样式的文字合并成一段，产物里不会有几百个碎片 run
        if self._runs:
            last = self._runs[-1]
            if (
                last.bold == bold
                and last.italic == italic
                and last.code == code
                and last.link == link
            ):
                last.text += text
                return
        self._runs.append(
            TextRun(text=text, bold=bold, italic=italic, link=link, code=code)
        )

    def _add_text(self, data: str) -> None:
        if not data:
            return
        if not self._pre:
            # 普通 HTML 里连续空白折叠成一个空格（浏览器的默认行为）；
            # <pre> 里一个字符都不能动。
            data = _WHITESPACE.sub(" ", data)
            if not self._runs:
                data = data.lstrip()
        if data:
            self._append(data)

    def flush(self) -> None:
        """把当前累积的文字收成一个段落。"""
        if self._table is not None or not self._runs:
            return
        if not any(run.text.strip() for run in self._runs):
            self._runs = []
            return
        self.blocks.append(
            Paragraph(
                runs=self._runs,
                level=self._level,
                kind=self._kind,
                marker=self._marker,
                indent=self._indent,
            )
        )
        self._runs = []

    def _reset_block(self) -> None:
        self._kind = PARA_KIND_BODY
        self._marker = None
        self._indent = 0

    # -- 标签 ---------------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:
        self.nodes += 1
        if self.nodes > MAX_MARKUP_NODES:
            raise ValidationError(TOO_MANY_NODES_MESSAGE)

        if self._suppress:
            # 已经在一个被丢弃的容器里：只有同类的嵌套值得记一层，
            # 别的一律当不存在 —— 它们的属性更是一个都不看。
            if tag in _SUPPRESS_TAGS:
                self._suppress.append(tag)
            return

        if tag in _SUPPRESS_TAGS:
            self.flush()
            self._suppress.append(tag)
            return

        if tag in _BLOCK_TAGS:
            self.flush()

        if tag == "hr":
            self.blocks.append(Paragraph(runs=[], kind=PARA_KIND_RULE))
            return

        if tag == "br":
            if self._cell is not None:
                self._cell.append(" ")
            elif self._runs:
                self._runs[-1].text += "\n"
            else:
                self._append("\n")
            return

        if tag == "img":
            self._handle_image(attrs)
            return

        if tag in _BOLD_TAGS:
            self._bold += 1
        elif tag in _ITALIC_TAGS:
            self._italic += 1
        elif tag in _MONO_TAGS:
            self._mono += 1

        if tag == "a":
            href = _attr(attrs, "href")
            safe = _safe_link(href)
            if safe is None and href:
                self._unsafe_links += 1
            self._links.append(safe)
        elif tag in _HEADING_TAGS:
            self._level = int(tag[1])
        elif tag == "pre":
            self._pre += 1
            self._kind = PARA_KIND_CODE
        elif tag == "blockquote":
            self._quotes += 1
            self._kind = PARA_KIND_QUOTE
            self._indent = self._quotes - 1
        elif tag in {"ul", "ol"}:
            self._lists.append({"ordered": tag == "ol", "count": 0})
        elif tag == "li":
            if self._lists:
                current = self._lists[-1]
                if current["ordered"]:
                    current["count"] += 1
                    self._marker = f"{current['count']}."
                else:
                    self._marker = BULLET_MARKER
            else:
                self._marker = BULLET_MARKER
            self._kind = PARA_KIND_LIST
            self._indent = max(0, len(self._lists) - 1)
        elif tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_startendtag(self, tag: str, attrs) -> None:
        """自闭合标签（``<br/>`` / ``<img/>`` / ``<param/>``）。"""
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._suppress:
            if self._suppress[-1] == tag:
                self._suppress.pop()
            return

        if tag == "table":
            if self._table is not None:
                rows = [row for row in self._table if any(cell for cell in row)]
                if rows:
                    self.blocks.append(TableBlock(rows=rows))
            self._table = None
            self._row = None
            self._cell = None
            return

        if tag == "tr":
            if self._table is not None and self._row is not None:
                self._table.append(self._row)
            self._row = None
            return

        if tag in {"td", "th"}:
            if self._row is not None and self._cell is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = None
            return

        if tag in _BOLD_TAGS:
            self._bold = max(0, self._bold - 1)
        elif tag in _ITALIC_TAGS:
            self._italic = max(0, self._italic - 1)
        elif tag in _MONO_TAGS:
            self._mono = max(0, self._mono - 1)
        elif tag == "a":
            if self._links:
                self._links.pop()
        elif tag == "pre":
            self.flush()
            self._pre = max(0, self._pre - 1)
            self._reset_block()
            return
        elif tag == "blockquote":
            self.flush()
            self._quotes = max(0, self._quotes - 1)
            self._reset_block()
            return
        elif tag in {"ul", "ol"}:
            self.flush()
            if self._lists:
                self._lists.pop()
        elif tag == "li":
            self.flush()
            self._reset_block()

        if tag in _BLOCK_TAGS:
            self.flush()

        if tag in _HEADING_TAGS:
            self._level = None

    def handle_data(self, data: str) -> None:
        if self._suppress:
            return
        if self._cell is not None:
            self._cell.append(data if self._pre else _WHITESPACE.sub(" ", data))
            return
        self._add_text(data)

    # -- 图片 ---------------------------------------------------------

    def _handle_image(self, attrs) -> None:
        self.flush()
        outcome, payload = _decode_data_image(_attr(attrs, "src") or "")

        if outcome == "remote":
            self._remote_images += 1
            return
        if outcome != "ok" or payload is None:
            self._bad_images += 1
            return

        self._images += 1
        if self._images > MAX_MARKUP_IMAGES:
            raise ValidationError(TOO_MANY_IMAGES_MESSAGE)

        # ``width`` / ``height`` 属性一律不看：真实尺寸只由解码结果决定。
        self.blocks.append(_image_block(payload))


def _notes_for(*, remote: int, bad: int, unsafe_links: int) -> list[str]:
    notes: list[str] = []
    if remote:
        notes.append(REMOTE_IMAGE_NOTE.format(count=remote))
    if bad:
        notes.append(BINARY_IMAGE_NOTE.format(count=bad))
    if unsafe_links:
        notes.append(UNSAFE_LINK_NOTE.format(count=unsafe_links))
    return notes


def _image_block(payload: tuple[bytes, int, int]) -> ImageBlock:
    data, width, height = payload
    return ImageBlock(
        data=data,
        width_pt=width * 72.0 / 96.0,
        height_pt=height * 72.0 / 96.0,
    )


def _attr(attrs, name: str) -> str | None:
    """取一个属性值（HTMLParser 给的是小写标签名、原样的属性名）。"""
    for key, value in attrs:
        if key.lower() == name:
            return value
    return None


def _safe_link(href: str | None) -> str | None:
    """超链接地址按协议白名单过滤；不安全的一律返回 None（文字保留）。"""
    if not href:
        return None
    value = href.strip()
    if not value:
        return None
    # 先还原一次百分号编码再判断协议 —— 否则 ``%6a%61vascript:`` 能绕过去。
    probe = urllib.parse.unquote(value).strip().lower()
    if probe.startswith(_SAFE_LINK_SCHEMES):
        return value
    return None


def _decode_data_image(src: str) -> tuple[str, tuple[bytes, int, int] | None]:
    """尝试把 ``data:`` 图片解码出来。

    返回 ``(结果, 负载)``，结果是 ``"ok"`` / ``"remote"`` / ``"bad"`` 之一。
    三个值要分清楚：``remote`` 是「引用了外部资源」（该告诉他我们只支持
    内嵌图），``bad`` 是「引用了内嵌图但读不出来」（该告诉他这张图坏了）。
    """
    value = (src or "").strip()
    if not value:
        return "bad", None

    match = _DATA_URI.match(value)
    if match is None:
        # 不是 data: —— 外链、file:、相对路径、协议相对地址全在这里被挡下
        return "remote", None

    mime = (match.group(1) or "").strip().lower()
    if mime not in _IMAGE_MIMES:
        return "bad", None

    parameters = (match.group(2) or "").lower()
    payload = match.group(3)

    if ";base64" in parameters:
        # 先按字符串长度粗筛再解码：一个 200 MB 的 base64 串不能先解出来
        # 再判断超没超限，那样内存已经花掉了。
        if len(payload) > (MAX_MARKUP_IMAGE_BYTES // 3 + 1) * 4:
            return "bad", None
        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            return "bad", None
    else:
        raw = urllib.parse.unquote_to_bytes(payload)

    if not raw or len(raw) > MAX_MARKUP_IMAGE_BYTES:
        return "bad", None

    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            width, height = image.size
    except Exception:
        # 声明成图片、实则不是图片的负载在这里被拦下
        return "bad", None

    if width <= 0 or height <= 0:
        return "bad", None
    return "ok", (raw, int(width), int(height))


# ----------------------------------------------------------------------
# Markdown 子集（决策 D：零依赖，自写，不做 CommonMark 兼容）
# ----------------------------------------------------------------------

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*(\S*)\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_QUOTE = re.compile(r"^\s{0,3}(>+)\s?(.*)$")
_BULLET = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]*)\)")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
#: 反斜杠转义：``\*`` 是一个字面星号，不是斜体标记。**只对 ASCII 标点生效** ——
#: 全字符转义会吃掉 ``C:\Users`` 里的路径分隔符，那是为了一个不存在的问题
#: 去弄脏每一段正常的文字。
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_BOLD_STAR = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDER = re.compile(r"(?<![A-Za-z0-9_])__(.+?)__(?![A-Za-z0-9_])", re.DOTALL)
_ITALIC_STAR = re.compile(r"\*(.+?)\*", re.DOTALL)
_ITALIC_UNDER = re.compile(r"(?<![A-Za-z0-9_])_(.+?)_(?![A-Za-z0-9_])", re.DOTALL)

#: 行内标记的尝试顺序。同一位置上有多个候选时，靠前的赢 ——
#: ``![`` 必须排在 ``[`` 前面，``**`` 必须排在 ``*`` 前面。
_INLINE_PATTERNS = (
    (_IMAGE, "image"),
    (_LINK, "link"),
    (_CODE_SPAN, "code"),
    (_BOLD_STAR, "bold"),
    (_BOLD_UNDER, "bold"),
    (_ITALIC_STAR, "italic"),
    (_ITALIC_UNDER, "italic"),
)


def parse_markdown(text: str) -> ParsedMarkup:
    """把 Markdown 子集解析成 IR。"""
    check_length(text)

    parser = _MarkdownParser()
    parser.run(text.replace("\r\n", "\n").replace("\r", "\n"))
    return ParsedMarkup(blocks=parser.blocks, notes=parser.notes())


class _MarkdownParser:
    """逐行解析。

    Markdown 没有「标签」可数，所以节点上限在这里按**行数**折算 ——
    一行至少产出一个块，行数远超上限的输入本身就该被拒。
    """

    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self._images = 0
        self._remote_images = 0
        self._bad_images = 0
        self._unsafe_links = 0
        #: 行内解析时遇到的图片先攒在这里，等所属段落落位之后再追加，
        #: 免得图片跑到它上面那段文字的前面去。
        self._pending: list[Block] = []

    def notes(self) -> list[str]:
        return _notes_for(
            remote=self._remote_images,
            bad=self._bad_images,
            unsafe_links=self._unsafe_links,
        )

    # -- 主循环 -------------------------------------------------------

    def run(self, text: str) -> None:
        lines = text.split("\n")
        if len(lines) > MAX_MARKUP_NODES:
            raise ValidationError(TOO_MANY_NODES_MESSAGE)

        index = 0
        paragraph: list[str] = []

        def flush_paragraph() -> None:
            if not paragraph:
                return
            self._emit(Paragraph(runs=self._inline("\n".join(paragraph))))
            paragraph.clear()

        while index < len(lines):
            line = lines[index]

            fence = _FENCE.match(line)
            if fence is not None:
                flush_paragraph()
                marker = fence.group(1)[0]
                body: list[str] = []
                index += 1
                while index < len(lines):
                    closing = _FENCE.match(lines[index])
                    if closing is not None and closing.group(1)[0] == marker:
                        index += 1
                        break
                    body.append(lines[index])
                    index += 1
                # 代码**块**，不是行内代码：``TextRun.code`` 的语义是
                # 「这一段是行内代码」（见 ``document_ir``），而块的身份由
                # ``kind=PARA_KIND_CODE`` 表达。两处都标，就是把同一件事
                # 说两遍 —— 行内代码那条路（下面的反引号分支）才该标它。
                self._emit(
                    Paragraph(
                        runs=[TextRun(text="\n".join(body))],
                        kind=PARA_KIND_CODE,
                    )
                )
                continue

            if not line.strip():
                flush_paragraph()
                index += 1
                continue

            if _RULE.match(line) is not None and _BULLET.match(line) is None:
                flush_paragraph()
                self._emit(Paragraph(runs=[], kind=PARA_KIND_RULE))
                index += 1
                continue

            heading = _ATX_HEADING.match(line)
            if heading is not None:
                flush_paragraph()
                self._emit(
                    Paragraph(
                        runs=self._inline(heading.group(2)),
                        level=len(heading.group(1)),
                    )
                )
                index += 1
                continue

            if self._is_table_start(lines, index):
                flush_paragraph()
                self._emit(self._consume_table(lines, index))
                index = self._table_end(lines, index)
                continue

            quote = _QUOTE.match(line)
            if quote is not None:
                flush_paragraph()
                depth = len(quote.group(1))
                body = [quote.group(2)]
                index += 1
                while index < len(lines):
                    nxt = _QUOTE.match(lines[index])
                    if nxt is None or len(nxt.group(1)) != depth:
                        break
                    body.append(nxt.group(2))
                    index += 1
                self._emit(
                    Paragraph(
                        runs=self._inline("\n".join(body)),
                        kind=PARA_KIND_QUOTE,
                        indent=max(0, depth - 1),
                    )
                )
                continue

            ordered = _ORDERED.match(line)
            bullet = _BULLET.match(line)
            if ordered is not None or bullet is not None:
                flush_paragraph()
                match = ordered if ordered is not None else bullet
                indent = len(match.group(1).replace("\t", "    ")) // 2
                marker = (
                    f"{ordered.group(2)}." if ordered is not None else BULLET_MARKER
                )
                self._emit(
                    Paragraph(
                        runs=self._inline(match.group(3)),
                        kind=PARA_KIND_LIST,
                        marker=marker,
                        indent=indent,
                    )
                )
                index += 1
                continue

            paragraph.append(line.strip())
            index += 1

        flush_paragraph()

    def _emit(self, block: Block) -> None:
        """落一个块，并把它前面攒下的图片紧跟其后。"""
        self.blocks.append(block)
        if self._pending:
            self.blocks.extend(self._pending)
            self._pending.clear()

    # -- 表格 ---------------------------------------------------------

    @staticmethod
    def _is_table_start(lines: list[str], index: int) -> bool:
        if index + 1 >= len(lines):
            return False
        head, separator = lines[index], lines[index + 1]
        if "|" not in head:
            return False
        return "-" in separator and _TABLE_SEPARATOR.match(separator) is not None

    @staticmethod
    def _table_end(lines: list[str], index: int) -> int:
        index += 2  # 表头 + 分隔行
        while index < len(lines) and lines[index].strip() and "|" in lines[index]:
            index += 1
        return index

    def _consume_table(self, lines: list[str], index: int) -> TableBlock:
        rows = [_split_row(lines[index])]
        end = self._table_end(lines, index)
        for cursor in range(index + 2, end):
            rows.append(_split_row(lines[cursor]))
        return TableBlock(rows=rows)

    # -- 行内 ---------------------------------------------------------

    def _inline(
        self,
        text: str,
        *,
        bold: bool = False,
        italic: bool = False,
        link: str | None = None,
    ) -> list[TextRun]:
        """把一段文字拆成带样式的 run。递归处理嵌套（``***粗斜体***``）。"""
        runs: list[TextRun] = []
        position = 0

        while position < len(text):
            match = self._next_inline(text, position)
            escape = _ESCAPE.search(text, position)
            # 转义和行内标记谁先出现谁说了算：``\*斜体\*`` 里的星号是文字，
            # 而 `` `a\b` `` 里那个反斜杠在代码段内部，轮不到这里管。
            if escape is not None and (match is None or escape.start() < match[0]):
                if escape.start() > position:
                    runs.append(
                        TextRun(
                            text=text[position : escape.start()],
                            bold=bold,
                            italic=italic,
                            link=link,
                        )
                    )
                runs.append(
                    TextRun(
                        text=escape.group(1), bold=bold, italic=italic, link=link
                    )
                )
                position = escape.end()
                continue
            if match is None:
                runs.append(
                    TextRun(text=text[position:], bold=bold, italic=italic, link=link)
                )
                break

            start, end, kind, groups = match
            if start > position:
                runs.append(
                    TextRun(text=text[position:start], bold=bold, italic=italic, link=link)
                )

            if kind == "code":
                # ``_CODE_SPAN`` 的两个组是（反引号, 内容）
                runs.append(
                    TextRun(
                        text=groups[1], bold=bold, italic=italic, link=link, code=True
                    )
                )
            elif kind == "image":
                self._add_image(groups[1])
            elif kind == "link":
                target = _safe_link(groups[1])
                if target is None and groups[1]:
                    self._unsafe_links += 1
                runs.extend(
                    self._inline(groups[0], bold=bold, italic=italic, link=target)
                )
            elif kind == "bold":
                runs.extend(
                    self._inline(groups[0], bold=True, italic=italic, link=link)
                )
            else:  # italic
                runs.extend(
                    self._inline(groups[0], bold=bold, italic=True, link=link)
                )

            position = end

        return _merge_runs(runs)

    @staticmethod
    def _next_inline(text: str, position: int):
        """从 ``position`` 起找最近的一处行内标记。"""
        best = None
        for pattern, kind in _INLINE_PATTERNS:
            found = pattern.search(text, position)
            if found is None:
                continue
            if best is None or found.start() < best[0]:
                best = (found.start(), found.end(), kind, found.groups())
        return best

    def _add_image(self, src: str) -> None:
        outcome, payload = _decode_data_image(src)
        if outcome == "remote":
            self._remote_images += 1
            return
        if outcome != "ok" or payload is None:
            self._bad_images += 1
            return

        self._images += 1
        if self._images > MAX_MARKUP_IMAGES:
            raise ValidationError(TOO_MANY_IMAGES_MESSAGE)

        self._pending.append(_image_block(payload))


def _merge_runs(runs: list[TextRun]) -> list[TextRun]:
    """相邻同样式的 run 合成一段，空 run 丢掉。

    ``\\# 注意事项`` 会先切出一个字面的 ``#``、再切出后面那截，两段样式
    一模一样 —— 不合的话同一句话在 IR 里就碎成两段，
    与 :class:`_HtmlCollector` 那边的处理也不一致。
    """
    merged: list[TextRun] = []
    for run in runs:
        if not run.text:
            continue
        if merged:
            last = merged[-1]
            if (
                last.bold == run.bold
                and last.italic == run.italic
                and last.code == run.code
                and last.link == run.link
            ):
                last.text += run.text
                continue
        merged.append(run)
    return merged


def _split_row(line: str) -> list[str]:
    """把 ``| a | b |`` 切成单元格。"""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]
