"""``options`` / ``capability_id`` 两个表单字段的解析与校验（第九阶段 §四十五）。

统一转换中心在第七阶段的九个扁平表单字段之上，又多了两个**可选**字段：

* ``options`` —— 一段 JSON（**扁平点号键**，如 ``{"resize.mode":"small"}``）；
* ``capability_id`` —— 前端从 ``/api/conversion/capabilities`` 拿到的那条能力的 ID。

新字段存在的意义是「加一种格式不用改前端」：目标格式换了，参数面板跟着
``options_schema`` 变，服务端不需要为每一种格式写一段表单解析。

## 五层校验链，每层归属明确

1. **语法层** —— 体积 / 合法 JSON / 必须是对象 / 键名合法；
2. **白名单层** —— 键必须落在该目标下所有能力的 schema **并集**里
   （提交时还不知道这一批的源格式，见 ``conversion.options.union_specs``）；
3. **覆盖层** —— JSON 覆盖同名的扁平字段（``resize.*`` 覆盖 ``width``/``height``）；
4. **类型与边界层** —— ``conversion.options.validate_payload``，纯函数、零 I/O；
5. **绑定层** —— **逐字复用既有解析器**（``routers/params.py`` /
   ``routers/pdf_params.py``）。边界与中文错误文案因此不可能与另外十四个
   工具漂移，§四十五 要的资源上限也天然绕不过：JSON 里的每一个值，
   最终都必须穿过某个已经在线上的解析器。

## 老前端不传 ``options`` 时，行为与第七阶段逐字节相同

``options`` 为空时整条链在第一步就返回：不发一次 JSON 解析，
不查一次白名单，不构造任何新对象 —— 直接把扁平字段解析出来的
:class:`~services.conversion_service.ConversionOptions` 原样交回去。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from compressors.cropper import CropRequest, parse_custom_ratio
from compressors.resizer import ResizeRequest
from conversion import registry
from conversion.capability import OPERATION_CONVERSION, OptionSpec
from conversion.options import (
    DPI_CUSTOM,
    DPI_ORIGINAL,
    MAX_OPTIONS_BYTES,
    MAX_OPTION_KEYS,
    MAX_OPTION_KEY_CHARS,
    RESIZE_CUSTOM,
    RESIZE_FILL,
    RESIZE_FIT,
    RESIZE_MAX_EDGE_BY_MODE,
    RESIZE_MAX_WIDTH_BY_MODE,
    RESIZE_ORIGINAL,
    RESIZE_PERCENT,
    RESIZE_PERCENT_BY_MODE,
    RESIZE_STRETCH,
    ROTATION_CUSTOM,
    TXT_ORIENTATION_VALUES,
    TXT_PAGE_SIZE_VALUES,
    OptionError,
    union_specs,
    validate_payload,
)
from routers.params import parse_quality_value, parse_target_bytes
from routers.pdf_params import parse_layout, parse_txt_options
from services.conversion_service import ConversionOptions
from utils.errors import ValidationError

__all__ = ["ParsedOptions", "parse_capability_id", "parse_options_json", "resolve_options"]

#: 合法键名：小写字母开头的小写词，可带点号分层（``resize.keep_aspect``）。
#: 键名最终会进日志与错误文案，卡在这个形状上可以杜绝控制字符与超长串。
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")

_BAD_JSON_MESSAGE = "参数格式不正确，请重新选择后提交"
_BAD_KEY_MESSAGE = "参数名不合法"

#: 扁平字段与 JSON 键的分组（覆盖层用）。
_RESIZE_KEYS = (
    "resize.mode",
    "resize.width",
    "resize.height",
    "resize.keep_aspect",
    "resize.percent",
    "resize.fit",
)
#: 裁剪键。任何一个出现就整组重算 —— 理由与 ``_resize_from`` 相同。
_CROP_KEYS = (
    "crop.enabled",
    "crop.ratio",
    "crop.custom_ratio",
    "crop.x",
    "crop.y",
    "crop.width",
    "crop.height",
)
_LAYOUT_KEYS = (
    "page_size",
    "page_size.width_mm",
    "page_size.height_mm",
    "orientation",
    "fit",
    "margin",
)
_TXT_KEYS = ("font", "font_size")


@dataclass(slots=True)
class ParsedOptions:
    """校验链的产物。

    * ``capability_id`` —— 客户端声明的那条能力（空串表示没声明），
      只用于收窄白名单与记录，**不参与执行**：真正命中的能力由
      ``tasks.conversion_tasks._classify`` 按文件真实内容判定；
    * ``payload`` —— 收敛后的干净 JSON，写进 ``TaskItem.options`` 供界面回显；
    * ``options`` —— 绑定完成的 :class:`ConversionOptions`，执行路径用的就是它。
    """

    capability_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    options: ConversionOptions = field(default_factory=ConversionOptions)


# ----------------------------------------------------------------------
# 第 1 层：语法
# ----------------------------------------------------------------------

def parse_options_json(raw: str | None) -> dict[str, Any]:
    """把 ``options`` 表单字段解析成 dict。空值返回 ``{}``。

    体积按 **UTF-8 字节**算而不是字符数：中文键名一个字符三字节，
    按字符算等于给攻击者三倍的额度。上限之下才做解析 ——
    先解析再判长度，等于让一段 8MB 的 JSON 先吃掉一次 CPU 与内存。
    """
    if raw is None:
        return {}
    text = raw.strip()
    if not text:
        return {}

    if len(text.encode("utf-8")) > MAX_OPTIONS_BYTES:
        raise ValidationError("参数过多，请重新选择后再提交")

    try:
        payload = json.loads(text)
    except RecursionError as exc:
        # 8 KB 的 ``[[[[...`` 足以把 json 的递归解析器顶穿。
        raise ValidationError(_BAD_JSON_MESSAGE) from exc
    except ValueError as exc:
        raise ValidationError(_BAD_JSON_MESSAGE) from exc

    if not isinstance(payload, dict):
        raise ValidationError(_BAD_JSON_MESSAGE)
    if len(payload) > MAX_OPTION_KEYS:
        raise ValidationError("参数过多")

    for key in payload:
        # JSON 对象的键一定是 str，长度与形状仍需自己把关：
        # 后面每一条错误文案都会把这个键名回显给用户。
        if not isinstance(key, str) or len(key) > MAX_OPTION_KEY_CHARS:
            raise ValidationError(_BAD_KEY_MESSAGE)
        if not _KEY_PATTERN.match(key):
            raise ValidationError(_BAD_KEY_MESSAGE)

    return payload


# ----------------------------------------------------------------------
# 第 2 层：白名单
# ----------------------------------------------------------------------

def parse_capability_id(raw: str | None, *, target_type: str) -> str:
    """校验客户端声明的能力 ID，返回它（没声明时返回空串）。

    声明了 ID 就把白名单收窄成**这一条能力自己的**选项，而不是整个目标格式的
    并集。这是一道**双向**的保险：客户端要是拿旧能力 ID 配新界面（版本错配），
    多出来的键会当场得到一句明确的 400，而不是被静默忽略；
    同时它也只能**减少**可用键，永远不可能凭空多拿到一个参数。

    只接受 ``conversion``：``operation`` 条目（PDF 合并之类的工具）
    不走统一队列，参数由各自端点的表单负责。
    """
    if raw is None or not raw.strip():
        return ""
    value = raw.strip()

    entry = registry.CAPABILITY_BY_ID.get(value)
    if entry is None or entry.operation_type != OPERATION_CONVERSION:
        raise ValidationError("这个转换能力不存在，请重新选择。")
    if entry.target_type != target_type:
        raise ValidationError("选择的转换与目标格式不一致，请重新选择。")
    return value


def _whitelist(*, target_type: str, capability_id: str) -> tuple[OptionSpec, ...]:
    if capability_id:
        entry = registry.CAPABILITY_BY_ID[capability_id]
        return entry.options
    return union_specs(registry.CONVERSIONS, target_type=target_type)


# ----------------------------------------------------------------------
# 第 3 + 5 层：覆盖与绑定
# ----------------------------------------------------------------------

def _has(clean: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in clean for key in keys)


def _text(value: Any) -> str | None:
    """把绑定层的入参转成既有解析器要求的字符串（None 原样透传）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _resize_from(
    clean: Mapping[str, Any], existing: ResizeRequest | None
) -> ResizeRequest | None:
    """``resize.*`` → :class:`ResizeRequest`。

    返回 ``None`` 表示「不改尺寸」。JSON 一旦出现 ``resize.*`` 的任何一个键，
    就**整组**取代扁平字段解析出来的那个 ``ResizeRequest`` ——
    只覆盖一半会得到「界面选了小图、结果按自定义宽高裁」这种半新半旧的状态。
    """
    mode = clean.get("resize.mode")
    width = clean.get("resize.width")
    height = clean.get("resize.height")
    keep_aspect = clean.get("resize.keep_aspect")
    percent = clean.get("resize.percent")
    fit = clean.get("resize.fit")

    if mode is None:
        # 没给档位但给了宽高/百分比 —— 意图只能是自定义（visible_when 管不到缺省键）
        if width is not None or height is not None:
            mode = RESIZE_CUSTOM
        elif percent is not None:
            mode = RESIZE_PERCENT
        else:
            mode = None
    if mode is None:
        return existing

    if mode == RESIZE_ORIGINAL:
        return None

    if mode == RESIZE_CUSTOM:
        if width is None and height is None:
            # §九 允许只填一条边，但两条都不填就不能叫「自定义」了。
            # 扁平字段里有宽高就沿用它，否则明确报错 ——
            # 静默按原尺寸处理会让用户以为设置生效了。
            if existing is not None:
                return existing
            raise ValidationError("选择自定义尺寸时，请至少填写宽度或高度")
        return ResizeRequest(
            width=width,
            height=height,
            keep_aspect=_keep_aspect_value(keep_aspect, fit),
            fit=_fit_value(fit),
        )

    if mode == RESIZE_PERCENT:
        if percent is None:
            raise ValidationError("选择自定义百分比时，请填写缩放百分比")
        if width is not None or height is not None:
            raise ValidationError("百分比缩放不能同时填写自定义宽高")
        return ResizeRequest(percent=int(percent))

    percent_preset = RESIZE_PERCENT_BY_MODE.get(mode)
    if percent_preset is not None:
        if width is not None or height is not None:
            raise ValidationError("选择百分比预设时不能填写自定义宽高")
        return ResizeRequest(percent=percent_preset)

    if width is not None or height is not None:
        # ``visible_when`` 已经拦过一次，这里是第二道 ——
        # 「预设档 + 自定义宽高」不该由谁悄悄胜出来决定结果。
        raise ValidationError("选择预设尺寸时不能填写自定义宽高")

    max_width = RESIZE_MAX_WIDTH_BY_MODE.get(mode)
    if max_width is not None:
        # §十九 的「最大宽度」档：限宽不限长边。复用 ``ResizeRequest`` 的
        # width 字段就够了 —— 「只填宽度 + 保持比例」正是它已有的语义，
        # 而且它与 max_edge 不同：这里**允许放大**（用户明确点了 2560，
        # 一张 800 宽的图确实会变成 2560）。preset 档「只缩不放」是
        # RESIZE_PRESETS 那三档的规矩，不是这一组的。
        return ResizeRequest(width=max_width, keep_aspect=True)

    edge = RESIZE_MAX_EDGE_BY_MODE.get(mode)
    if edge is None:  # pragma: no cover - 词表里每个档位都在两张表之一里
        raise ValidationError("尺寸参数无效，请重新选择")
    # 预设档只缩不放，宽高比是它唯一的含义，因此不接受 keep_aspect=False
    return ResizeRequest(max_edge=edge)


