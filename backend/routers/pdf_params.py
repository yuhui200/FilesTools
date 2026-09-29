"""PDF / 文档转换接口的表单参数解析。

前端传来的一律是字符串，这里统一做白名单校验 —— 参数值不在枚举内
就直接报错，而不是悄悄退回默认值：用户选了「横向」却拿到纵向的 PDF，
比明确报错难查得多。
"""

from __future__ import annotations

import math

from config import settings
from office.txt_to_pdf import TxtOptions, available_fonts
from pdf.image_to_pdf import (
    FITS,
    MARGINS,
    ORIENTATIONS,
    PAGE_SIZES,
    PdfLayout,
)
from pdf.compressor import LEVEL_LABELS, TARGET_PRESETS_MB
from pdf.pdf_to_image import MAX_QUALITY, MIN_QUALITY, RenderOptions, normalize_format
from pdf.splitter import MODE_EVERY, MODE_SELECTED, SPLIT_MODES, SPLIT_MODE_LABELS
from utils.errors import ValidationError

__all__ = [
    "parse_page_size",
    "parse_orientation",
    "parse_fit",
    "parse_margin",
    "parse_layout",
    "parse_render_options",
    "parse_resolution",
    "parse_image_quality",
    "parse_page_range_text",
    "parse_required_page_range",
    "parse_split_mode",
    "parse_split_value",
    "parse_compress_level",
    "parse_target_size",
    "parse_txt_options",
]

# 自定义页面尺寸的范围，与 pdf/image_to_pdf 保持一致
# 值与 ``config.settings`` 同源：参数面板上的滑杆范围与这里接受的区间
# 必须是同一对数。
_MIN_CUSTOM_MM = settings.PDF_MIN_CUSTOM_MM
_MAX_CUSTOM_MM = settings.PDF_MAX_CUSTOM_MM

# 目标文件大小的范围（MB）。上限给得很宽，但必须有 ——
# 把「目标 100000 MB」当成有效参数去试五档压缩纯属浪费 CPU。
#
# 值取自 ``config.settings``：第九阶段把它加进参数面板后，界面上的滑杆
# 范围与这里接受的区间必须是同一对数，各存一份迟早会分叉。
_MIN_TARGET_MB = settings.PDF_MIN_TARGET_MB
_MAX_TARGET_MB = settings.PDF_MAX_TARGET_MB


def _pick(raw: str | None, allowed: tuple[str, ...], default: str, label: str) -> str:
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value not in allowed:
        raise ValidationError(f"{label}参数无效，可选值：{'、'.join(allowed)}")
    return value


def parse_page_size(raw: str | None) -> str:
    return _pick(raw, PAGE_SIZES, "auto", "页面大小")


def parse_orientation(raw: str | None) -> str:
    return _pick(raw, ORIENTATIONS, "auto", "页面方向")


def parse_fit(raw: str | None) -> str:
    return _pick(raw, FITS, "contain", "图片适应方式")


def parse_margin(raw: str | None) -> str:
    return _pick(raw, MARGINS, "none", "页边距")


def parse_custom_mm(raw: str | None, label: str) -> float | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValidationError(f"自定义页面{label}必须是数字") from exc
    if not math.isfinite(value):
        raise ValidationError(f"自定义页面{label}必须是数字")
    if not _MIN_CUSTOM_MM <= value <= _MAX_CUSTOM_MM:
        raise ValidationError(
            f"自定义页面{label}需要在 {_MIN_CUSTOM_MM:.0f}–{_MAX_CUSTOM_MM:.0f} 毫米之间"
        )
    return value


def parse_layout(
    *,
    page_size: str | None,
    orientation: str | None,
    fit: str | None,
    margin: str | None,
    custom_width_mm: str | None,
    custom_height_mm: str | None,
) -> PdfLayout:
    """把表单字段解析成排版参数。"""
    size = parse_page_size(page_size)

    width = parse_custom_mm(custom_width_mm, "宽度")
    height = parse_custom_mm(custom_height_mm, "高度")
    if size == "custom" and (width is None or height is None):
        raise ValidationError("选择自定义页面时，请填写宽度和高度")

    return PdfLayout(
        page_size=size,
        orientation=parse_orientation(orientation),
        fit=parse_fit(fit),
        margin=parse_margin(margin),
        custom_width_mm=width,
        custom_height_mm=height,
    )


