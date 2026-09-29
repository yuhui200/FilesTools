"""选项词表 + 纯校验（第九阶段 §九 / §十 / §二十二）。

这个模块是「用户可以提交哪些参数」的**唯一手写真相**：词表在这里，
边界在这里，校验也在这里 —— 加一个选项不可能只加词表而忘了校验，
因为两者在同一个文件里。

## 为什么它只 import ``config`` 而不 import 任何实现

枚举值（页面大小、适应方式、页边距）在实现里各有自己的一份常量
（``pdf.image_to_pdf.PAGE_SIZES`` / ``compressors.encoder.OUTPUT_FORMATS``）。
这里**故意不 import 它们**：``conversion.registry`` 要 import 本模块，
而注册表有一条机械测试（§十三）禁止它拉起 ``PIL`` / ``fitz``。
所以枚举值在本模块里写字面量，由测试逐条与实现对账
（``tests/test_conversion_options.py::test_enums_agree_with_the_implementations``）。
数值边界取自 ``config.settings`` —— 那是配置常量，不是 I/O。

## 不变式：schema 是体验，不是防线

    spec.min >= 服务端解析器的下界   且   spec.max <= 服务端解析器的上界

即 schema 只允许**比服务端更窄**的范围。界面因此不会摆出一个必然被拒的
输入框，而资源上限仍然由「值最终必须穿过既有解析器」保证（§四十五）：
``validate_payload`` 通过的每一个值，之后都要再交给
``routers/params.py`` / ``routers/pdf_params.py`` 里那个已经存在的解析器。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

from config import settings
from conversion.capability import (
    DYNAMIC_FONTS,
    OPERATION_CONVERSION,
    TYPE_BOOLEAN,
    TYPE_ENUM,
    TYPE_INTEGER,
    TYPE_NUMBER,
    TYPE_STRING,
    Capability,
    OptionSpec,
)

__all__ = [
    "DEFAULT_QUALITY",
    "DPI_CUSTOM",
    "DPI_ORIGINAL",
    "DPI_PRESETS",
    "IMAGE_CONVERT",
    "IMAGE_TO_PDF",
    "MARKUP_CONVERT",
    "MARKUP_TO_PDF",
    "MAX_CUSTOM_MM",
    "MAX_OPTIONS_BYTES",
    "MAX_OPTION_KEYS",
    "MAX_TARGET_MB",
    "METADATA_DEFAULT",
    "METADATA_KEEP",
    "METADATA_MODES",
    "METADATA_REMOVE",
    "MIN_CUSTOM_MM",
    "MIN_TARGET_MB",
    "OFFICE_TO_PDF",
    "PDF_TO_DOCX",
    "RESIZE_CUSTOM",
    "RESIZE_MAX_EDGE_BY_MODE",
    "RESIZE_ORIGINAL",
    "RESIZE_PRESETS",
    "ROTATIONS",
    "ROTATION_LABELS",
    "TEXT_CONVERT",
    "TEXT_TO_DOCX",
    "TEXT_TO_PDF",
    "TXT_ORIENTATION_VALUES",
    "TXT_PAGE_SIZE_VALUES",
    "OptionError",
    "converter_keys",
    "operation_ids",
    "operation_options",
    "options_for",
    "union_specs",
    "validate_payload",
]

# ----------------------------------------------------------------------
# 家族标识 —— 与执行层 ``services.conversion_service.CONVERTERS`` 的键一一对应
#
# 一个「家族」= 一个叶子实现。选项按家族定义，执行按家族分派，
# 于是「加一种格式」不需要新写一份选项解析。
# ----------------------------------------------------------------------

IMAGE_CONVERT = "image.convert"
IMAGE_TO_PDF = "image.to_pdf"
#: SVG → PDF 单独一族，**不是** ``image.to_pdf`` 的重复实现：
#: 那条路把位图贴进 PDF，这条路导出真正的矢量页（文字可选可搜）。
#: 两者选项也不同 —— 这一族一个参数都没有，页面尺寸就是 SVG 自己声明的
#: 那个尺寸（见 ``compressors.svg.svg_to_pdf``）。
#: 反过来，SVG → PNG/JPG/WEBP 共用 ``IMAGE_CONVERT``：渲染出来的就是一张
#: 普通的 ``PIL.Image``，后面每一步（裁剪/缩放/旋转/翻转/质量/目标大小）
#: 与位图完全一样，另起一族才是真的重复。
SVG_TO_PDF = "svg.to_pdf"
OFFICE_TO_PDF = "office.to_pdf"
PDF_TO_DOCX = "pdf.to_docx"
TEXT_TO_PDF = "text.to_pdf"
TEXT_TO_DOCX = "text.to_docx"
#: TXT → HTML / Markdown：与 ``TEXT_TO_DOCX`` 分开，因为引擎不同
#: （``markup_render`` 而不是 ``docx_writer``），选项也不同（这一族没有选项）。
#: 与图片组按目标分成 ``image.convert`` / ``image.to_pdf`` 是同一个分法。
TEXT_CONVERT = "text.convert"
MARKUP_TO_PDF = "markup.to_pdf"
MARKUP_CONVERT = "markup.convert"

# ----------------------------------------------------------------------
# 边界
# ----------------------------------------------------------------------

#: §九：质量 10–100，默认 85。
#: 服务端解析器（``routers/params.parse_quality_value``）允许 1–100，
#: 这里更窄 —— 见模块 docstring 的不变式。
MIN_QUALITY = 10
MAX_QUALITY = 100
DEFAULT_QUALITY = 85

MIN_EDGE = 1
MAX_EDGE = settings.MAX_IMAGE_EDGE

MIN_CUSTOM_MM = settings.PDF_MIN_CUSTOM_MM
MAX_CUSTOM_MM = settings.PDF_MAX_CUSTOM_MM

#: 图片 DPI：只写密度信息，不重采样，所以上限给得宽。
#: 1 是 Pillow 能表达的下界；再小没有意义。
MIN_DPI = 1
MAX_DPI = 1200

#: 目标大小的上下界（§二十九）。取自 ``config``，与
#: ``routers/params.parse_target_bytes`` 读的是同一份 —— 见那边的说明。
MIN_TARGET_BYTES = settings.MIN_TARGET_BYTES
MAX_TARGET_BYTES = settings.MAX_TARGET_BYTES


def _human_size(size: int) -> str:
    """把字节数写成人看的大小，只用于**预设档的标签**。

    只用 1024 进制且不留小数：预设档全是整的 500 KB / 1 MB / 10 MB，
    写成 ``0.5 MB`` 或 ``500.0 KB`` 都只是把同一个数说得更难读。
    这个函数不参与任何校验，也不是给结果卡用的格式化器
    （那个在前端，``formatBytes``）。
    """
    for unit, scale in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= scale and size % scale == 0:
            return f"{size // scale} {unit}"
    return f"{size // 1024} KB"

MIN_FONT_SIZE = settings.TXT_MIN_FONT_SIZE
MAX_FONT_SIZE = settings.TXT_MAX_FONT_SIZE

#: PDF 压缩的「自定义目标大小」范围（MB）。与 ``routers/pdf_params`` 的
#: 解析器同源：那一侧从这里取，所以界面上的滑杆范围与真正接受的区间
#: 不可能各说各话。
MIN_TARGET_MB = settings.PDF_MIN_TARGET_MB
MAX_TARGET_MB = settings.PDF_MAX_TARGET_MB

#: ``options`` 这个表单字段的尺寸上限（§四十五：不能让 options 变成
#: 一条绕过限制的旁路，也不能让它变成一次免费的 CPU 攻击）。
MAX_OPTIONS_BYTES = 8 * 1024

#: 键数量上限。真实表单最多十来个键，64 已经是两个数量级的余量。
MAX_OPTION_KEYS = 64

#: 键名长度上限（``resize.keep_aspect`` 才 18 个字符）。
MAX_OPTION_KEY_CHARS = 64

# ----------------------------------------------------------------------
# 按目标格式裁剪用的集合
# ----------------------------------------------------------------------

#: DPI 说明里要点名的格式，分「支持」与「不支持」两组。
#:
#: **必须与编码器的能力表一一对应**（``encoder.DENSITY_FORMATS`` 及其补集）：
#: 少列一个，界面上就会出现「说明里没说不行、一提交却报不支持」的旋钮 ——
#: 真机验收时就是这么发现 WEBP 被漏掉的。所以这两组名字不由人反复手抄，
#: 而是由 ``test_image_formats`` 里的一条测试还原成内部名后逐一核对。
#:
#: HEIC 在「不支持」那一组，但理由与 WEBP / GIF / BMP / ICO **都不同**，
#: 值得写下来：它不是「Pillow 拒绝这个参数」，而是**照单收下再默默丢掉** ——
#: 实测 ``save(dpi=(300,300))`` 不报错，回读 ``img.info['dpi']`` 是 ``None``。
#: 这种「不报错但不生效」比报错更糟：用户设了 300 DPI，拿到一张没有密度的图，
#: 而全程没有任何一处告诉他这件事。
_DENSITY_OK_LABELS = ("JPG", "PNG", "TIFF")
_DENSITY_NO_LABELS = ("WEBP", "GIF", "BMP", "ICO", "HEIC")

#: 有损格式：只有它们才有「质量」这个概念。
#: PNG / BMP / GIF / TIFF / ICO 是无损或索引色，摆一个质量滑杆等于骗人。
#: HEIC 是**有损**的（实测同一张 384×384 的细节图：quality=10 出 2 988 字节，
#: quality=95 出 198 164 字节），所以它有质量这个维度。
_LOSSY_TARGETS = frozenset({"jpg", "webp", "heic"})

#: 能携带密度（DPI）信息的容器。
_DENSITY_TARGETS = frozenset({"jpg", "png", "tiff"})

#: 有 EXIF 容器的格式。GIF / BMP / ICO 没有 EXIF，
#: 「保留元数据」在那里是无从谈起的。
#: HEIC **有**（实测 ``save(exif=...)`` 写入后回读 Make / Model 都在），
#: 所以它能谈保留还是清除 —— 这一点对手机照片尤其要紧，
#: 那正是带 GPS 的那一类。
_EXIF_TARGETS = frozenset({"jpg", "webp", "png", "tiff", "heic"})

#: 需要按目标裁剪的键 -> 允许出现该键的目标集合。
_TARGET_GATED: dict[str, frozenset[str]] = {
    "quality": _LOSSY_TARGETS,
    "dpi": _DENSITY_TARGETS,
    "dpi.custom": _DENSITY_TARGETS,
    "metadata": _EXIF_TARGETS,
}

# ----------------------------------------------------------------------
# 旋转 / 元数据 / DPI / 尺寸预设的词表
#
# schema 与**绑定层共用这一份**：``_ROTATION.values`` 由 ``ROTATIONS`` 生成，
# 路由里的绑定层再把用户选中的字符串转成 int。两处各写一遍就会出现
# 「界面能选 90°，提交上来却说不认识」这种对不上的状态。
# ----------------------------------------------------------------------

ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)

ROTATION_LABELS: dict[int, str] = {
    0: "不旋转",
    90: "顺时针 90°",
    180: "180°",
    270: "顺时针 270°",
}

#: ``rotation`` 的第五个取值：角度由 ``rotation.angle`` 给出。
#: 与 ``dpi`` / ``dpi.custom`` 完全同型 —— 枚举选「要哪种」，配套键给数值。
ROTATION_CUSTOM = "custom"
ROTATION_CUSTOM_LABEL = "自定义角度"

#: 裁剪比例。与 ``compressors.cropper.CROP_RATIO_VALUES`` 必须逐项相等，
#: 由 ``test_conversion_options.py`` 还原后对账（本模块不 import 实现，
#: 理由见文件头的「为什么它只 import config」）。
CROP_RATIOS: tuple[str, ...] = ("free", "1:1", "4:3", "3:2", "16:9", "custom")

CROP_RATIO_LABELS: dict[str, str] = {
    "free": "自由裁剪",
    "1:1": "1:1 正方形",
    "4:3": "4:3",
    "3:2": "3:2",
    "16:9": "16:9",
    "custom": "自定义比例",
}

#: 百分比缩放的边界。下界 1 与 ``compute_target_size`` 的「必须大于 0」一致；
#: 上界只是挡掉「放大 1000 倍」这种把内存打爆的输入，不是画质判断。
MIN_PERCENT = 1
MAX_PERCENT = 400

METADATA_KEEP = "keep"
METADATA_REMOVE = "remove"
METADATA_MODES: tuple[str, ...] = (METADATA_KEEP, METADATA_REMOVE)

#: 统一转换中心**没有选**元数据时按哪一个走。
#:
#: 这是「保留 / 清除」的唯一一份默认值：schema 里那一项的 ``default`` 与
#: ``ConversionOptions.metadata`` 的兜底都读它（第十阶段 C §三）。两者
#: 各写一个字面量的话，表现是「界面显示『保留』已选中、接口不传 options
#: 时却按『清除』处理」—— 同一件事两套说法，而且不报错。
#:
#: 老页面（压缩 / 尺寸调整 / 旧转换页）**不读这个值**：它们走
#: ``PipelineOptions`` 的默认 ``remove``，那是第一阶段定下的隐私取舍
#: （不把拍摄地点带进结果），不能因为这里统一就跟着改。
METADATA_DEFAULT = METADATA_KEEP

#: DPI 的两个哨兵值：``original`` 表示不写入密度信息，``custom`` 表示读
#: ``dpi.custom``。中间的预设值在 :data:`DPI_PRESETS` 里。
DPI_ORIGINAL = "original"
DPI_CUSTOM = "custom"
DPI_PRESETS: tuple[int, ...] = (72, 96, 150, 300)

RESIZE_ORIGINAL = "original"
RESIZE_CUSTOM = "custom"
RESIZE_PERCENT = "percent"
#: 尺寸预设档 -> ``(值, 中文名, 长边像素)``。预设档**只缩不放**：
#: 比它还小的图不会被拉大到这个长边（§九「不要强行改变原始尺寸」）。
RESIZE_PRESETS: tuple[tuple[str, str, int], ...] = (
    ("small", "小", 1024),
    ("medium", "中", 1600),
    ("large", "大", 2560),
)

#: 第十阶段 A §十九 点名的「最大宽度」预设档。
#:
#: 与上面三档的差别在**量哪条边**，也在**允不允许放大**：这里定的是**宽**，
#: 高按比例走，而且宽度就是用户点的那个数 —— 800 宽的图选 1920 会变成
#: 1920 宽。上面三档是「长边不超过」，只缩不放。
#:
#: 两组的差别是刻意的，不是笔误：``small/medium/large`` 是「别把我的图撑大」，
#: 「宽 1920」是「我就要 1920 宽」。都不裁剪。
#:
#: 这条区分曾经在两处说反（本段的旧注释、下面 ``_RESIZE_MODE`` 的 label 与
#: help 都写着「只缩不放」），而界面上的下拉框正是照 label 渲染的 ——
#: 第十阶段 C 的界面审计对照 ``conversion_params._resize_from`` 逐条核出来
#: 并改正。实现一行没动，错的只是说法。
#:
#: 为什么不合并进 ``RESIZE_PRESETS``：那三档的语义是「长边」，值是
#: 1024/1600/2560，改成宽会让第九阶段已经上线的行为变味；而
#: 640/1280/1920/2560 是 §十九 字面点名的另一组。两组并存，各说各的边。
RESIZE_WIDTH_PRESETS: tuple[tuple[str, str, int], ...] = (
    ("w640", "宽 640", 640),
    ("w1280", "宽 1280", 1280),
    ("w1920", "宽 1920", 1920),
    ("w2560", "宽 2560", 2560),
)

RESIZE_MAX_EDGE_BY_MODE: dict[str, int] = {
    mode: edge for mode, _, edge in RESIZE_PRESETS
}

RESIZE_MAX_WIDTH_BY_MODE: dict[str, int] = {
    mode: edge for mode, _, edge in RESIZE_WIDTH_PRESETS
}

#: 尺寸档位的全部取值 —— 绑定层用它判断「这是个预设档吗」，
#: 不用把三张表抄一遍。
RESIZE_PRESET_MODES: frozenset[str] = frozenset(
    RESIZE_MAX_EDGE_BY_MODE
) | frozenset(RESIZE_MAX_WIDTH_BY_MODE)

#: 百分比缩放档 -> ``(值, 中文名, 百分比)``（§十九）。
RESIZE_PERCENT_PRESETS: tuple[tuple[str, str, int], ...] = (
    ("p25", "25%", 25),
    ("p50", "50%", 50),
    ("p75", "75%", 75),
    ("p100", "100%（原尺寸）", 100),
)

RESIZE_PERCENT_BY_MODE: dict[str, int] = {
    mode: percent for mode, _, percent in RESIZE_PERCENT_PRESETS
}

#: width / height 都给时，怎么处理与目标框的关系（§十六 / §十七）。
#: 这**不是**上面那个 ``resize.mode``（档位选择器），而是「放进 / 填满 / 拉伸」。
#: 名字错开是必要的：``resize.mode`` 在第九阶段已经上线且被测试钉住，
#: 不能再拿来表达第二件事。
RESIZE_FIT = "fit"
RESIZE_FILL = "fill"
RESIZE_STRETCH = "stretch"

# ----------------------------------------------------------------------
# 图片选项（§九）
# ----------------------------------------------------------------------

_QUALITY = OptionSpec(
    key="quality",
    type=TYPE_INTEGER,
    label="图片质量",
    default=DEFAULT_QUALITY,
    min=MIN_QUALITY,
    max=MAX_QUALITY,
    unit="%",
    # §二十七 的七档。值仍是这个整数键自己的取值，只是给界面一组常用落点，
    # 所以它不改变 type（不像 enum 那样限死）。
    presets=tuple(
        (str(stop), settings.QUALITY_PRESET_LABELS[stop])
        for stop in settings.QUALITY_PRESET_STOPS
    ),
    help="数值越高越清晰、文件也越大；只对 JPG / WebP 有效",
)

#: 目标大小（§二十九）。第六档「自定义」= 允许填任意字节数，
#: 由 min/max 承担，不是预设里的一个值。
_TARGET_BYTES = OptionSpec(
    key="target_bytes",
    type=TYPE_INTEGER,
    label="目标大小",
    default=None,
    min=MIN_TARGET_BYTES,
    max=MAX_TARGET_BYTES,
    unit="B",
    presets=tuple(
        (str(size), f"≤ {_human_size(size)}")
        for size in settings.TARGET_SIZE_PRESET_BYTES
    ),
    help="会自动搜索质量（必要时再缩尺寸）让结果不超过它；达不到会如实说明",
)

_RESIZE_MODE = OptionSpec(
    key="resize.mode",
    type=TYPE_ENUM,
    label="尺寸",
    default=RESIZE_ORIGINAL,
    values=(
        (RESIZE_ORIGINAL, "保持原尺寸"),
        *(
            (value, f"{label}（长边 {edge} 像素）")
            for value, label, edge in RESIZE_PRESETS
        ),
        *(
            (value, f"{label}（等比，宽度就取这个值）")
            for value, label, edge in RESIZE_WIDTH_PRESETS
        ),
        *(
            (value, f"缩放到 {label}")
            for value, label, percent in RESIZE_PERCENT_PRESETS
        ),
        (RESIZE_PERCENT, "自定义百分比"),
        (RESIZE_CUSTOM, "自定义宽高"),
    ),
    help=(
        "「小 / 中 / 大」按长边缩放，只缩不放，小图不会被拉糊；"
        "「宽 640 ~ 宽 2560」按宽度缩放，宽度就是所选的值，小图会被放大。"
        "两档都不裁剪。"
    ),
)

_RESIZE_WIDTH = OptionSpec(
    key="resize.width",
    type=TYPE_INTEGER,
    label="宽度",
    min=MIN_EDGE,
    max=MAX_EDGE,
    unit="像素",
    visible_when=(("resize.mode", "custom"),),
)

_RESIZE_HEIGHT = OptionSpec(
    key="resize.height",
    type=TYPE_INTEGER,
    label="高度",
    min=MIN_EDGE,
    max=MAX_EDGE,
    unit="像素",
    visible_when=(("resize.mode", "custom"),),
)

_RESIZE_KEEP_ASPECT = OptionSpec(
    key="resize.keep_aspect",
    type=TYPE_BOOLEAN,
    label="保持宽高比",
    default=True,
    help="取消后图片会被拉伸到指定的宽高",
)

_DPI = OptionSpec(
    key="dpi",
    type=TYPE_ENUM,
    label="分辨率",
    default=DPI_ORIGINAL,
    values=(
        (DPI_ORIGINAL, "保持原样"),
        *((str(value), f"{value} DPI") for value in DPI_PRESETS),
        (DPI_CUSTOM, "自定义"),
    ),
    help=(
        "只写入图片的密度信息，不会重新采样像素；仅 "
        + " / ".join(_DENSITY_OK_LABELS)
        + " 支持，"
        + " / ".join(_DENSITY_NO_LABELS)
        + " 都不支持"
    ),
)

_DPI_CUSTOM = OptionSpec(
    key="dpi.custom",
    type=TYPE_INTEGER,
    label="自定义 DPI",
    min=MIN_DPI,
    max=MAX_DPI,
    visible_when=(("dpi", DPI_CUSTOM),),
)

_ROTATION = OptionSpec(
    key="rotation",
    type=TYPE_ENUM,
    label="旋转",
    default=str(ROTATIONS[0]),
    values=tuple((str(angle), ROTATION_LABELS[angle]) for angle in ROTATIONS)
    + ((ROTATION_CUSTOM, ROTATION_CUSTOM_LABEL),),
)

# ----------------------------------------------------------------------
# 第十阶段 A：任意角度旋转 / 翻转 / 裁剪 / 缩放模式
# ----------------------------------------------------------------------

#: 自定义旋转角度的取值范围（度）。
MIN_ROTATION_ANGLE = 0
MAX_ROTATION_ANGLE = 359

_ROTATION_ANGLE = OptionSpec(
    key="rotation.angle",
    type=TYPE_INTEGER,
    label="自定义角度",
    min=MIN_ROTATION_ANGLE,
    max=MAX_ROTATION_ANGLE,
    unit="度",
    visible_when=(("rotation", ROTATION_CUSTOM),),
    help="顺时针。90 的整数倍是无损的，其他角度会重采样并可能留下透明角",
)

_ROTATION_EXPAND = OptionSpec(
    key="rotation.expand",
    type=TYPE_BOOLEAN,
    label="自动扩大画布",
    default=True,
    help="关闭后画布尺寸不变，转到画布外的部分会被切掉",
)

_FLIP = OptionSpec(
    key="flip",
    type=TYPE_ENUM,
    label="翻转",
    default="none",
    values=(
        ("none", "不翻转"),
        ("horizontal", "水平翻转（左右镜像）"),
        ("vertical", "垂直翻转（上下镜像）"),
        ("both", "水平 + 垂直"),
    ),
    help="镜像只重排像素、不重采样，没有画质损失",
)

_CROP_RATIO = OptionSpec(
    key="crop.ratio",
    type=TYPE_ENUM,
    label="裁剪比例",
    default="free",
    values=tuple(
        (value, CROP_RATIO_LABELS[value]) for value in CROP_RATIOS
    ),
    help="选好比例后只需填宽度，高度会按比例自动算出来并如实说明",
)

_CROP_CUSTOM_RATIO = OptionSpec(
    key="crop.custom_ratio",
    type=TYPE_STRING,
    label="自定义比例",
    visible_when=(("crop.ratio", "custom"),),
    help="写成「宽:高」，例如 7:5",
)

#: 裁剪区域的四个几何键。
#:
#: **有意不加 ``visible_when``**：它们该不该出现取决于「用户打没打开裁剪」，
#: 而那是一个布尔开关，不是枚举。``visible_when`` 的契约要求父键是枚举
#: （``test_visible_when_only_points_at_sibling_keys`` 钉住了这一点，
#: 而那条断言不该为了这个功能放宽）。裁剪区的显隐由前端的裁剪面板自己管，
#: 后端只负责：这四个键出现且合法，就裁。
_CROP_X = OptionSpec(
    key="crop.x",
    type=TYPE_INTEGER,
    label="起点 X",
    default=0,
    min=0,
    max=MAX_EDGE,
    unit="像素",
)

_CROP_Y = OptionSpec(
    key="crop.y",
    type=TYPE_INTEGER,
    label="起点 Y",
    default=0,
    min=0,
    max=MAX_EDGE,
    unit="像素",
)

_CROP_WIDTH = OptionSpec(
    key="crop.width",
    type=TYPE_INTEGER,
    label="裁剪宽度",
    min=1,
    max=MAX_EDGE,
    unit="像素",
)

_CROP_HEIGHT = OptionSpec(
    key="crop.height",
    type=TYPE_INTEGER,
    label="裁剪高度",
    min=1,
    max=MAX_EDGE,
    unit="像素",
)

_RESIZE_PERCENT_CUSTOM = OptionSpec(
    key="resize.percent",
    type=TYPE_INTEGER,
    label="缩放百分比",
    min=MIN_PERCENT,
    max=MAX_PERCENT,
    unit="%",
    visible_when=(("resize.mode", RESIZE_PERCENT),),
    help="100 保持原尺寸，大于 100 是放大",
)

_RESIZE_FIT = OptionSpec(
    key="resize.fit",
    type=TYPE_ENUM,
    label="适配方式",
    default=RESIZE_FIT,
    values=(
        (RESIZE_FIT, "完整放入（可能留白）"),
        (RESIZE_FILL, "填满并居中裁剪"),
        (RESIZE_STRETCH, "拉伸到指定尺寸（会变形）"),
    ),
    visible_when=(("resize.mode", RESIZE_CUSTOM),),
    help="只在宽高都填写时才有区别",
)

_METADATA = OptionSpec(
    key="metadata",
    type=TYPE_ENUM,
    label="元数据",
    # 与 ``ConversionOptions.metadata`` 的兜底读同一个常量（第十阶段 C §三）
    default=METADATA_DEFAULT,
    values=(
        (METADATA_KEEP, "保留"),
        (METADATA_REMOVE, "清除"),
    ),
    help="清除只移除可安全剥离的拍摄信息（EXIF），不做像素级擦除",
)

_IMAGE_OPTIONS: tuple[OptionSpec, ...] = (
    _QUALITY,
    _TARGET_BYTES,
    _RESIZE_MODE,
    _RESIZE_WIDTH,
    _RESIZE_HEIGHT,
    _RESIZE_KEEP_ASPECT,
    _RESIZE_PERCENT_CUSTOM,
    _RESIZE_FIT,
    _CROP_RATIO,
    _CROP_CUSTOM_RATIO,
    _CROP_X,
    _CROP_Y,
    _CROP_WIDTH,
    _CROP_HEIGHT,
    _DPI,
    _DPI_CUSTOM,
    _ROTATION,
    _ROTATION_ANGLE,
    _ROTATION_EXPAND,
    _FLIP,
    _METADATA,
)

# ----------------------------------------------------------------------
# 排版选项（§十）—— 图片 → PDF、Markdown/HTML → PDF 共用
# ----------------------------------------------------------------------

_PAGE_SIZE = OptionSpec(
    key="page_size",
    type=TYPE_ENUM,
    label="页面大小",
    default="auto",
    values=(
        ("auto", "原图尺寸（页面跟随图片）"),
        ("a4", "A4"),
        ("a5", "A5"),
        ("letter", "Letter"),
        ("custom", "自定义"),
    ),
)

_PAGE_WIDTH_MM = OptionSpec(
    key="page_size.width_mm",
    type=TYPE_NUMBER,
    label="自定义宽度",
    min=MIN_CUSTOM_MM,
    max=MAX_CUSTOM_MM,
    unit="毫米",
    visible_when=(("page_size", "custom"),),
)

_PAGE_HEIGHT_MM = OptionSpec(
    key="page_size.height_mm",
    type=TYPE_NUMBER,
    label="自定义高度",
    min=MIN_CUSTOM_MM,
    max=MAX_CUSTOM_MM,
    unit="毫米",
    visible_when=(("page_size", "custom"),),
)

_ORIENTATION = OptionSpec(
    key="orientation",
    type=TYPE_ENUM,
    label="页面方向",
    default="auto",
    values=(("auto", "自动"), ("portrait", "纵向"), ("landscape", "横向")),
)

_FIT = OptionSpec(
    key="fit",
    type=TYPE_ENUM,
    label="图片适应方式",
    default="contain",
    values=(
        ("contain", "适应页面：完整显示，四周可能留白"),
        ("fill", "填充页面：铺满整页，超出部分会被裁掉"),
        ("original", "原始大小：1 像素 = 1 磅，可能超出页面"),
    ),
)

_MARGIN = OptionSpec(
    key="margin",
    type=TYPE_ENUM,
    label="页边距",
    default="none",
    # 标签里的毫米数与 ``config.settings.PDF_MARGINS`` 是同一组值，
    # 由测试对账（``test_margin_labels_match_the_configured_values``）。
    values=(("none", "无"), ("small", "5 毫米"), ("medium", "10 毫米"), ("large", "20 毫米")),
)

_IMAGE_TO_PDF_OPTIONS: tuple[OptionSpec, ...] = (
    _PAGE_SIZE,
    _PAGE_WIDTH_MM,
    _PAGE_HEIGHT_MM,
    _ORIENTATION,
    _MARGIN,
    _FIT,
)

# ----------------------------------------------------------------------
# 文本选项 —— TXT 家族
#
# 页面大小与方向沿用 ``TxtOptions`` 的词表（``settings.TXT_PAGE_SIZES``），
# 与图片那套**不是同一组值**：TXT 排版没有「跟随原图尺寸」这一说。
# ----------------------------------------------------------------------

_FONT = OptionSpec(
    key="font",
    type=TYPE_ENUM,
    label="字体",
    values=(),
    dynamic=DYNAMIC_FONTS,
    help="只列出服务器上真实安装的字体",
)

_FONT_SIZE = OptionSpec(
    key="font_size",
    type=TYPE_INTEGER,
    label="字号",
    default=settings.TXT_DEFAULT_FONT_SIZE,
    min=MIN_FONT_SIZE,
    max=MAX_FONT_SIZE,
    unit="磅",
)

_TXT_PAGE_SIZE = OptionSpec(
    key="page_size",
    type=TYPE_ENUM,
    label="页面大小",
    default=settings.TXT_PAGE_SIZES[0],
    values=(("a4", "A4"), ("a5", "A5"), ("letter", "Letter")),
)

_TXT_ORIENTATION = OptionSpec(
    key="orientation",
    type=TYPE_ENUM,
    label="页面方向",
    default=settings.TXT_ORIENTATIONS[0],
    values=(("portrait", "纵向"), ("landscape", "横向")),
)

#: TXT 词表的取值域。绑定层靠它判断**同名的 ``page_size`` / ``orientation``
#: 该不该同时喂给 TXT 那套解析器：``page_size=auto`` 只对「图片 → PDF」成立，
#: 对 TXT 是个无效值，绝不能传下去（传了会得到一句用户看不懂的参数错误）。
TXT_PAGE_SIZE_VALUES: tuple[str, ...] = tuple(v for v, _ in _TXT_PAGE_SIZE.values)
TXT_ORIENTATION_VALUES: tuple[str, ...] = tuple(v for v, _ in _TXT_ORIENTATION.values)

_TXT_PDF_OPTIONS: tuple[OptionSpec, ...] = (
    _FONT,
    _FONT_SIZE,
    _TXT_PAGE_SIZE,
    _TXT_ORIENTATION,
)

#: TXT → DOCX 只给字体。
#:
#: ``office/docx_writer.build_docx`` 目前只设置 Normal 样式的东亚字体，
#: **字号没有实现路径** —— 发一个点了没反应的控件比不发更糟，
#: 所以这里不放 ``font_size``。将来写入器支持了字号，在这里加一行即可。
_TXT_DOCX_OPTIONS: tuple[OptionSpec, ...] = (_FONT,)

#: TXT → HTML / Markdown：只换一层写法，没有任何可调参数。
#:
#: 页面大小、方向、字体都是**排版**选项 —— HTML 与 Markdown 里没有「页」
#: 这个概念，摆在那里只会是一排点了没反应的控件。字号同理：
#: 结果是一份靠 CSS 决定观感的网页，用户改字号应该去改 CSS。
_TXT_CONVERT_OPTIONS: tuple[OptionSpec, ...] = ()

# ----------------------------------------------------------------------
# Markdown / HTML 选项
# ----------------------------------------------------------------------

_MARKUP_PDF_OPTIONS: tuple[OptionSpec, ...] = (
    _TXT_PAGE_SIZE,
    _TXT_ORIENTATION,
    _MARGIN,
    _FONT,
    _FONT_SIZE,
)

#: HTML ↔ Markdown ↔ 纯文本：结构互转，没有可调参数。
_MARKUP_CONVERT_OPTIONS: tuple[OptionSpec, ...] = ()

# ----------------------------------------------------------------------
# PDF 操作（第九阶段第 11 步，决策 B）—— 工具类条目的参数
#
# 这些条目不进统一队列，**执行走各自已有的接口**。所以这里的 schema 不是
# 「服务端认哪些键」，而是「那个端点的 Form 字段长什么样」。两者的一致性
# 由 ``tests/test_conversion_operations.py`` 拿 ``inspect.signature`` 逐字对账：
# 少一个字段、多一个旋钮、改一个枚举取值都会在那里当场红。
#
# 键名一律**照抄端点的参数名**（``custom_width_mm``，不是转换侧的
# ``page_size.width_mm``）：前端会把 schema 里的键原样发回那个端点，
# 中间少一层翻译就少一处能写错的地方。同一个概念在两处叫两个名字是
# 有意接受的代价 —— 它们本来就是两个接口。
#
# 页面大小 / 方向 / 适应方式 / 页边距四项**直接复用**图片转 PDF 的那几个
# spec 对象：同一个端点、同一套取值域，再抄一份迟早会有一份忘了改。
# ----------------------------------------------------------------------

#: ``/api/pdf/from-images`` 的自定义尺寸键名与转换侧不同，所以是新的 spec；
#: 数值范围仍与 ``_PAGE_WIDTH_MM`` 共用同一对常量。
_OP_CUSTOM_WIDTH_MM = OptionSpec(
    key="custom_width_mm",
    type=TYPE_NUMBER,
    label="自定义宽度",
    min=MIN_CUSTOM_MM,
    max=MAX_CUSTOM_MM,
    unit="毫米",
    visible_when=(("page_size", "custom"),),
)

_OP_CUSTOM_HEIGHT_MM = OptionSpec(
    key="custom_height_mm",
    type=TYPE_NUMBER,
    label="自定义高度",
    min=MIN_CUSTOM_MM,
    max=MAX_CUSTOM_MM,
    unit="毫米",
    visible_when=(("page_size", "custom"),),
)

#: 拆分方式。三个取值与 ``pdf.splitter.SPLIT_MODES`` 是同一套，
#: 中文名与 ``SPLIT_MODE_LABELS`` 是同一套（在本模块里抄一份是因为
#: ``pdf.splitter`` 会拉起 PyMuPDF，注册表碰不得 —— 由测试对账）。
_OP_SPLIT_MODE = OptionSpec(
    key="mode",
    type=TYPE_ENUM,
    label="拆分方式",
    default="every",
    values=(
        ("every", "每页一个 PDF"),
        ("ranges", "按范围拆分"),
        ("selected", "自定义页面"),
    ),
)

#: 页面表达式。**不使用 ``visible_when``**：它只在「按范围 / 自定义页面」
#: 两种方式下才有意义，而 ``visible_when`` 的语义是「全部成立」，
#: 表达不了「二者之一」。与其发一个在「每页一个」时也亮着的必填框，
#: 不如把它写成一句帮助文案 —— 端点的 Form 说明就是这么写的。
_OP_SPLIT_PAGES = OptionSpec(
    key="pages",
    type=TYPE_STRING,
    label="页面",
    help="按范围拆分时每行写一个范围（如 1-3）；自定义页面时写 1,3,5,8；每页一个时不需要填",
)

_OP_PAGES = OptionSpec(
    key="pages",
    type=TYPE_STRING,
    label="页面",
    required=True,
    help="例如 1,3,5,8 或 2,4,7-9",
)

_OP_COMPRESS_LEVEL = OptionSpec(
    key="level",
    type=TYPE_ENUM,
    label="压缩等级",
    default="balanced",
    values=(("light", "轻度"), ("balanced", "平衡"), ("strong", "高压缩")),
)

_OP_COMPRESS_TARGET = OptionSpec(
    key="target",
    type=TYPE_ENUM,
    label="目标大小",
    default="none",
    values=(
        ("none", "不限制"),
        ("500kb", "500 KB"),
        ("1mb", "1 MB"),
        ("2mb", "2 MB"),
        ("5mb", "5 MB"),
        ("10mb", "10 MB"),
        ("20mb", "20 MB"),
        ("custom", "自定义"),
    ),
)

_OP_COMPRESS_TARGET_MB = OptionSpec(
    key="target_mb",
    type=TYPE_NUMBER,
    label="自定义目标大小",
    min=MIN_TARGET_MB,
    max=MAX_TARGET_MB,
    unit="MB",
    visible_when=(("target", "custom"),),
)

#: 操作 ID -> 该操作的参数。**键是能力 ID**（``op.pdf-merge``），与
#: ``registry.CAPABILITIES`` 里的条目一一对应，注册表按同一个键取用。
#:
#: 每一条都必须在这里出现，哪怕它一个参数都没有（``op.pdf-merge`` 就是
#: 空的）：注册表用的是**硬下标**，漏登记一条会在 import 那一刻就炸，
#: 而不是悄悄发出一张没有参数的面板。
_OPERATION_OPTIONS: dict[str, tuple[OptionSpec, ...]] = {
    "op.pdf-merge": (),
    "op.pdf-split": (_OP_SPLIT_MODE, _OP_SPLIT_PAGES),
    "op.pdf-delete-pages": (_OP_PAGES,),
    "op.pdf-extract-pages": (_OP_PAGES,),
    "op.pdf-compress": (_OP_COMPRESS_LEVEL, _OP_COMPRESS_TARGET, _OP_COMPRESS_TARGET_MB),
    "op.image-images-to-pdf": (
        _PAGE_SIZE,
        _OP_CUSTOM_WIDTH_MM,
        _OP_CUSTOM_HEIGHT_MM,
        _ORIENTATION,
        _MARGIN,
        _FIT,
    ),
    # 元数据查看器**一个参数都没有**，而且这是有意的：它只是把文件里
    # 已经写着的东西读出来。摆一个「要读哪几项」的勾选框在这里，用户
    # 勾完发现结果一模一样，比没有控件糟得多。
    "op.image-metadata": (),
}


def operation_options(operation_id: str) -> tuple[OptionSpec, ...]:
    """某个操作条目的参数。**硬下标**：漏登记会在 import 期当场抛错。"""
    return _OPERATION_OPTIONS[operation_id]


def operation_ids() -> tuple[str, ...]:
    """登记了参数的操作 ID，供测试与注册表对账。"""
    return tuple(_OPERATION_OPTIONS)


# ----------------------------------------------------------------------
# 家族 → 选项
# ----------------------------------------------------------------------

_FAMILY_OPTIONS: dict[str, tuple[OptionSpec, ...]] = {
    IMAGE_CONVERT: _IMAGE_OPTIONS,
    IMAGE_TO_PDF: _IMAGE_TO_PDF_OPTIONS,
    # SVG → PDF **一个参数都没有**，而且这是有意的，不是「还没来得及加」：
    # 页面尺寸就是 SVG 自己声明的那个尺寸。摆一个「页面大小」下拉在这里，
    # 用户选了 A4 却发现毫无变化，比没有这个控件糟得多。
    # 那条路的资源上限由 ``svg.svg_to_pdf`` 自己把关（矢量缩不了，只能拒）。
    SVG_TO_PDF: (),
    OFFICE_TO_PDF: (),
    PDF_TO_DOCX: (),
    TEXT_TO_PDF: _TXT_PDF_OPTIONS,
    TEXT_TO_DOCX: _TXT_DOCX_OPTIONS,
    TEXT_CONVERT: _TXT_CONVERT_OPTIONS,
    MARKUP_TO_PDF: _MARKUP_PDF_OPTIONS,
    MARKUP_CONVERT: _MARKUP_CONVERT_OPTIONS,
}


def converter_keys() -> tuple[str, ...]:
    """所有家族标识，供测试与执行层对账。"""
    return tuple(_FAMILY_OPTIONS)


def _applies(spec: OptionSpec, target_type: str) -> bool:
    allowed = _TARGET_GATED.get(spec.key)
    return allowed is None or target_type in allowed


def options_for(
    *,
    converter_key: str | None,
    source_type: str,
    target_type: str,
) -> tuple[OptionSpec, ...]:
    """某个家族在某个目标格式下真正可用的选项。

    ``source_type`` 目前不参与裁剪 —— 图片选项只取决于目标格式。
    保留这个参数是有意的：将来出现「只对某个源成立的选项」时，
    调用方不必改签名。
    """
    specs = _FAMILY_OPTIONS.get(converter_key or "", ())
    return tuple(spec for spec in specs if _applies(spec, target_type))


def applicable_keys(*, converter_key: str | None, source_type: str, target_type: str) -> frozenset[str]:
    return frozenset(
        spec.key
        for spec in options_for(
            converter_key=converter_key, source_type=source_type, target_type=target_type
        )
    )


def union_specs(
    entries: Iterable[Capability], *, target_type: str | None
) -> tuple[OptionSpec, ...]:
    """能产出这个目标的**全部** conversion 条目，它们选项的并集。

    提交时服务端还不知道这一批文件的源格式（真实格式要等收完文件按内容判定），
    所以白名单只能是并集，不能是某一条能力的选项 —— 否则一个 .jpg 与一个 .txt
    混在同一批里转 PDF 时，先到的那条能力会把另一条的参数判成非法。

    **同名的键不会互相覆盖**：``page_size`` 在「图片 → PDF」与「TXT → PDF」里
    各有一份 spec（取值域不同），两份都留在返回值里，
    :func:`validate_payload` 会接受**任意一份认可**的取值。合并成一份就得
    发明一套「谁更宽」的规则，而两套取值域本来就不是宽窄关系。

    ``operation`` 条目一律不参与：它们不走统一队列，参数由各自端点的
    Form 字段负责，混进来只会让 ``/tasks`` 接受一批根本用不上的键。
    """
    specs: list[OptionSpec] = []
    for entry in entries:
        if entry.operation_type != OPERATION_CONVERSION:
            continue
        if target_type is None or entry.target_type != target_type:
            continue
        for spec in entry.options:
            # OptionSpec 是 frozen dataclass，相等即去重：
            # TXT 与 Markdown 共用同一个字体 spec 时就只会留一份。
            if spec not in specs:
                specs.append(spec)
    return tuple(specs)


# ----------------------------------------------------------------------
# 纯校验
# ----------------------------------------------------------------------

class OptionError(ValueError):
    """选项不合法。

    **故意**不继承 ``utils.errors.ValidationError``：这一层要能在
    没有 Web 框架、没有服务层的环境里单独测试。路由捕获它并转成
    ``ValidationError``（HTTP 400），中文文案原样带过去。
    """


def _label(spec: OptionSpec) -> str:
    return spec.label or spec.key


def _as_bool(value: Any, spec: OptionSpec) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
    raise OptionError(f"{_label(spec)}只能是「是」或「否」")


def _as_number(value: Any, spec: OptionSpec) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise OptionError(f"{_label(spec)}必须是数字")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise OptionError(f"{_label(spec)}必须是数字")
        try:
            number = float(text)
        except ValueError as exc:
            raise OptionError(f"{_label(spec)}必须是数字") from exc
    else:
        number = float(value)
    if not math.isfinite(number):
        # JSON 里可以塞进 NaN / Infinity —— 它们会让后面每一次比较都返回 False，
        # 一路溜到实现层。在这一层就掐掉。
        raise OptionError(f"{_label(spec)}必须是数字")
    return number


def _as_int(value: Any, spec: OptionSpec) -> int:
    number = _as_number(value, spec)
    if not number.is_integer():
        raise OptionError(f"{_label(spec)}必须是整数")
    return int(number)


def _as_enum(value: Any, spec: OptionSpec) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise OptionError(f"{_label(spec)}参数无效，可选值：{_enum_hint(spec)}")
    text = str(value).strip()
    if spec.dynamic:
        # 动态枚举（字体）的真实值域只有运行期才知道，纯层不装作知道。
        # 真正的白名单校验在绑定层：那个值的去向是 ``parse_txt_options``，
        # 它拿 ``available_fonts()`` 现算的键做白名单。
        if not text:
            raise OptionError(f"{_label(spec)}不能为空")
        if len(text) > MAX_OPTION_KEY_CHARS:
            raise OptionError(f"{_label(spec)}参数无效，可选值：请从服务器返回的列表中选择")
        return text
    if text not in {item for item, _ in spec.values}:
        raise OptionError(f"{_label(spec)}参数无效，可选值：{_enum_hint(spec)}")
    return text


def _enum_hint(spec: OptionSpec) -> str:
    return "、".join(label for _, label in spec.values)


def _as_string(value: Any, spec: OptionSpec) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise OptionError(f"{_label(spec)}必须是文本")
    text = str(value)
    if len(text) > MAX_OPTION_KEY_CHARS:
        raise OptionError(f"{_label(spec)}过长")
    return text


_COERCERS = {
    TYPE_INTEGER: _as_int,
    TYPE_NUMBER: _as_number,
    TYPE_BOOLEAN: _as_bool,
    TYPE_ENUM: _as_enum,
    TYPE_STRING: _as_string,
}


def validate_payload(
    payload: Mapping[str, Any],
    specs: Sequence[OptionSpec],
) -> dict[str, Any]:
    """把一份 ``options`` 收敛成干净的值。

    只做四件事：键必须在词表里、类型必须对得上、值必须在范围内、
    依赖项必须自洽。**不碰文件、不碰实现、不碰网络** —— 纯函数。

    ``specs`` 里**允许出现同名的多个 spec**（见 :func:`union_specs`）。
    同名时只要**任意一份**认可这个取值就算通过：一份 spec 代表一条真实
    存在的能力，取值能被某条能力接受，就说明用户没有把参数发给一个
    不存在的选项。全部不认可才报错，错误用第一份的文案。

    返回的键与传入的键一一对应（不做重命名），值已经转成 Python 类型：
    ``integer`` → ``int``，``number`` → ``float``，``boolean`` → ``bool``，
    其余 → ``str``。调用方再把这个结果交给既有解析器做第二道校验。
    """
    if len(payload) > MAX_OPTION_KEYS:
        raise OptionError("参数过多")

    known: dict[str, list[OptionSpec]] = {}
    for spec in specs:
        known.setdefault(spec.key, []).append(spec)

    result: dict[str, Any] = {}

    for raw_key, value in payload.items():
        key = str(raw_key)
        if not key or len(key) > MAX_OPTION_KEY_CHARS:
            raise OptionError("参数名不合法")
        candidates = known.get(key)
        if not candidates:
            raise OptionError(f"参数 {key} 不适用于该转换")

        if value is None:
            # ``null`` 与「不传」等价：前端把没填的输入框原样序列化出来时
            # 就是 null，这不该是一次失败。必填项仍然要报错。
            if any(spec.required for spec in candidates):
                raise OptionError(f"{_label(candidates[0])}不能为空")
            continue

        result[key] = _coerce(value, candidates)

    _check_visibility(result, known)
    return result


def _coerce(value: Any, candidates: Sequence[OptionSpec]) -> Any:
    """按候选 spec 逐个试，返回第一个接受它的结果。

    边界检查与类型转换在同一个循环里：``50`` 对 ``quality``（10–100）成立，
    对 ``dpi.custom``（1–1200 之外还有 ``visible_when``）也成立 —— 谁也不比
    谁「更对」，先试哪个都不影响结论，只影响报错时引用哪一句话。
    """
    first_error: OptionError | None = None
    for spec in candidates:
        coercer = _COERCERS.get(spec.type)
        if coercer is None:  # pragma: no cover - 词表里的 type 都被 _COERCERS 覆盖
            continue
        try:
            if spec.type in (TYPE_INTEGER, TYPE_NUMBER):
                number = coercer(value, spec)
                if spec.min is not None and number < spec.min:
                    raise OptionError(f"{_label(spec)}不能小于 {_fmt_bound(spec.min)}")
                if spec.max is not None and number > spec.max:
                    raise OptionError(f"{_label(spec)}不能大于 {_fmt_bound(spec.max)}")
                return number
            return coercer(value, spec)
        except OptionError as exc:
            if first_error is None:
                first_error = exc

    # 走到这里说明所有候选都拒了它，于是候选一定非空（调用方已保证）
    assert first_error is not None
    raise first_error


def _fmt_bound(value: float) -> str:
    """``10.0`` → ``10``，``14.17`` → ``14.17`` —— 给用户看的数字不带多余的零。"""
    return str(int(value)) if float(value).is_integer() else str(value)


def _check_visibility(
    result: Mapping[str, Any], known: Mapping[str, Sequence[OptionSpec]]
) -> None:
    """依赖项自洽：``visible_when`` 不成立时那个键就不该出现。

    不这样做的后果很具体：用户在「自定义尺寸」下填了宽高，又把尺寸改回
    「保持原尺寸」，前端可能来不及清空就提交了 —— 那时候**静默忽略**会让
    用户以为尺寸生效了。明确报错，用户才知道要重新选一次。

    依赖项本身用第一份同名 spec 的标签：同名 spec 的 ``visible_when``
    由 ``test_no_key_has_two_visible_when_shapes`` 钉住必须一致，
    所以取哪一份都不影响结论。
    """
    for key, value in result.items():
        spec = known[key][0]
        for dependency, expected in spec.visible_when:
            if dependency not in known:
                # 依赖项本身被目标格式裁剪掉了（例如 ICO 没有 dpi，
                # 于是 dpi.custom 也不存在）—— 那么这个键也不该出现在这里。
                raise OptionError(f"参数 {key} 不适用于该转换")
            actual = result.get(dependency)
            if actual is None:
                continue
            if str(actual) != str(expected):
                raise OptionError(
                    f"选择了「{_label(known[dependency][0])}」的其它取值时不能使用「{_label(spec)}」"
                )