def _rotation_from(clean: Mapping[str, Any], existing: float) -> float:
    """``rotation`` / ``rotation.angle`` → 顺时针角度（度）。

    ``rotation`` 的四个离散值直接就是角度；``custom`` 时角度由
    ``rotation.angle`` 给出 —— 与 ``dpi`` / ``dpi.custom`` 同型。

    只给 ``rotation.angle`` 而没给 ``rotation`` 也认：填了角度却忘了把
    下拉框拨到「自定义」，静默不转会让用户以为功能坏了。
    """
    value = clean.get("rotation")
    if value is None:
        if "rotation.angle" in clean:
            value = ROTATION_CUSTOM
        else:
            return existing

    text = str(value)
    if text == ROTATION_CUSTOM:
        angle = clean.get("rotation.angle")
        if angle is None:
            raise ValidationError("选择自定义角度时，请填写旋转角度")
        return float(angle)
    # 词表已经保证取值在 ROTATIONS 里，这里只做字符串 → 数字
    return float(text)


def _fit_value(raw: Any) -> str:
    """``resize.fit`` → ``fit`` / ``fill`` / ``stretch``。

    词表负责让用户看到「拉伸」这个名字，实现里只保留一条不保比例的分支，
    所以 ``stretch`` 自己不带 ``keep_aspect`` 的语义 —— 由
    :func:`_keep_aspect_value` 把两者对齐。
    """
    text = "" if raw is None else str(raw)
    if text == RESIZE_STRETCH:
        return RESIZE_STRETCH
    if text == RESIZE_FILL:
        return RESIZE_FILL
    return RESIZE_FIT


