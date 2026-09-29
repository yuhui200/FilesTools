"""表单参数解析。

前端提交的是字符串（可能为空），这里统一解析并给出中文错误提示，
避免每个接口各写一套、提示口径不一致。
"""

from __future__ import annotations

from config import settings
from converters.image_converter import label_for, parse_target_format
from utils.errors import ValidationError

# 目标大小的合理区间：太小没有意义，太大说明前端算错了。
#
# 第十阶段 A 把这两个数搬去了 ``config.settings`` —— 选项词表
# （``conversion/options.py``）要拿它们当 schema 的 min/max，而那个模块
# 只许 import ``config``。这里保留同名别名，是为了让既有调用点与测试
# （``from routers.params import MIN_TARGET_BYTES``）一个字都不用改；
# 值只有一份，就在 settings 里。
MIN_TARGET_BYTES = settings.MIN_TARGET_BYTES
MAX_TARGET_BYTES = settings.MAX_TARGET_BYTES

# 尺寸调整的合法区间
MIN_EDGE = 1
MAX_EDGE = settings.MAX_IMAGE_EDGE


def parse_target_bytes(raw: str | None) -> int | None:
    """把表单里的目标大小解析成字节数。

    前端可能传空字符串（表示「不限制」），因此这里不用 FastAPI 的自动类型转换，
    改为手动解析并给出中文错误提示。
    """
    if raw is None:
        return None
    text = raw.strip()
    if text == "" or text.lower() in {"none", "null", "0"}:
        return None
    try:
        value = int(float(text))
    except ValueError as exc:
        raise ValidationError("目标大小必须是数字") from exc

    if value < MIN_TARGET_BYTES:
        raise ValidationError(f"目标大小不能小于 {MIN_TARGET_BYTES // 1024} KB")
    if value > MAX_TARGET_BYTES:
        raise ValidationError("目标大小超出允许范围")
    return value


def parse_quality(raw: str | None) -> str:
    """解析压缩质量档位。"""
    value = (raw or settings.DEFAULT_QUALITY_PRESET).strip().lower()
    if value not in settings.QUALITY_PRESETS:
        allowed = "、".join(settings.QUALITY_PRESETS)
        raise ValidationError(f"压缩质量只能是：{allowed}")
    return value


def parse_quality_value(raw: str | None) -> int | None:
    """解析 1-100 的自定义质量。留空表示使用默认值。"""
    if raw is None:
        return None
    text = raw.strip()
    if text == "" or text.lower() in {"none", "null"}:
        return None
    try:
        value = int(float(text))
    except ValueError as exc:
        raise ValidationError("图片质量必须是 1-100 之间的整数") from exc
    if not 1 <= value <= 100:
        raise ValidationError("图片质量必须在 1-100 之间")
    return value


def parse_keep_aspect(raw: str | None, default: bool = True) -> bool:
    """解析「保持宽高比例」开关。"""
    if raw is None:
        return default
    text = raw.strip().lower()
    if text == "":
        return default
    return text in {"1", "true", "yes", "on"}


def parse_edge(raw: str | None, label: str) -> int | None:
    """解析宽度或高度。留空表示不指定该边。"""
    if raw is None:
        return None
    text = raw.strip()
    if text == "" or text.lower() in {"none", "null", "0"}:
        return None
    try:
        value = int(float(text))
    except ValueError as exc:
        raise ValidationError(f"{label}必须是整数") from exc
    if value < MIN_EDGE:
        raise ValidationError(f"{label}必须大于 0")
    if value > MAX_EDGE:
        raise ValidationError(f"{label}不能超过 {MAX_EDGE} 像素")
    return value


def parse_format(raw: str | None) -> str:
    """解析目标格式，返回内部名（jpeg / png / webp）。"""
    try:
        return parse_target_format(raw)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def format_label(fmt: str) -> str:
    return label_for(fmt)
