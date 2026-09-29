"""SVG 解码：把一份**不可信**的 SVG 安全地变成位图或 PDF（第十阶段 A §九–§十五）。

## 为什么放在 ``compressors/`` 而不是新建 ``services/image/``

对流水线来说 SVG 只是**另一种解码器**：``load_image`` 把位图文件变成一张
``PIL.Image``，这里把 SVG 变成同样的一张 ``PIL.Image``。于是裁剪 / 缩放 /
旋转 / 翻转 / 按质量编码 / 按目标大小压缩这一整套 Phase 10A 的操作，
SVG 与 JPG **走的是同一条代码路径**，一行都不用重写 —— 这正是 §二
「不要创建第二套图片转换系统」想要的结果。若另起一套 SVG 处理流程，
「SVG 的旋转」和「JPG 的旋转」早晚会不一致。

## 安全模型：SVG 是**活动内容**，不是图片

一份 SVG 里可以写 ``<script>``、可以 ``<image href="file:///etc/passwd">``、
可以 ``<image href="http://内网地址/">``。直接丢给渲染器等于给上传者一个
「读服务器本地文件」和「让服务器替你发请求」的入口。

这里的做法是**净化 + 渲染器自身的约束**两层：

1. **净化**（:func:`sanitize_svg`）：在 XML 层把危险的东西**删掉**再交给
   渲染器 —— 而不是指望渲染器忽略它。删掉的东西会如实记进 ``notes``，
   用户能知道自己的文件被动过。
2. **渲染器约束**：实测 PyMuPDF 自己就会忽略外部图片引用
   （``svg: ignoring external image '...'``），并且**不执行** ``<script>``。
   这是第二层，不是唯一一层 —— 只靠它的话，换一个渲染器就全塌了。

### 实测确认的边界（``scripts/_probe_svg*.py``，两个探针跑完即删）

* ``file://`` / ``http://`` / ``https://`` 的外部图片**没有被读取、没有被取回**，
  渲染照常完成，输出里不含被引用文件的内容；
* ``<use xlink:href="file://...">``、``@import``、CSS ``url()``、``feImage``
  同样都没有发生 I/O；
* ``data:`` 内联栅格图**会被真的画上去**（这是我们要的）；
  而 ``data:image/svg+xml`` 被渲染器当作外部图片忽略，**不会递归解析**；
* ``<script>`` / ``javascript:`` / ``<foreignObject>`` 解析通过但**不执行**；
* 实体膨胀与递归实体没有炸开；
* **一份 HTML 改名成 ``.svg`` 会被渲染器当成 SVG 打开**（实测
  ``<!DOCTYPE html><html>...`` 得到 534×800 的页面）—— 所以「渲染器能打开」
  绝不等于「这是一份 SVG」。:func:`sanitize_svg` 因此**必须**自己检查根元素，
  这是防伪造的那一道，不是多余的。

## 尺寸：用户单位与像素 1:1

SVG 的 ``width="200"``（无单位时）就是 200 个用户单位。渲染时用
``dpi=72``，于是 200 用户单位 → 200 像素，与浏览器里看到的一致。
带物理单位的（``100mm``）按 72 单位/英寸换算 —— 这是**明确写下来的约定**，
不是碰巧：SVG 规范本身就把无单位值定义为 CSS 像素，而 MuPDF 的内部单位是
点（1/72 英寸），两者在 72 这个数上重合。

源文件既没有 ``width``/``height`` 也没有 ``viewBox`` 时，MuPDF 会兜底成
612×792（美式信纸）。这个值不是文件里写的任何东西，直接采信等于凭空造出
一个用户没要求过的尺寸，所以 :func:`_declared_size` 会自己解析一遍根元素，
只在**文件真的什么都没说**时才用它，并如实记一条 note。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

import pymupdf
from PIL import Image

from config import settings
from utils.errors import CorruptedFileError, UnsupportedTypeError, ValidationError

# ----------------------------------------------------------------------
# 词汇表
# ----------------------------------------------------------------------

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"

#: 让 ``ElementTree.tostring`` 输出 ``<svg xmlns="...">`` 而不是 ``<ns0:svg>``。
#: 全局注册，但后端只有这一个模块用 ElementTree（已 grep 确认），
#: 不会踩到别人的解析。
ElementTree.register_namespace("", SVG_NAMESPACE)
ElementTree.register_namespace("xlink", XLINK_NAMESPACE)

#: 连内容一起删掉的元素。
#:
#: * ``script`` —— 脚本；
#: * ``foreignObject`` / ``iframe`` / ``object`` / ``embed`` / ``canvas`` ——
#:   可以把任意 HTML 或另一份文档塞进 SVG 的容器，是「SVG 里再开一个浏览器」
#:   的那类入口；
#: * ``audio`` / ``video`` —— 会引用外部资源；
#: * ``annotation-xml`` / ``handler`` / ``listener`` —— SVG 里既有的
#:   事件绑定入口（SVG 1.2 的旧写法）。
_FORBIDDEN_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "foreignObject",
        "iframe",
        "object",
        "embed",
        "canvas",
        "audio",
        "video",
        "annotation-xml",
        "handler",
        "listener",
    }
)

#: 一律删除的属性：这些名字在 SVG 里本来就不该有，出现即说明是想干别的。
_FORBIDDEN_ATTRS: frozenset[str] = frozenset(
    {
        "externalResourcesRequired",
        "src",
        "poster",
        "action",
        "formaction",
        "dynsrc",
        "lowsrc",
        "background",
        "data",           # <object data="...">，元素已删，属性一并防住
        "srcdoc",
    }
)

#: 可以写成 ``url(...)`` 的呈现属性 —— 外面那层括号里就是一个引用。
#: 漏掉一个就等于留了一条没被检查的引用通道，所以这里是**穷举**。
_URL_ATTRS: frozenset[str] = frozenset(
    {
        "fill",
        "stroke",
        "filter",
        "clip-path",
        "mask",
        "marker",
        "marker-start",
        "marker-mid",
        "marker-end",
        "cursor",
        "color-profile",
        "src",
        "href",
    }
)

#: 走「链接」语义、值本身就是一个 URL 的属性。
_HREF_ATTRS: frozenset[str] = frozenset({"href"})

#: 允许出现在 ``data:`` 里的 MIME —— **只有栅格图**。
#:
#: ``image/svg+xml`` 有意不在其中：那等于允许 SVG 里嵌一份 SVG，
#: 净化只做了一层，里层没被检查过。实测渲染器会忽略它，但「渲染器碰巧
#: 忽略」不是我们可以依赖的防线。
_ALLOWED_DATA_MIME: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "image/bmp",
)

#: 允许的 URL 协议前缀 —— 空元组表示「一个都不允许」，只放行 ``#`` 片段。
_URL_SCHEME_RE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9+.\-]*)\s*:", re.ASCII)
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]*)\1\s*\)""", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"@import[^;]*(;|$)", re.IGNORECASE)

