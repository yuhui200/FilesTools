"""图片元数据读取（第十阶段 A §三十三–§三十六）。

这是「看一眼这个文件里有什么」的**只读**读取器：不解码像素、不做任何变换、
不产出文件、不写任何日志。它回答的是「这张图带了哪些拍摄信息」，
不是「怎么把它变小」。

## 为什么在 ``compressors`` 里

第十阶段 A §二 明令不许再建第二套图片系统。``compressors`` 已经是图片这件事
的唯一真相所在地（``loader`` 解码、``resizer`` 缩放、``cropper`` 裁剪、
``encoder`` 编码、``pipeline`` 串起来）。元数据读取是这条流水线之外的
**旁路观察**，但它观察的是同一批文件、同一批格式常量，所以它在这里，
而不是新开的 ``services/image/``。

## 三个刻意的取舍

**一、只报「有没有」，不报「是什么」。**
GPS 只出一行「有 / 无」，**绝不给出坐标**；相机只出厂商与型号，
**绝不读机身序列号**（``BodySerialNumber`` 0xA431 与 ``LensSerialNumber``
0xA435 这两个 tag 在下面根本没有常量 —— 不是忘了，是不要）。
理由见 §三十五：位置与序列号是这张图里最不该被复制到第二个地方的两样东西，
而「这张图带不带位置信息」才是用户真正需要知道的判断依据。
顺带一提，这也让本模块的返回值天然可以安全地进日志 —— 虽然它压根不写日志。

**二、读不出来就是 ``None``，不编数字。**
每一项的 ``value`` 为 ``None`` 表示「这一项读不出来」，由界面显示成
「无法读取」。``0`` 与 ``None`` 在展示上是两件事：前者是「量出来是零」，
后者是「没量到」。

**三、密度小于 2 一律当作读不出来。**
JFIF 的密度字段为 0 或 1 时表示「这个文件只声明了像素宽高比，没有真实
密度」，Pillow 会原样交出 ``(1, 1)``。报一句「1 × 1 DPI」比报「无法读取」
更糟 —— 它看起来像一个测量结果，其实是一个约定值。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from utils.errors import CorruptedFileError

# ----------------------------------------------------------------------
# EXIF tag
# ----------------------------------------------------------------------

_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_SOFTWARE = 0x0131
_TAG_DATETIME = 0x0132
#: 指向 Exif 子 IFD 的指针。拍摄时间与镜头型号都在子 IFD 里，
#: 不在 0 号 IFD —— 只读 ``getexif()`` 会漏掉它们。
_TAG_EXIF_IFD = 0x8769
#: 指向 GPS 子 IFD 的指针。
_TAG_GPS_IFD = 0x8825

_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_LENS_MAKE = 0xA433
_TAG_LENS_MODEL = 0xA434

#: TIFF 的 0 号 IFD 里**必然**存在的一批结构字段：宽、高、每样本位数、
#: 压缩方式、光度解释、条带偏移、每像素样本数、每带行数、条带字节数、
#: 平面配置。它们描述的是像素怎么排布，是解码器要用的，**不是拍摄信息**。
#:
#: 这十个数字是量出来的，不是查来的：拿一张 JPEG 用
#: ``metadata="remove"`` 转成 TIFF，再打开它的 0 号 IFD，剩下的正好是
#: 这一组；``metadata="keep"`` 会多出 271（Make）与 272（Model）。
#:
#: 只有 TIFF 需要这张表，理由见 :func:`_has_exif`。
#:
#: **第十阶段 C §四 起有两个使用方**：这里判「这份文件到底有没有拍摄信息」，
#: 以及 ``compressors.encoder.read_exif`` 把 TIFF 源的 0 号 IFD 重打包成
#: 可携带的 EXIF 字节时剔掉它们。两处必须用同一张表 —— 各抄一份的话，
#: 将来认出新的结构标签就只会改一处，另一处会把偏移量写进结果的 EXIF。
TIFF_STRUCTURAL_TAGS = frozenset(
    {256, 257, 258, 259, 262, 273, 277, 278, 279, 284}
)

#: 单条元数据的字符上限。EXIF 字符串字段可以是任意长度（有些扫描仪
#: 会把整段路径塞进 Software），而没有上限的展示字段就是一个
#: 「把服务端内存和界面布局一起撑爆」的入口。
MAX_METADATA_CHARS = 120

#: 小于这个值的密度按「读不出来」处理，理由见模块开头的第三条。
_MIN_MEANINGFUL_DPI = 2

#: EXIF 的日期写成 ``2023:05:01 10:00:00``（日期部分用冒号）。
#: 只把日期那三个冒号换成短横线，后面原样保留 —— 这是展示习惯，
#: 不是数据订正，改多了就成了替用户改写他自己的信息。
_EXIF_DATE_PREFIX = re.compile(r"^(\d{4}):(\d{2}):(\d{2})")

#: Pillow 的色彩模式 -> 中文说明。没列到的原样输出（``CMYK`` 之类
#: 本来就没有约定俗成的中文译名，硬翻反而看不懂）。
_COLOR_MODE_LABELS: dict[str, str] = {
    "1": "黑白（1 位）",
    "L": "灰度",
    "LA": "灰度 + 透明",
    "P": "索引色",
    "RGB": "RGB",
    "RGBA": "RGB + 透明",
    "CMYK": "CMYK",
    "YCbCr": "YCbCr",
}

# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetadataField:
    """一行元数据。``value is None`` 表示这一行读不出来。"""

    key: str
    label: str
    value: str | None = None
    unit: str | None = None

    def to_json(self) -> dict[str, Any]:
        """出网形状。``unit`` 只在有值时出现，``value`` 恒在（可为 null）。"""
        out: dict[str, Any] = {"key": self.key, "label": self.label, "value": self.value}
        if self.unit is not None:
            out["unit"] = self.unit
        return out


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    """一次读取的**全部**结果 —— 十一个**具名**字段，不是一张行表格。

    具名字段是这一层的真相，行列表（:meth:`to_fields`）是它的**视图**。
    反过来存（只留一个行列表、按字符串键去取）会逼调用方把 ``"1600"``
    再解析回整数，而「格式是 JPEG、宽 1600」这件事本来就有确定的类型。

    顺序（§三十四）只写在 :meth:`to_fields` 一处 —— 它是给界面铺行用的，
    顺序写在两个地方，迟早有一处被改漏。
    """

    format: str | None = None
    width: int = 0
    height: int = 0
    color_mode: str | None = None
    dpi: str | None = None
    exif_available: bool = False
    camera: str | None = None
    lens: str | None = None
    captured_at: str | None = None
    #: **只表示「有没有」**，坐标永远不进这个对象。理由见模块开头。
    gps: bool = False
    software: str | None = None

    def to_fields(self) -> tuple[MetadataField, ...]:
        """按 §三十四 的顺序铺成十一行。**顺序的唯一出处。**"""
        return (
            MetadataField("format", "格式", self.format),
            MetadataField("width", "宽度", str(self.width), unit="px"),
            MetadataField("height", "高度", str(self.height), unit="px"),
            MetadataField("color_mode", "色彩模式", self.color_mode),
            MetadataField("dpi", "分辨率", self.dpi, unit="DPI"),
            MetadataField("exif_available", "EXIF 信息", _yes_no(self.exif_available)),
            MetadataField("camera", "相机", self.camera),
            MetadataField("lens", "镜头", self.lens),
            MetadataField("captured_at", "拍摄时间", self.captured_at),
            MetadataField("gps", "GPS 位置信息", _yes_no(self.gps)),
            MetadataField("software", "创建软件", self.software),
        )

    def get(self, key: str) -> MetadataField | None:
        """按 key 取一行；没有这一行返回 ``None``。测试与调用方用。"""
        return next((item for item in self.to_fields() if item.key == key), None)

    def to_json(self) -> dict[str, Any]:
        return {"fields": [item.to_json() for item in self.to_fields()]}


# ----------------------------------------------------------------------
# 取值清洗
# ----------------------------------------------------------------------


def _clean(value: Any) -> str | None:
    """把任意一个 EXIF 取值变成可以安全展示的短字符串；不值得展示就返回 None。

    做四件事，每一件都对应一个真实的坏输入：

    * **非字符串先筛一道** —— 只接受 ``str`` / ``bytes`` / 数字。传进来一个
      元组（有些 tag 本来就是元组，例如 ``YCbCrSubSampling``）时返回 None，
      而不是把它的 ``repr`` 当内容展示出去；
    * **控制字符丢掉，空白折叠成一个空格** —— EXIF 字符串常带 NUL 填充，
      直接塞进 JSON 会让响应体里出现 ``\\u0000``；
    * **截断到** :data:`MAX_METADATA_CHARS`；
    * 清完只剩空白就返回 ``None``（空字符串与「读不出来」在界面上该长得一样）。
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None

    chars: list[str] = []
    for char in value:
        if char in "\t\n\r\v\f":
            chars.append(" ")
        elif char.isprintable():
            chars.append(char)
    text = " ".join("".join(chars).split())
    if not text:
        return None
    if len(text) > MAX_METADATA_CHARS:
        return text[:MAX_METADATA_CHARS] + "…"
    return text