# ----------------------------------------------------------------------
# PDF 转图片
# ----------------------------------------------------------------------

def parse_resolution(raw: str | None) -> int:
    """清晰度档位 -> DPI。"""
    presets = settings.PDF_RESOLUTION_PRESETS
    if raw is None or raw.strip() == "":
        return presets[settings.DEFAULT_PDF_RESOLUTION]

    value = raw.strip().lower()
    if value in presets:
        return presets[value]

    # 也允许直接传数字，方便命令行和脚本调用
    try:
        dpi = int(value)
    except ValueError as exc:
        raise ValidationError(
            f"清晰度参数无效，可选值：{'、'.join(presets)}，或直接填 DPI（36–300）"
        ) from exc

    if not settings.PDF_RENDER_MIN_DPI <= dpi <= settings.PDF_RENDER_MAX_DPI:
        raise ValidationError(
            f"清晰度需要在 {settings.PDF_RENDER_MIN_DPI}–{settings.PDF_RENDER_MAX_DPI} DPI 之间"
        )
    return dpi


def parse_image_quality(raw: str | None, fmt: str) -> int | None:
    """导出图片的质量。

    只在有损格式（JPG / WEBP）下有意义：PNG 是无损格式，
    传了质量也不会被使用，这里直接返回 None，避免前端以为它生效了。
    """
    from pdf.pdf_to_image import is_lossy

    if not is_lossy(fmt):
        return None
    if raw is None or raw.strip() == "":
        return None

    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValidationError("图片质量必须是 1–100 的整数") from exc

    if not MIN_QUALITY <= value <= MAX_QUALITY:
        raise ValidationError("图片质量必须在 1–100 之间")
    return value


def parse_render_options(
    *,
    target_format: str | None,
    quality: str | None,
    resolution: str | None,
) -> RenderOptions:
    """把表单字段解析成导出参数。"""
    fmt = normalize_format(target_format)
    return RenderOptions(
        fmt=fmt,
        dpi=parse_resolution(resolution),
        quality=parse_image_quality(quality, fmt),
    )


def parse_page_range_text(raw: str | None) -> str | None:
    """页面范围原样透传，真正的校验在解析时按文档页数进行。

    这里只做长度限制：一个几百字符的范围串没有意义，
    而且解析它本身要消耗 CPU。
    """
    if raw is None:
        return None
    text = raw.strip()
    if len(text) > 2000:
        raise ValidationError("页面范围过长，请分批处理")
    return text


def parse_required_page_range(raw: str | None, message: str) -> str:
    """必须填写的页面范围。

    「删除页面」「提取页面」都不接受空值：什么都不选就点按钮，
    得到的会是一份和原文件一样的 PDF，用户以为功能没生效。
    """
    text = parse_page_range_text(raw)
    if not text:
        raise ValidationError(message)
    return text


# ----------------------------------------------------------------------
# PDF 拆分
# ----------------------------------------------------------------------

def parse_split_mode(raw: str | None) -> str:
    """拆分方式白名单，报错用中文方式名而不是内部值。"""
    value = (raw or "").strip().lower()
    if value not in SPLIT_MODES:
        labels = "、".join(SPLIT_MODE_LABELS[mode] for mode in SPLIT_MODES)
        raise ValidationError(f"拆分方式无效，可选：{labels}")
    return value


def parse_split_value(raw: str | None, mode: str) -> str | None:
    """拆分依据：按范围是多段文本，自定义页面是单段页面范围。

    「每页一个 PDF」不需要填写，传了也忽略 —— 前端切换方式时
    不必清空输入框，用户切回去时填过的范围还在。
    """
    if mode == MODE_EVERY:
        return None
    if mode == MODE_SELECTED:
        return parse_required_page_range(raw, "请填写要提取的页码，例如：1,3,5,8")
    return parse_required_page_range(raw, "请至少填写一个页面范围，例如：1-5")