#: CSS 声明里「值本身就是一个可执行协议」的写法。
#:
#: ``_CSS_URL_RE`` 只管 ``url(...)`` 这一条通道。``background:javascript:...``
#: 与 ``width:expression(...)`` 不经过它，所以整条值会被原样留下 ——
#: 实测 ``style="background:javascript:alert(1)"`` 就是这么活下来的。
#: 现代渲染器对这些老写法一律忽略，但理由和 ``data:image/svg+xml`` 那条一样：
#: 「渲染器碰巧忽略」不是可以依赖的防线，净化器要让**文件本身**干净。
_CSS_DANGEROUS_RE = re.compile(
    r"(?:javascript|vbscript|livescript|mocha)\s*:"
    r"|expression\s*\("
    r"|-moz-binding",
    re.IGNORECASE,
)

#: 一条 CSS 声明：``属性: 值``，值不含 ``;`` ``{`` ``}``。
_CSS_DECLARATION_RE = re.compile(r"[a-zA-Z-]+\s*:\s*[^;{}]*")

# ----------------------------------------------------------------------
# 面向用户的说明（净化动了什么，必须说）
# ----------------------------------------------------------------------

SOCKET_NOTE = "SVG 中的脚本或可执行内容已被移除（服务器不接受活动内容）。"
EXTERNAL_NOTE = "SVG 引用的外部资源（网络地址或本地文件）已被忽略，服务器不会去取它们。"
FOREIGN_NOTE = "SVG 中内嵌的外部文档对象已被移除。"
DEFAULT_SIZE_NOTE = "这份 SVG 没有声明尺寸，已按 {width}×{height} 像素输出。"
DOWNSIZED_NOTE = "这份 SVG 声明的尺寸超过服务器上限，已缩小到 {width}×{height} 像素。"