def _ifd(exif: Image.Exif, tag: int) -> dict[int, Any]:
    """取一个子 IFD；取不到就当空字典。

    ``get_ifd`` 在文件被截断、偏移量指向文件外等情况会抛异常。
    元数据是锦上添花，不该因为一次读取失败把整个请求掀翻 ——
    读不出来的项在下面各自变成 ``None``。
    """
    try:
        return dict(exif.get_ifd(tag) or {})
    except Exception:
        return {}


def _camera(make: Any, model: Any) -> str | None:
    """厂商 + 型号。型号里已经含厂商时不重复写（``Canon`` + ``Canon EOS R6``）。"""
    make_text = _clean(make)
    model_text = _clean(model)
    if make_text and model_text:
        if model_text.casefold().startswith(make_text.casefold()):
            return model_text
        return f"{make_text} {model_text}"
    return model_text or make_text


def _captured_at(original: Any, fallback: Any) -> str | None:
    """拍摄时间：优先 Exif 子 IFD 的 ``DateTimeOriginal``，退回 0 号 IFD 的 ``DateTime``。

    两者的区别是有意义的：前者是快门按下的时刻，后者是文件**被改写**的时刻
    （很多编辑器会刷新它）。所以退回时要写在注释里，而不是当成同一件事。
    """
    text = _clean(original) or _clean(fallback)
    if text is None:
        return None
    return _EXIF_DATE_PREFIX.sub(r"\1-\2-\3", text, count=1)