def _keep_aspect_value(raw: Any, fit: Any) -> bool:
    """``resize.keep_aspect`` 与 ``resize.fit`` 谁说了算。

    ``stretch`` 就是「不保比例」的另一个名字，所以它**强制**关掉
    ``keep_aspect`` —— 否则用户选了「拉伸」却因为 ``keep_aspect`` 默认是
    true 而拿到一张等比缩放的图，界面上那个下拉框看起来完全没生效。
    反过来 ``fit`` / ``fill`` 都保比例，用户显式关掉 ``keep_aspect``
    时以用户为准（他可能就想拉伸，只是没改下拉框）。
    """
    if str(fit or "") == RESIZE_STRETCH:
        return False
    return True if raw is None else bool(raw)


def _crop_from(clean: Mapping[str, Any]) -> CropRequest | None:
    """``crop.*`` → :class:`CropRequest`，``None`` 表示不裁剪。

    开启与关闭由 ``crop.enabled`` 决定；没给这个键但给了宽高时按「开启」算 ——
    与 ``resize`` 同一套宽容策略：用户填了裁剪尺寸却忘了拨开关，
    静默不裁会让他以为功能坏了。
    """
    enabled = clean.get("crop.enabled")
    width = clean.get("crop.width")
    height = clean.get("crop.height")

    if enabled is None:
        enabled = width is not None or height is not None
    if not enabled:
        return None

    if width is None or height is None:
        raise ValidationError("裁剪需要同时填写宽度和高度")

    ratio = str(clean.get("crop.ratio") or "free")
    custom_ratio = None
    if ratio == "custom":
        custom_ratio = parse_custom_ratio(_text(clean.get("crop.custom_ratio")))
        if custom_ratio is None:
            raise ValidationError("选择了自定义比例，但没有填写比例数值")

    return CropRequest(
        x=int(clean.get("crop.x") or 0),
        y=int(clean.get("crop.y") or 0),
        width=int(width),
        height=int(height),
        ratio=ratio,
        custom_ratio=custom_ratio,
    )