# ----------------------------------------------------------------------
# 渲染结果
# ----------------------------------------------------------------------


@dataclass(slots=True)
class SvgRender:
    """一份 SVG 解码出来的位图，以及「净化时动过什么」的如实说明。"""

    image: Image.Image
    width: int
    height: int
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# 第一层：净化
# ----------------------------------------------------------------------

def read_svg(path: Path) -> bytes:
    """读出 SVG 字节并做体积检查。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValidationError("读取上传文件失败") from exc
    if not data:
        raise CorruptedFileError("这个 SVG 文件是空的。")
    if len(data) > settings.MAX_SVG_BYTES:
        raise ValidationError(
            f"这份 SVG 太大（{len(data) // 1024} KB），"
            f"超过服务器 {settings.MAX_SVG_BYTES // 1024} KB 的处理上限"
        )
    return data


def sanitize_svg(data: bytes) -> tuple[bytes, list[str]]:
    """把一份不可信的 SVG 净化成可以安全渲染的字节。

    返回 ``(净化后的字节, 说明列表)``。任何一条说明都对应「用户的文件里
    本来有、但被我们拿掉了」的东西 —— 不做声地把内容删掉再报「转换成功」，
    用户拿到一张缺了东西的图却不知道为什么。

    拒绝（而不是净化）的三种情况，都是**没法安全地猜**的：

    * 带 ``DOCTYPE`` —— 实体膨胀攻击的载体，而且 SVG 根本不需要 DTD；
    * 根元素不是 ``svg`` —— 实测一份 HTML 改名成 ``.svg`` 渲染器照收不误，
      这一层是唯一能识破它的地方；
    * XML 解析不过 —— 内容坏了。
    """
    if not data or not data.strip():
        raise CorruptedFileError("这个 SVG 文件是空的。")

    # DOCTYPE 一律拒绝：实体声明只能出现在 DTD 里，没有 DTD 就没有实体膨胀。
    # 在解析**之前**按字节查，是因为有些解析器在报错前就已经开始展开实体了。
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise ValidationError("这份 SVG 带有文档类型声明（DTD），出于安全考虑无法处理。")

    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise CorruptedFileError("无法解析这份 SVG，文件可能已损坏。") from exc
    except (ValueError, LookupError) as exc:
        # 编码声明与实际字节不符等
        raise CorruptedFileError("无法解析这份 SVG，文件可能已损坏。") from exc

    _check_root(root)

    nodes = sum(1 for _ in root.iter())
    if nodes > settings.MAX_SVG_NODES:
        raise ValidationError(
            f"这份 SVG 的元素过多（{nodes} 个），"
            f"超过服务器 {settings.MAX_SVG_NODES} 个的处理上限"
        )

    notes: list[str] = []
    flags = _sanitize_element(root, notes, is_root=True)
    if flags["external"]:
        notes.append(EXTERNAL_NOTE)
    if flags["foreign"]:
        notes.append(FOREIGN_NOTE)
    if flags["script"]:
        notes.append(SOCKET_NOTE)

    try:
        clean = ElementTree.tostring(root, encoding="utf-8")
    except (ValueError, TypeError) as exc:  # pragma: no cover - 极罕见
        raise CorruptedFileError("无法解析这份 SVG，文件可能已损坏。") from exc
    return clean, notes


def _check_root(root: ElementTree.Element) -> None:
    """根元素必须真的是 ``svg``。

    命名空间允许为空：手写的 ``<svg width="10">`` 没有 ``xmlns`` 也是合法的
    SVG，浏览器与渲染器都认。但**元素名必须是 svg** —— 这一条挡住的是
    「HTML/XML 改名成 .svg」，而不是「写法不规范的 SVG」。
    """
    tag = root.tag
    if not isinstance(tag, str):
        raise UnsupportedTypeError("这不是一个 SVG 文件。")
    namespace, _, local = tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if local != "svg" or namespace not in ("", SVG_NAMESPACE):
        raise UnsupportedTypeError("这不是一个 SVG 文件，请上传真正的 SVG。")


def _local_name(name: str) -> str:
    """``{http://www.w3.org/2000/svg}rect`` → ``rect``。"""
    return name.rpartition("}")[2] if name.startswith("{") else name


def _sanitize_element(
    element: ElementTree.Element, notes: list[str], *, is_root: bool = False
) -> dict[str, bool]:
    """递归净化一棵子树，返回「动过哪几类东西」。"""
    flags = {"script": False, "external": False, "foreign": False}

    for name in list(element.attrib):
        local = _local_name(name)
        value = element.attrib[name]

        # 事件处理器：onclick / onload / onbegin … 一个都不留
        if local.lower().startswith("on"):
            del element.attrib[name]
            flags["script"] = True
            continue

        if local in _FORBIDDEN_ATTRS:
            del element.attrib[name]
            flags["external"] = True
            continue

        if local == "style":
            cleaned, changed, external = _clean_css(value)
            if external:
                flags["external"] = True
            if changed:
                if cleaned.strip():
                    element.attrib[name] = cleaned
                else:
                    del element.attrib[name]
            continue

        if local in _HREF_ATTRS or local in _URL_ATTRS:
            # 顺序是关键，反过来会漏掉一整类引用：
            #
            # 1. 先把 ``url(...)`` 里指向外部的目标抠掉；
            # 2. 再判断**剩下的整条值**是不是一个不安全的引用。
            #
            # 只做第 1 步挡不住 ``href="javascript:alert(1)"`` ——
            # ``_strip_unsafe_urls`` 只认识 ``url(...)`` 形式，对这种裸
            # 引用无从下手，会把原值原样返回，于是属性被「清理」成了它自己。
            # 只做第 2 步则挡不住 ``fill="url(http://evil/x.png)"`` ——
            # 它不以协议开头，整体看不像 URL。两步都做才封闭。
            cleaned = _strip_unsafe_urls(value)
            if not _is_safe_reference(cleaned):
                cleaned = ""
            if cleaned.strip():
                element.attrib[name] = cleaned
            else:
                del element.attrib[name]
            # 只在**真的改动了**才记一笔。``fill="url(#grad)"`` 这种内部引用
            # 是最常见的正常写法，对它也说「已忽略外部资源」等于骗用户，
            # 会让他以为自己的图被削过。
            if cleaned != value:
                flags["external"] = True

    for child in list(element):
        local = _local_name(child.tag) if isinstance(child.tag, str) else ""

        if local in _FORBIDDEN_TAGS:
            # 整个子树连内容一起删：``<script>`` 的正文留着毫无意义，
            # ``<foreignObject>`` 里那棵 HTML 更是不能留。
            element.remove(child)
            if local == "script":
                flags["script"] = True
            elif local == "foreignObject":
                flags["foreign"] = True
            else:
                flags["external"] = True
            continue

        if local == "style":
            # ``<style>`` 是内联样式表：只允许它引用文档内部的东西。
            text = child.text or ""
            cleaned, changed, external = _clean_css(text)
            if external:
                flags["external"] = True
            if changed:
                child.text = cleaned
            continue

        child_flags = _sanitize_element(child, notes)
        for key, value in child_flags.items():
            flags[key] = flags[key] or value

    # 根元素的命名空间要保住，否则 ``tostring`` 出来的东西渲染器不认。
    if is_root and "}" not in element.tag and element.tag == "svg":
        element.tag = f"{{{SVG_NAMESPACE}}}svg"

    return flags


def _clean_css(text: str) -> tuple[str, bool, bool]:
    """清理一段 CSS，返回 ``(清理后, 有没有改动, 有没有删掉外部引用)``。

    ``@import`` 整条规则删掉 —— 它加载的是**另一份样式表**，那里面有什么
    我们完全没检查过。``url(...)`` 只放行内部片段与安全的内联图片，
    其余把它所在的声明整条删掉（只删 URL 本身会得到 ``fill:;`` 这种残句，
    渲染器对残句的处理不可预期）。
    """
    changed = False
    external = False

    if _CSS_IMPORT_RE.search(text):
        text = _CSS_IMPORT_RE.sub("", text)
        changed = True
        external = True

    def replace(match: re.Match[str]) -> str:
        nonlocal changed, external
        target = match.group(2).strip()
        if _is_safe_reference(target):
            return match.group(0)
        changed = True
        external = True
        return ""

    cleaned = _CSS_URL_RE.sub(replace, text)

    # 值里带可执行协议的声明，整条删掉（理由见 _CSS_DANGEROUS_RE）。
    def drop_dangerous(match: re.Match[str]) -> str:
        nonlocal changed, external
        if _CSS_DANGEROUS_RE.search(match.group(0)):
            changed = True
            external = True
            return ""
        return match.group(0)

    cleaned = _CSS_DECLARATION_RE.sub(drop_dangerous, cleaned)

    if changed:
        # 把因为删掉 url() 或整条声明而变成空壳的声明一起收掉，
        # 避免留下 "fill:;" 这种残句。
        cleaned = re.sub(r"[a-zA-Z-]+\s*:\s*(?=[;}])", "", cleaned)
        cleaned = re.sub(r"[a-zA-Z-]+\s*:\s*$", "", cleaned)
    return cleaned, changed, external


def _strip_unsafe_urls(value: str) -> str:
    """把属性值里不安全的 ``url(...)`` 去掉，保留其余部分。"""

    def replace(match: re.Match[str]) -> str:
        return match.group(0) if _is_safe_reference(match.group(2).strip()) else ""

    return _CSS_URL_RE.sub(replace, value)


def _is_safe_reference(target: str) -> bool:
    """这个引用目标安不安全。

    只有两种算安全：

    * ``#id`` —— 指向**同一份文档内部**的元素，不产生任何 I/O；
    * ``data:`` 且 MIME 是允许的栅格图 —— 字节就在文件里，同样没有 I/O。

    其余一律不安全：``http(s)`` / ``file`` / ``ftp`` / ``javascript`` /
    协议相对地址 ``//host/x``，以及 ``data:image/svg+xml``。
    """
    value = (target or "").strip()
    if not value:
        return True  # 空值没有引用任何东西
    if value.startswith("#"):
        return True
    lowered = value.lower()
    if lowered.startswith("data:"):
        mime = lowered[5:].split(";", 1)[0].split(",", 1)[0].strip()
        return mime in _ALLOWED_DATA_MIME
    if lowered.startswith("//"):
        return False  # 协议相对地址，仍然是外部主机
    return _URL_SCHEME_RE.match(value) is None


# ----------------------------------------------------------------------
# 第二层：渲染
# ----------------------------------------------------------------------

def _open_svg(clean: bytes) -> pymupdf.Document:
    """把净化后的字节交给渲染器，失败一律转成业务错误。"""
    try:
        return pymupdf.open(stream=clean, filetype="svg")
    except Exception as exc:  # pymupdf 的异常类型很杂，且都不是 FileToolsError
        raise CorruptedFileError("无法解析这份 SVG，文件可能已损坏。") from exc


def declared_size(clean: bytes) -> tuple[float, float] | None:
    """从根元素的 ``width``/``height``/``viewBox`` 里读出**文件自己声明的**尺寸。

    两个地方要用它，用法不同但问的是同一件事：

    * 上传校验（``utils.validation.validate_svg_upload``）拿它填结果卡片上的
      尺寸 —— 只解析 XML，**不栅格化**，提交路径上不该为了一张卡片去渲染；
    * 渲染时拿它判断下面那个兜底尺寸是不是真的被用上了。

    只为了一件事：MuPDF 在「什么都没声明」时会兜底成 612×792，而那个数字
    文件里根本没有。渲染器给的页面尺寸与这里算出来的对不上时，
    就说明用的是兜底值，此时按 ``viewBox`` 走更接近用户的预期。

    解析失败不算错误（净化那一步已经确认过它是合法 XML），返回 ``None``
    让调用方走渲染器的结果。读不出来就是 ``None`` —— 「不知道」比编一个
    数字安全，调用方各自决定怎么显示「不知道」。
    """
    try:
        root = ElementTree.fromstring(clean)
    except ElementTree.ParseError:  # pragma: no cover - 上游已解析过一次
        return None

    width = _length_to_units(root.get("width"))
    height = _length_to_units(root.get("height"))
    if width and height:
        return width, height

    box = root.get("viewBox") or root.get("viewbox")
    if box:
        parts = re.split(r"[\s,]+", box.strip())
        if len(parts) == 4:
            try:
                _, _, box_width, box_height = (float(item) for item in parts)
            except ValueError:
                return None
            if box_width > 0 and box_height > 0:
                return box_width, box_height
    return None


_UNIT_TO_UNITS: dict[str, float] = {
    "": 1.0,
    "px": 1.0,
    "pt": 1.0,
    "pc": 16.0,
    "mm": 72.0 / 25.4,
    "cm": 72.0 / 2.54,
    "in": 72.0,
    "q": 72.0 / 101.6,
}


def _length_to_units(raw: str | None) -> float | None:
    """把 ``"100mm"`` 这类长度换算成用户单位（= 点 = 72dpi 下的像素）。

    百分比**有意**返回 ``None``：``width="100%"`` 的含义取决于外层容器，
    而一份独立 SVG 没有外层容器，算不出确定的数。实测渲染器在这种情况
    也会退回默认页面尺寸 —— 与其猜，不如承认算不出来。
    """
    if raw is None:
        return None
    text = raw.strip().lower()
    if not text or text.endswith("%"):
        return None
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([a-z]*)", text, re.ASCII)
    if match is None:
        return None
    factor = _UNIT_TO_UNITS.get(match.group(2))
    if factor is None:
        return None
    value = float(match.group(1)) * factor
    return value if value > 0 else None


def _render_zoom(page: pymupdf.Page) -> tuple[float, float, float, bool]:
    """算出渲染倍率与最终像素尺寸，返回 ``(zoom, 宽, 高, 有没有被缩小)``。

    ``dpi=72`` 对应 ``zoom=1`` —— 用户单位与像素 1:1（见模块文档）。
    超过服务器上限时**缩小**而不是拒绝：位图那条路（``load_image``）
    对超大图也是缩放而不是拒绝，两条路保持一致。真正的拒绝发生在
    PDF 那条路 —— 矢量没法缩，只能拒。
    """
    width = max(1.0, float(page.rect.width))
    height = max(1.0, float(page.rect.height))

    zoom = 1.0
    # 长边上限
    longest = max(width, height)
    if longest > settings.MAX_IMAGE_EDGE:
        zoom = min(zoom, settings.MAX_IMAGE_EDGE / longest)
    # 像素总量上限
    pixels = width * height * zoom * zoom
    if pixels > settings.MAX_IMAGE_PIXELS:
        zoom *= (settings.MAX_IMAGE_PIXELS / pixels) ** 0.5

    out_width = max(1, int(width * zoom))
    out_height = max(1, int(height * zoom))
    return zoom, out_width, out_height, zoom < 1.0


def render_svg(data: bytes) -> SvgRender:
    """把 SVG 字节解码成一张位图。

    传入的必须是**已净化**的字节（:func:`sanitize_svg` 的返回值）。
    这里不再净化一次：两处各净化一遍，早晚会有一处被改漏。
    """
    document = _open_svg(data)
    try:
        if document.page_count < 1:
            raise CorruptedFileError("这份 SVG 没有任何可渲染的内容。")
        page = document[0]
        zoom, width, height, downsized = _render_zoom(page)

        notes: list[str] = []
        declared = declared_size(data)
        if downsized:
            notes.append(DOWNSIZED_NOTE.format(width=width, height=height))
        elif declared is None and _is_default_page(page):
            notes.append(DEFAULT_SIZE_NOTE.format(width=width, height=height))

        try:
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=True)
        except Exception as exc:
            # 渲染器自己的资源上限（FzErrorLimit）落在这里。预检查已经
            # 挡掉了绝大多数，剩下的按「图片过大」如实报告，不能漏成 500。
            raise ValidationError("这份 SVG 渲染后尺寸过大，超过服务器处理上限") from exc

        image = Image.frombytes("RGBA", (pixmap.width, pixmap.height), pixmap.samples)
        return SvgRender(image=image, width=pixmap.width, height=pixmap.height, notes=notes)
    finally:
        document.close()