# ----------------------------------------------------------------------
# PDF 压缩
# ----------------------------------------------------------------------

def parse_compress_level(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return settings.DEFAULT_PDF_COMPRESS_LEVEL
    if value not in settings.PDF_COMPRESS_LEVELS:
        labels = "、".join(
            LEVEL_LABELS.get(name, name) for name in settings.PDF_COMPRESS_LEVELS
        )
        raise ValidationError(f"压缩等级无效，可选：{labels}")
    return value


def parse_target_size(target: str | None, custom_mb: str | None) -> int | None:
    """解析目标最大文件大小，返回字节数；不限制时返回 None。

    前端传的是档位（``none`` / ``5mb`` / ``10mb`` / ``20mb`` / ``custom``），
    选「自定义」时再读 ``custom_mb`` 这个数字。
    """
    value = (target or "").strip().lower() or "none"

    if value == "custom":
        return _parse_mb(custom_mb)

    if value not in TARGET_PRESETS_MB:
        allowed = "、".join(TARGET_PRESETS_MB)
        raise ValidationError(f"目标大小参数无效，可选：{allowed}、custom")

    preset = TARGET_PRESETS_MB[value]
    return None if preset is None else round(preset * 1024 * 1024)


def _parse_mb(raw: str | None) -> int:
    if raw is None or raw.strip() == "":
        raise ValidationError("选择自定义目标大小时，请填写目标大小（MB）")

    try:
        mb = float(raw)
    except ValueError as exc:
        raise ValidationError("目标大小必须是数字（单位 MB）") from exc

    if not math.isfinite(mb):
        raise ValidationError("目标大小必须是数字（单位 MB）")
    if not _MIN_TARGET_MB <= mb <= _MAX_TARGET_MB:
        raise ValidationError(
            f"目标大小需要在 {_MIN_TARGET_MB:g}–{_MAX_TARGET_MB:g} MB 之间"
        )
    return round(mb * 1024 * 1024)


# ----------------------------------------------------------------------
# TXT 转 PDF 的排版选项
# ----------------------------------------------------------------------

def parse_txt_options(
    *,
    font: str | None,
    font_size: str | None,
    page_size: str | None,
    orientation: str | None,
) -> TxtOptions:
    """把表单字段解析成 TXT 排版选项（字体 / 字号 / 页面大小 / 页面方向）。

    字体的可选值是**运行时探测出来的**（``available_fonts``），不是写死的：
    服务器上没装的字体不该出现在选项里，否则用户选了也只会得到别的字体。
    探测不到任何系统字体时这个列表只有内置字体一项，界面照样能用。
    """
    fonts = available_fonts()
    keys = tuple(item.key for item in fonts)

    size = settings.TXT_DEFAULT_FONT_SIZE
    if font_size is not None and font_size.strip():
        try:
            value = float(font_size.strip())
        except ValueError as exc:
            raise ValidationError("字体大小必须是数字") from exc
        if not value.is_integer():
            raise ValidationError("字体大小必须是整数")
        size = int(value)
        if not settings.TXT_MIN_FONT_SIZE <= size <= settings.TXT_MAX_FONT_SIZE:
            raise ValidationError(
                f"字体大小需要在 {settings.TXT_MIN_FONT_SIZE}–"
                f"{settings.TXT_MAX_FONT_SIZE} 之间"
            )

    return TxtOptions(
        font=_pick(font, keys, keys[0], "字体"),
        font_size=size,
        page_size=_pick(
            page_size, settings.TXT_PAGE_SIZES, settings.TXT_PAGE_SIZES[0], "页面大小"
        ),
        orientation=_pick(
            orientation,
            settings.TXT_ORIENTATIONS,
            settings.TXT_ORIENTATIONS[0],
            "页面方向",
        ),
    )