def _dpi_from(clean: Mapping[str, Any]) -> int | str | None:
    """``dpi`` / ``dpi.custom`` → ``None`` / ``"original"`` / 正整数。

    ``None`` 表示**不覆盖**（保持既有行为：不写入密度信息）。
    """
    value = clean.get("dpi")
    if value is None:
        if "dpi.custom" in clean:
            value = DPI_CUSTOM
        else:
            return None

    if value == DPI_ORIGINAL:
        return DPI_ORIGINAL
    if value == DPI_CUSTOM:
        custom = clean.get("dpi.custom")
        if custom is None:
            raise ValidationError("选择自定义分辨率时，请填写 DPI")
        return int(custom)
    return int(value)


def _layout_from(clean: Mapping[str, Any], existing: Any) -> Any:
    """排版键 → :class:`~pdf.image_to_pdf.PdfLayout`，逐字复用既有解析器。

    既有值作为缺省塞回去，只被 JSON 覆盖掉真正给了的那些键 ——
    于是「只改页边距」不会把页面大小一起打回默认。
    """
    return parse_layout(
        page_size=_text(clean.get("page_size", existing.page_size)),
        orientation=_text(clean.get("orientation", existing.orientation)),
        fit=_text(clean.get("fit", existing.fit)),
        margin=_text(clean.get("margin", existing.margin)),
        custom_width_mm=_text(clean.get("page_size.width_mm", existing.custom_width_mm)),
        custom_height_mm=_text(clean.get("page_size.height_mm", existing.custom_height_mm)),
    )