#: MuPDF 在 SVG 没声明任何尺寸时用的兜底页面（美式信纸）。
_DEFAULT_PAGE_SIZE = (612.0, 792.0)


def _is_default_page(page: pymupdf.Page) -> bool:
    return (
        abs(page.rect.width - _DEFAULT_PAGE_SIZE[0]) < 0.5
        and abs(page.rect.height - _DEFAULT_PAGE_SIZE[1]) < 0.5
    )


def svg_to_pdf(data: bytes) -> bytes:
    """把 SVG 转成 PDF —— **保留矢量与真实文字**，不是把位图塞进 PDF。

    位图那条路（PNG/JPG/WEBP）要先栅格化，这条路不用：``convert_to_pdf``
    产出的是真正的矢量页，文字可以被选中与搜索（实测能抽出 ``get_text()``）。

    页面尺寸就是 SVG 声明的尺寸（点）。矢量没法「缩小到上限以内」，
    所以超过上限的页面在这里**直接拒绝** —— 与位图那条路的缩放处理不同，
    原因是可用的手段不同，不是标准不一致。
    """
    document = _open_svg(data)
    try:
        if document.page_count < 1:
            raise CorruptedFileError("这份 SVG 没有任何可渲染的内容。")
        rect = document[0].rect
        if max(rect.width, rect.height) > settings.MAX_IMAGE_EDGE:
            raise ValidationError(
                f"这份 SVG 的页面尺寸（{rect.width:.0f}×{rect.height:.0f}）"
                f"超过服务器 {settings.MAX_IMAGE_EDGE} 的上限，无法导出为 PDF"
            )
        try:
            pdf = document.convert_to_pdf()
        except Exception as exc:
            raise CorruptedFileError("无法把这份 SVG 转成 PDF，文件可能已损坏。") from exc
        if not pdf:
            raise CorruptedFileError("无法把这份 SVG 转成 PDF。")
        return pdf
    finally:
        document.close()


def looks_like_svg(head: bytes) -> bool:
    """文件头看起来是不是 SVG。

    位图的魔数没有以 ``<`` 开头的，所以这一个判断就够把两条路分开；
    真正的「是不是 SVG」由 :func:`sanitize_svg` 的根元素检查负责 ——
    这一层只是**选解码器**，不是校验。
    """
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    return text.startswith(b"<")


__all__ = [
    "DEFAULT_SIZE_NOTE",
    "DOWNSIZED_NOTE",
    "EXTERNAL_NOTE",
    "FOREIGN_NOTE",
    "SOCKET_NOTE",
    "SVG_NAMESPACE",
    "SvgRender",
    "declared_size",
    "looks_like_svg",
    "read_svg",
    "render_svg",
    "sanitize_svg",
    "svg_to_pdf",
]