def _density(info: dict[str, Any]) -> str | None:
    """分辨率，形如 ``300 × 300``；读不出来或小到没有意义时返回 None。"""
    raw = info.get("dpi")
    if not isinstance(raw, (tuple, list)) or len(raw) != 2:
        return None
    try:
        horizontal = float(raw[0])
        vertical = float(raw[1])
    except (TypeError, ValueError):
        return None
    if horizontal < _MIN_MEANINGFUL_DPI or vertical < _MIN_MEANINGFUL_DPI:
        return None
    return f"{round(horizontal)} × {round(vertical)}"


def _color_mode(mode: str) -> str | None:
    text = _clean(mode)
    if text is None:
        return None
    return _COLOR_MODE_LABELS.get(text, text)


def _yes_no(flag: bool) -> str:
    return "有" if flag else "无"


# ----------------------------------------------------------------------
# 读取
# ----------------------------------------------------------------------


def read_metadata(path: Path) -> ImageMetadata:
    """读一张位图文件的元数据，按 §三十四 的顺序返回十一行。

    **不解码像素**：``Image.open`` 是惰性的，``getexif()`` 只读文件头那一段。
    调用方（``services.intake``）已经在校验阶段解过一次码，这里再解一遍
    只是为了问几个字符串，纯属浪费。

    校验（扩展名 / 魔数 / 真正解不打得开）不在这里做 —— 那是
    ``utils.validation`` 的活，同一个规则判两遍早晚有一处被改漏。
    走到这里时文件已经过了那一关。
    """
    try:
        with Image.open(path) as img:
            raw_format = img.format or ""
            width, height = img.size
            mode = img.mode
            info = dict(img.info)
            try:
                exif = img.getexif()
            except Exception:
                exif = None
    except Exception as exc:
        raise CorruptedFileError("无法读取该图片，请检查文件是否损坏。") from exc

    return _build(
        raw_format=raw_format,
        width=width,
        height=height,
        mode=mode,
        info=info,
        exif=exif,
    )