def _txt_keys_in(clean: Mapping[str, Any]) -> frozenset[str]:
    """这份 JSON 里哪些键是 TXT / Markdown 家族真的能消费的。

    ``page_size`` 与 ``orientation`` 是**两个家族共用**的键名，取值域却不同：
    ``auto`` / ``custom`` 只对「图片 → PDF」成立，喂给 TXT 那套解析器
    只会得到一句用户看不懂的参数错误。于是按 TXT 自己的词表放行，
    不在词表里的值连碰都不碰 —— 一个 ``page_size=auto`` 不该让 TXT
    那一侧平白重建一次默认值。
    """
    keys = {key for key in _TXT_KEYS if key in clean}
    if clean.get("page_size") in TXT_PAGE_SIZE_VALUES:
        keys.add("page_size")
    if clean.get("orientation") in TXT_ORIENTATION_VALUES:
        keys.add("orientation")
    return frozenset(keys)


def _txt_from(clean: Mapping[str, Any], existing: Any) -> Any:
    """TXT / Markdown 排版键 → :class:`~office.txt_to_pdf.TxtOptions`。

    未出现在 :func:`_txt_keys_in` 里的值**交给默认值**。这确实是一次静默
    降级 —— 提交时服务端还不知道这批文件的真实格式，``page_size=custom``
    到底是给图片的还是给文本的，只有文件收上来才知道。前端声明了
    ``capability_id`` 时不会有这个问题：白名单会收窄成一条能力，
    ``custom`` 在 TXT 能力上当场被拒（400），而不是被丢掉。
    """
    page_size = clean.get("page_size")
    if page_size not in TXT_PAGE_SIZE_VALUES:
        page_size = None
    orientation = clean.get("orientation")
    if orientation not in TXT_ORIENTATION_VALUES:
        orientation = None

    seed_size = getattr(existing, "page_size", None)
    seed_orientation = getattr(existing, "orientation", None)
    seed_font = getattr(existing, "font", None)
    seed_font_size = getattr(existing, "font_size", None)

    return parse_txt_options(
        font=_text(clean.get("font", seed_font)),
        font_size=_text(clean.get("font_size", seed_font_size)),
        page_size=_text(page_size if page_size is not None else seed_size),
        orientation=_text(
            orientation if orientation is not None else seed_orientation
        ),
    )


