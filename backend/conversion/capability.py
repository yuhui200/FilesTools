"""能力条目的类型定义 —— **纯类型，零依赖、零 I/O**（第九阶段 §十三/§十四）。

这个模块只有 ``dataclasses`` 与 ``typing`` 两个 import，**故意**不 import
任何转换实现、不 import ``config``。注册表（``conversion/registry.py``）
拿它当条目的形状，路由与测试拿它当公开契约。

## 为什么用 ``frozen=True, slots=True`` 的 dataclass 而不是 ``NamedTuple``

* 条目有十几个字段，``NamedTuple`` 的位置参数一旦顺序写错是**静默串位**，
  关键字构造不会；
* ``frozen=True`` 是真只读，改一个字段会当场抛 ``FrozenInstanceError``，
  而不是像 ``_replace`` 那样悄悄造一个新实例；
* 字段全是不可变类型（``tuple`` / ``str`` / ``bool`` / 标量），
  所以**整个条目可哈希**，可以直接进 ``set``。这一点有测试钉住
  （``test_every_capability_is_hashable``）—— 它同时保证了没人往条目里塞
  ``list`` / ``dict``，因为那会当场让哈希失败。

## ``available`` 绝不入条目

「这台服务器现在能不能转」**不是**条目的属性。把探测结果写进条目，
等于在 import 期做一次 I/O 并把那一刻的答案冻结成常量 —— 之后组件装上或
卸掉，条目还在撒谎。所以条目只声明「需要什么」（``requires``），
可用性由 ``registry.capability_snapshot(...)`` 在**序列化时**现算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ----------------------------------------------------------------------
# 操作类型（§十二）
# ----------------------------------------------------------------------

#: 1→1，走统一任务队列（Task Queue → Worker Pool → Converter）。
OPERATION_CONVERSION = "conversion"
#: 多文件 / 页面级操作。**不进统一队列** —— 它走各自已有的接口，
#: 因为「把 3 份 PDF 合成 1 份」与「一份进一份出」在结构上就不是一回事，
#: 硬塞进批量模型只会两边都别扭。
OPERATION_OPERATION = "operation"

OPERATION_TYPES: tuple[str, ...] = (OPERATION_CONVERSION, OPERATION_OPERATION)

# ----------------------------------------------------------------------
# 类别（新词汇，§四十三 的 ID 前缀）
# ----------------------------------------------------------------------

CATEGORY_IMAGE = "image"
CATEGORY_DOCUMENT = "document"
CATEGORY_PDF = "pdf"

CATEGORIES: tuple[str, ...] = (CATEGORY_IMAGE, CATEGORY_DOCUMENT, CATEGORY_PDF)

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_IMAGE: "图片",
    CATEGORY_DOCUMENT: "文档",
    CATEGORY_PDF: "PDF",
}

# ----------------------------------------------------------------------
# 分组（旧词汇，Phase 7 前端与既有测试靠它）
# ----------------------------------------------------------------------

#: ``category`` 与 ``group`` 是**有意分开**的两件事：
#: ``category`` 回答「这是什么能力」，进 ID；``group`` 回答「界面上分到哪一栏」。
#: TXT→DOCX 的 category 是 ``document``，但 group 仍是 ``text`` ——
#: 它和 TXT→PDF 是同一栏里的两个选项，用户不该看到两栏 TXT。
GROUP_IMAGE = "image"
GROUP_OFFICE = "office"
GROUP_TEXT = "text"
GROUP_PDF = "pdf"

GROUPS: tuple[str, ...] = (GROUP_IMAGE, GROUP_OFFICE, GROUP_TEXT, GROUP_PDF)

GROUP_LABELS: dict[str, str] = {
    GROUP_IMAGE: "图片",
    GROUP_OFFICE: "Office 文档",
    GROUP_TEXT: "文本",
    GROUP_PDF: "PDF",
}

# ----------------------------------------------------------------------
# 组件要求
# ----------------------------------------------------------------------

#: 只靠必装依赖（PyMuPDF / Pillow），不额外要求任何东西。
REQUIREMENT_BUILTIN = "builtin"
#: Pillow —— 硬依赖，装了服务就能转。
REQUIREMENT_IMAGE = "image"
#: LibreOffice。
REQUIREMENT_OFFICE = "office"
#: python-docx（PDF → Word）。
REQUIREMENT_PDF_TO_WORD = "pdf_to_word"
#: 自写的 Markdown/HTML 子集解析与渲染 —— 纯标准库，**永远可用**。
REQUIREMENT_MARKUP = "markup"
#: python-docx，用在「**写出**一份 DOCX」的能力上（TXT → Word）。
#:
#: 与 :data:`REQUIREMENT_PDF_TO_WORD` 是同一个库，却分成两个名字：
#: 前者说的是「读 PDF 再写成 Word」，后者只说「写 Word」。合成一个的话，
#: 界面上的缺失说明会把两个不相干的功能一起报出来 ——
#: 缺 python-docx 时用户看到的是「PDF 转 Word 不可用」，
#: 而他真正点不动的是 TXT 转 Word。
REQUIREMENT_DOCX = "docx"
#: HEIC / HEIF **解码**所需（pillow-heif 里的 libheif + libde265）。
#:
#: 第十阶段 A §五–§八 要求 HEIC **动态探测、绝不假设可用**，而它与前面几个
#: 需求名有一个本质区别：**它有一个「只有一半」的中间态**。HEIC 的编解码器
#: 是分开打包的 —— libde265 管解码、libx265 管编码，一个只带前者的构建
#: 能 ``HEIC → JPG`` 而不能 ``JPG → HEIC``。
#:
#: 所以这里是**两个**需求名而不是一个 ``heif``。合成一个的话，
#: §六 那句「只有解码时就只发布解码方向」就无从落地：要么把能用的解码
#: 一起藏掉（过度降级），要么把一个点了必然失败的编码按钮摆出去
#: （伪装成可用）。两种都是那句规格明令禁止的。
REQUIREMENT_HEIF_DECODE = "heif_decode"
#: HEIC / HEIF **编码**所需（libx265）。
#:
#: 与 :data:`REQUIREMENT_HEIF_DECODE` 分开的理由见那里。
REQUIREMENT_HEIF_ENCODE = "heif_encode"

REQUIREMENTS: tuple[str, ...] = (
    REQUIREMENT_BUILTIN,
    REQUIREMENT_IMAGE,
    REQUIREMENT_OFFICE,
    REQUIREMENT_PDF_TO_WORD,
    REQUIREMENT_MARKUP,
    REQUIREMENT_DOCX,
    REQUIREMENT_HEIF_DECODE,
    REQUIREMENT_HEIF_ENCODE,
)

# ----------------------------------------------------------------------
# 选项类型（§二十二）
# ----------------------------------------------------------------------

TYPE_INTEGER = "integer"
TYPE_NUMBER = "number"
TYPE_BOOLEAN = "boolean"
TYPE_ENUM = "enum"
TYPE_STRING = "string"

OPTION_TYPES: tuple[str, ...] = (
    TYPE_INTEGER,
    TYPE_NUMBER,
    TYPE_BOOLEAN,
    TYPE_ENUM,
    TYPE_STRING,
)

#: 唯一一个需要**运行期探测**的动态枚举来源：服务器上真实装了的字体。
#: 探测由 services 层做（要 scandir 字体目录），这里只留一个标记。
DYNAMIC_FONTS = "fonts"


# ----------------------------------------------------------------------
# 操作的输入怎么送（只有 operation 用得上）
# ----------------------------------------------------------------------

#: 用户选的文件**直接**作为 multipart 文件字段发给端点。可以多选。
INPUT_FILES = "files"
#: 同上，但**一次只有一个文件**（第十阶段 A 的元数据查看器）。
#:
#: 为什么不复用 ``INPUT_FILES``：那个名字在端点签名里就是 ``files``，
#: 而查看元数据这件事结构上只接受一张图 —— 收下一个列表再默默只用第一张，
#: 是一个用户看不见的谎。前端那边两种写法兼容得很自然
#: （``formData.append(entry.input_field, file)`` 对单复数都成立），
#: 所以多一个词不会让它多一个分支。
INPUT_FILE = "file"
#: 用户选的文件先 POST 给 ``/api/pdf/upload`` 换一个 ``input_id``，
#: 再带着这个令牌调用端点（令牌与内容绑定，端点因此不必再收一次字节）。
INPUT_UPLOAD = "input_id"

#: 三个值都是**端点上真实的 Form 字段名**，不是自造的词 —— 于是
#: 「条目声明的送法」与「端点真的收什么」可以用一条机械测试对账
#: （``test_declared_input_field_really_exists_on_the_endpoint``）。
INPUT_KINDS: tuple[str, ...] = (INPUT_FILES, INPUT_FILE, INPUT_UPLOAD)


# ----------------------------------------------------------------------
# 条目
# ----------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OptionSpec:
    """一个用户可以提交的选项。

    ``key`` 是**扁平点号串**（``"resize.width"``），不是嵌套对象 ——
    前端拿到就能直接铺成表单控件，不需要理解层级；``visible_when``
    也用同一个点号键，两边始终对得上。

    ``values`` 一律是 ``((value, label), ...)``，且 ``value`` **都是字符串**
    （``"90"`` 而不是 ``90``）。这样表单天然是「字符串进、字符串出」的
    单一通道，服务端再做一次类型收敛 —— 不必为数字枚举再造一条路。
    """

    key: str
    type: str
    label: str
    default: Any = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str | None = None
    values: tuple[tuple[str, str], ...] = ()
    #: 快捷档：``((值, 名字), ...)``，**值仍是这个键自己的字符串形式**。
    #:
    #: 与 ``values`` 的区别是它不改变字段的类型 —— ``values`` 是「只能从这几个
    #: 里选」（枚举），``presets`` 是「这几个是常用值，其余也认」（连续量上的
    #: 快捷按钮）。质量 85 与目标大小 1 MB 都属于后者。
    #:
    #: 放在这里而不是让前端各写一份常量表，是 §五十二 的直接要求：
    #: 加一档、改一个数只改服务端一处，三个页面同时跟上。
    presets: tuple[tuple[str, str], ...] = ()
    help: str | None = None
    required: bool = False
    #: 依赖项：形如 ``(("resize.mode", "custom"),)``，全部成立时才显示。
    visible_when: tuple[tuple[str, Any], ...] = ()
    #: 非 None 表示这个枚举的值要在运行期填（目前只有 ``"fonts"``）。
    dynamic: str | None = None

    def to_json(self) -> dict[str, Any]:
        """出网形状。**只输出真正有值的字段**，不留一堆 ``null``。"""
        out: dict[str, Any] = {
            "key": self.key,
            "type": self.type,
            "label": self.label,
            "default": self.default,
            "required": self.required,
        }
        for name in ("min", "max", "step", "unit", "help", "dynamic"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.values:
            out["enum"] = [{"value": v, "label": label} for v, label in self.values]
            # 默认值的中文名一并给出：界面不用自己再查一遍表，
            # 也不会出现「值变了标签没变」这种对不上的情况。
            out["default_label"] = next(
                (label for value, label in self.values if value == str(self.default)),
                None,
            )
        if self.presets:
            out["presets"] = [{"value": v, "label": label} for v, label in self.presets]
        if self.visible_when:
            out["visible_when"] = {key: value for key, value in self.visible_when}
        return out


@dataclass(frozen=True, slots=True)
class Capability:
    """统一转换中心里的一格能力（§十四）。

    字段分三类：

    * **身份**：``id`` / ``source_type`` / ``target_type`` / ``operation_type``
    * **展示**：``display_name`` / ``category`` / ``group`` / ``note``
    * **执行**：``converter_key``（conversion）或 ``endpoint``+``method``（operation）

    ``requires`` 与 ``worker_pool`` 是**声明**，不是探测结果：
    ``requires`` 说「要哪个组件」，``worker_pool`` 说「该排哪个资源池」。
    两者都由序列化层和队列层分别消费。
    """

    #: 稳定唯一 ID（§四十三），形如 ``image.jpg-to-png`` / ``op.pdf-merge``。
    id: str
    source_type: str
    target_type: str
    operation_type: str
    display_name: str
    category: str
    group: str
    requires: str
    worker_pool: str
    supports_batch: bool = True
    supports_preview: bool = False
    #: 执行入口：conversion 用 ``converter_key``，operation 用 ``endpoint``。
    converter_key: str | None = None
    endpoint: str | None = None
    method: str = "POST"
    options: tuple[OptionSpec, ...] = ()
    tags: tuple[str, ...] = ()
    note: str | None = None
    #: **只有 operation 用**：这份操作接受哪些源类型的文件。
    #:
    #: 为什么不复用 ``source_type``：那是契约要求的**一个**标量（§十四），
    #: 而「图片合成 PDF」收的是七种图片。两者不是同一件事 ——
    #: ``source_type`` 回答「这条能力挂在哪个源上」，``accepts`` 回答
    #: 「界面上该放行哪些文件」。conversion 恒为空：它的输入由
    #: ``(source_type, target_type)`` 唯一确定，再来一份就是第二份真相。
    accepts: tuple[str, ...] = ()
    #: **只有 operation 用**：文件怎么交给端点，取值见 :data:`INPUT_KINDS`。
    input_field: str | None = None

    @property
    def is_conversion(self) -> bool:
        return self.operation_type == OPERATION_CONVERSION

    @property
    def is_operation(self) -> bool:
        return self.operation_type == OPERATION_OPERATION

    @property
    def supports_options(self) -> bool:
        """**派生**自 ``options``，不是独立字段。

        写成字段就会出现「声明说支持选项、实际一条 schema 都没有」这种
        自相矛盾的状态，而界面会照着声明渲染出一个空面板。真实来源只有一个：
        有没有 ``OptionSpec``。
        """
        return bool(self.options)

    def option_schema(self) -> dict[str, Any] | None:
        """``options_schema`` 的线上形状；没有选项时返回 ``None``。

        ``dynamic`` 枚举此时还是空壳（``enum`` 缺失），
        由 services 层在序列化时填 —— 探测要碰磁盘，不能在这一层做。
        """
        if not self.options:
            return None
        return {"version": 1, "items": [spec.to_json() for spec in self.options]}

    def to_json(self, *, available: bool) -> dict[str, Any]:
        """出网形状。``available`` 由调用方现算后传进来。

        **每个键都在**，没有值时是 ``null`` —— 一个逐条目变形的 JSON 会让前端
        每次都要 ``in`` 判断，而契约测试也没法把「有哪些键」钉死。
        想知道有没有选项请看 ``supports_options``，不要去猜
        ``options_schema`` 缺没缺。

        **不含** ``converter_key``：那是服务端的分发细节（它直接对应一个
        Python 函数名），对前端没有意义，泄漏出去只会变成一套没人敢改的
        隐性契约。
        """
        return {
            "id": self.id,
            "source_type": self.source_type,
            "target_type": self.target_type,
            "operation_type": self.operation_type,
            "display_name": self.display_name,
            "category": self.category,
            "group": self.group,
            "requires": self.requires,
            "worker_pool": self.worker_pool,
            "available": available,
            "supports_batch": self.supports_batch,
            "supports_options": self.supports_options,
            "supports_preview": self.supports_preview,
            "endpoint": self.endpoint,
            "method": self.method if self.endpoint is not None else None,
            "tags": list(self.tags),
            "note": self.note,
            "options_schema": self.option_schema(),
            # 操作专用（conversion 恒为 ``[]`` / ``null``）：前端据此决定
            # 放行哪些文件、以及要不要先换一个上传令牌。
            "accepts": list(self.accepts),
            "input_field": self.input_field,
        }


# ----------------------------------------------------------------------
# ID
# ----------------------------------------------------------------------

_ID_SEPARATOR = "-to-"
_OPERATION_PREFIX = "op."


def capability_id(source_type: str, target_type: str, *, category: str) -> str:
    """拼一个 conversion 的稳定 ID。

    源与目标用的是**类型词汇**而不是扩展名：``jpg`` 与 ``jpeg`` 是同一个
    源类型，ID 里出现 ``jpeg-to-`` 会让人以为存在一个叫 ``jpeg`` 的源。
    扩展名是 ``EXTENSIONS_BY_SOURCE`` 的事，不在这里表达。
    """
    return f"{category}.{source_type}{_ID_SEPARATOR}{target_type}"


def operation_id(name: str) -> str:
    """拼一个 operation 的稳定 ID，如 ``op.pdf-merge``。"""
    return f"{_OPERATION_PREFIX}{name}"


def parse_capability_id(value: str) -> tuple[str, str, str] | None:
    """``"image.jpg-to-png"`` → ``("image", "jpg", "png")``；形状不对返回 None。

    有了它，ID 与 ``(source_type, target_type)`` 就是**同一事实的两种视图**，
    可以互相反推 —— ``test_every_id_round_trips`` 正是靠这一点保证
    不会有人手写一个与内容不符的 ID。operation 的 ID 不是这个形状，返回 None。
    """
    category, dot, rest = value.partition(".")
    if not dot or category not in CATEGORIES:
        return None
    source, separator, target = rest.partition(_ID_SEPARATOR)
    if not separator or not source or not target:
        return None
    if _ID_SEPARATOR in target:
        return None
    return category, source, target


__all__ = [
    "CATEGORIES",
    "CATEGORY_DOCUMENT",
    "CATEGORY_IMAGE",
    "CATEGORY_LABELS",
    "CATEGORY_PDF",
    "Capability",
    "DYNAMIC_FONTS",
    "GROUPS",
    "GROUP_IMAGE",
    "GROUP_LABELS",
    "GROUP_OFFICE",
    "GROUP_PDF",
    "GROUP_TEXT",
    "INPUT_FILE",
    "INPUT_FILES",
    "INPUT_KINDS",
    "INPUT_UPLOAD",
    "OPERATION_CONVERSION",
    "OPERATION_OPERATION",
    "OPERATION_TYPES",
    "OPTION_TYPES",
    "OptionSpec",
    "REQUIREMENTS",
    "REQUIREMENT_BUILTIN",
    "REQUIREMENT_DOCX",
    "REQUIREMENT_HEIF_DECODE",
    "REQUIREMENT_HEIF_ENCODE",
    "REQUIREMENT_IMAGE",
    "REQUIREMENT_MARKUP",
    "REQUIREMENT_OFFICE",
    "REQUIREMENT_PDF_TO_WORD",
    "TYPE_BOOLEAN",
    "TYPE_ENUM",
    "TYPE_INTEGER",
    "TYPE_NUMBER",
    "TYPE_STRING",
    "capability_id",
    "operation_id",
    "parse_capability_id",
]