def _build(
    *,
    raw_format: str,
    width: int,
    height: int,
    mode: str,
    info: dict[str, Any],
    exif: Image.Exif | None,
) -> ImageMetadata:
    """把已经读出来的原料拼成 :class:`ImageMetadata`。

    与 :func:`read_metadata` 分开，是为了让「读文件」与「取值」两件事
    各自能被单独读懂；这个函数不碰磁盘。
    """
    sub = _ifd(exif, _TAG_EXIF_IFD) if exif is not None else {}
    gps = _ifd(exif, _TAG_GPS_IFD) if exif is not None else {}

    return ImageMetadata(
        format=_clean(raw_format.upper()),
        width=width,
        height=height,
        color_mode=_color_mode(mode),
        dpi=_density(info),
        # 「有没有 EXIF」问的是**0 号 IFD 里有没有拍摄信息**，不是「文件里
        # 有没有那个字节段」：一段空的 EXIF 与没有 EXIF，对用户是同一件事。
        exif_available=_has_exif(raw_format, exif),
        camera=_camera(_ifd0(exif, _TAG_MAKE), _ifd0(exif, _TAG_MODEL)),
        lens=_clean(sub.get(_TAG_LENS_MODEL)) or _clean(sub.get(_TAG_LENS_MAKE)),
        captured_at=_captured_at(
            sub.get(_TAG_DATETIME_ORIGINAL), _ifd0(exif, _TAG_DATETIME)
        ),
        # ``bool(gps)`` —— **只问这个 IFD 空不空**，不碰里面任何一个坐标 tag。
        gps=bool(gps),
        software=_clean(_ifd0(exif, _TAG_SOFTWARE)),
    )


def _has_exif(raw_format: str, exif: Image.Exif | None) -> bool:
    """这份文件里到底有没有**拍摄信息**。

    多数格式上就是「0 号 IFD 空不空」，但 TIFF 不是：它的 0 号 IFD 里
    **必然**躺着一批描述像素排布的结构字段（见
    :data:`TIFF_STRUCTURAL_TAGS`），Pillow 的 ``getexif()`` 直接把整张
    ``tag_v2`` 交出来，于是 ``bool(exif)`` 恒为真。

    实测：一张 JPEG 用 ``metadata="remove"`` 转成 TIFF 之后，0 号 IFD 里
    只剩那十个结构 tag（271 Make / 272 Model 都真的没了），可旧写法会
    报「EXIF 信息：有」—— 用户刚选了「清除元数据」，界面转头告诉他
    「有 EXIF」。这正是「不报错但说的话不对」，比抛错更难发现。

    只对 TIFF 特判：其余格式的 0 号 IFD 就是真的 EXIF，空就是空。
    """
    if exif is None:
        return False
    if raw_format.upper() != "TIFF":
        return bool(exif)
    return any(tag not in TIFF_STRUCTURAL_TAGS for tag in exif)


def _ifd0(exif: Image.Exif | None, tag: int) -> Any:
    """从 0 号 IFD 取一个 tag。Pillow 的 ``Exif`` 本身就是 0 号 IFD 的映射。"""
    if exif is None:
        return None
    try:
        return exif.get(tag)
    except Exception:
        return None


__all__ = [
    "MAX_METADATA_CHARS",
    "ImageMetadata",
    "MetadataField",
    "read_metadata",
]