def _bind(
    clean: Mapping[str, Any], base: ConversionOptions
) -> ConversionOptions:
    """第 3 + 5 层：把干净的 JSON 绑到既有 DTO 上。"""
    options = ConversionOptions(
        quality_preset=base.quality_preset,
        quality_value=base.quality_value,
        target_bytes=base.target_bytes,
        resize=base.resize,
        layout=base.layout,
        txt_options=base.txt_options,
        rotation=base.rotation,
        dpi=base.dpi,
        metadata=base.metadata,
        crop=base.crop,
        rotation_expand=base.rotation_expand,
        flip=base.flip,
    )

    # 质量：JSON 的显式数值优先于扁平字段的档位（``_resolve_quality``
    # 里 quality_value 本来就压过 quality_preset，这里只是把值填进去）
    if "quality" in clean:
        options.quality_value = parse_quality_value(_text(clean["quality"]))

    # 目标大小（第十阶段 A §二十九）。
    #
    # 这个键第十阶段才进 schema，但它**不是**新参数 —— ``target_bytes``
    # 从第七阶段起就是那九个扁平字段之一，走的是下面 ``base`` 那条路。
    # 这里补的是另一条：用户在统一参数面板里从预设档里挑了一个，
    # 前端会把它塞进 ``options`` JSON 而不是扁平字段。两条路都通到
    # **同一个** ``parse_target_bytes``，所以边界与中文提示不可能漂移。
    if "target_bytes" in clean:
        options.target_bytes = parse_target_bytes(_text(clean["target_bytes"]))

    if _has(clean, _RESIZE_KEYS):
        options.resize = _resize_from(clean, base.resize)

    if _has(clean, _CROP_KEYS):
        options.crop = _crop_from(clean)

    if "dpi" in clean or "dpi.custom" in clean:
        options.dpi = _dpi_from(clean)

    if "rotation" in clean or "rotation.angle" in clean:
        options.rotation = _rotation_from(clean, base.rotation)

    if "rotation.expand" in clean:
        options.rotation_expand = bool(clean["rotation.expand"])

    if "flip" in clean:
        options.flip = str(clean["flip"])

    if "metadata" in clean:
        options.metadata = str(clean["metadata"])

    # 排版：两个家族各自按自己的词表消费同一批键名
    if _has(clean, _LAYOUT_KEYS):
        options.layout = _layout_from(clean, base.layout)
    if _txt_keys_in(clean):
        options.txt_options = _txt_from(clean, base.txt_options)

    return options


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def resolve_options(
    *,
    raw_options: str | None,
    raw_capability_id: str | None,
    target_type: str,
    base: ConversionOptions,
) -> ParsedOptions:
    """跑完五层校验链，返回 (声明的能力, 干净的 JSON, 绑定好的 DTO)。

    ``base`` 是路由层用既有扁平字段（``quality_preset`` / ``quality_value`` /
    ``target_bytes`` / ``width`` / ``height`` / ``keep_aspect``）解析出来的，
    它**永远是**这次提交的基础；JSON 只覆盖它明确提到的那些键。
    """
    capability_id = parse_capability_id(raw_capability_id, target_type=target_type)
    payload = parse_options_json(raw_options)

    if not payload:
        # 第七阶段的调用方（含前端 1.0 与全部既有测试）走的就是这条路：
        # 不解析、不查表、不新建对象，返回的就是传进来的那个 base。
        return ParsedOptions(capability_id=capability_id, payload={}, options=base)

    specs = _whitelist(target_type=target_type, capability_id=capability_id)
    try:
        clean = validate_payload(payload, specs)
    except OptionError as exc:
        # 纯层不依赖 Web 框架，中文文案原样带出去（§三十三：不泄漏内部信息）
        raise ValidationError(str(exc)) from exc

    return ParsedOptions(
        capability_id=capability_id,
        payload=clean,
        options=_bind(clean, base),
    )
