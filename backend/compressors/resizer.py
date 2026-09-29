"""尺寸调整的几何计算。

只负责「根据用户输入算出目标宽高」，不做实际的像素重采样
（重采样由 loader.resize_exact 完成），也不涉及质量控制。
"""

from __future__ import annotations

from dataclasses import dataclass

from utils.errors import ValidationError


#: width / height 都给时怎么处理与目标框的关系（第十阶段 A §十六 / §十七）。
#:
#: * ``fit`` —— 等比缩放到**放进**框内，两边都不超出，可能有一边小于框（默认）
#: * ``fill`` —— 等比缩放到**填满**框，短边贴边、长边超出后再居中裁掉
#: * ``stretch`` —— 直接拉伸到精确宽高，**不保持宽高比**（会变形）
#:
#: ``stretch`` 与 ``keep_aspect=False`` 的区别值得写下来：两者结果相同，
#: 但入口不同 —— ``keep_aspect`` 是「我要不要保比例」这个开关，
#: ``stretch`` 是这个开关关掉之后的**名字**。绑定层把 stretch 翻成
#: ``keep_aspect=False``，于是 ``compute_target_size`` 里只有一条
#: 不保比例的分支，不是两条。
RESIZE_FITS: tuple[str, ...] = ("fit", "fill", "stretch")

RESIZE_FIT_LABELS: dict[str, str] = {
    "fit": "完整放入（可能留白）",
    "fill": "填满并居中裁剪",
    "stretch": "拉伸到指定尺寸（会变形）",
}

DEFAULT_RESIZE_FIT = "fit"

#: 百分比缩放的取值范围（第十阶段 A §十八）。
#: 上界 400% 与 ``MAX_IMAGE_EDGE`` 无关 —— 放大后的边长由
#: ``compute_target_size`` 的 ``_clamp`` 与流水线的尺寸上限兜底，
#: 这里只是挡掉「放 1000 倍」这种把内存打爆的输入。
MIN_PERCENT = 1
MAX_PERCENT = 400


@dataclass(slots=True)
class ResizeRequest:
    """用户提交的尺寸调整参数。

    width / height 为 None 表示该边不指定；三个都为 None 表示不改尺寸。
    keep_aspect 为 True 时按原图比例推导另一条边。

    ``max_edge``（第九阶段 §九 的「小 / 中 / 大」预设档）是**另一种**表达：
    它说的是「最长边不超过这个像素数」，**只缩不放** —— 比它还小的图原样保留，
    不会被拉大。它与 width/height 互斥，两者同时给出是调用方的错误，
    由 ``compute_target_size`` 直接拒绝而不是悄悄挑一个。

    ``percent``（第十阶段 A §十八）是第三种：按原图的百分比缩放。
    它同样与 width/height/max_edge 互斥。

    ``fit`` 只在 width 与 height **都给**时才有意义 —— 只给一边时
    「放进框内」和「填满框」是同一件事，没有可选的余地。
    """

    width: int | None = None
    height: int | None = None
    keep_aspect: bool = True
    max_edge: int | None = None
    percent: float | None = None
    fit: str = DEFAULT_RESIZE_FIT


def compute_target_size(
    original_width: int,
    original_height: int,
    request: ResizeRequest,
) -> tuple[int, int]:
    """算出目标宽高。

    规则（保持比例时）：

    - 只填宽度 → 高度按原图比例自动推导（4032×3024 填 1920 → 1440）
    - 只填高度 → 宽度按原图比例自动推导
    - 两个都填 → 等比缩放到正好放进这个框内（不裁剪、不变形）
    - 只给 ``max_edge`` → 最长边缩到它，本来就比它小则原样返回
    - 只给 ``percent`` → 两边都乘以这个百分比

    不保持比例时：只填一边则另一边维持原尺寸；两边都填则取精确值。

    ``fit == "fill"`` 时两边都给会**填满**框：先按较大的缩放系数放大，
    再在这里把多出来的部分居中裁掉 —— 裁剪必须发生在返回之前，
    因为调用方拿到宽高就只会 ``resize``，不会再裁一次。
    """
    if original_width <= 0 or original_height <= 0:
        raise ValidationError("原图尺寸异常，无法调整")

    if request.max_edge is not None:
        if request.width is not None or request.height is not None:
            raise ValidationError("预设尺寸档与自定义宽高不能同时使用")
        if request.percent is not None:
            raise ValidationError("预设尺寸档与百分比缩放不能同时使用")
        edge = _positive_or_none(request.max_edge, "长边")
        assert edge is not None
        longest = max(original_width, original_height)
        if longest <= edge:
            return original_width, original_height
        scale = edge / longest
        return _clamp(round(original_width * scale), round(original_height * scale))

    if request.percent is not None:
        if request.width is not None or request.height is not None:
            raise ValidationError("百分比缩放与自定义宽高不能同时使用")
        percent = _positive_or_none(request.percent, "缩放百分比")
        assert percent is not None
        scale = percent / 100
        return _clamp(round(original_width * scale), round(original_height * scale))

    width = _positive_or_none(request.width, "宽度")
    height = _positive_or_none(request.height, "高度")

    if width is None and height is None:
        return original_width, original_height

    if not request.keep_aspect:
        return (width or original_width, height or original_height)

    ratio = original_width / original_height

    if width is not None and height is not None:
        if request.fit == "fill":
            # 填满：取**较大**的缩放系数 —— 也就是 CSS 的 ``cover``。
            # 结果一定不小于用户给的框，多余的部分由调用方按框居中裁掉
            # （``centre_crop_box``，pipeline 负责那一步）。
            #
            # 这里**不能**直接返回 ``(width, height)``：那等于拉伸，
            # 是 ``stretch`` 的语义。fill 与 stretch 的区别正在于此 ——
            # 一个保比例后裁剪，一个不保比例。
            scale = max(width / original_width, height / original_height)
            return _clamp(round(original_width * scale), round(original_height * scale))
        # 等比缩放到框内：取较小的缩放系数，保证两边都不超出用户给的框
        scale = min(width / original_width, height / original_height)
        return _clamp(round(original_width * scale), round(original_height * scale))

    if width is not None:
        return _clamp(width, round(width / ratio))

    assert height is not None
    return _clamp(round(height * ratio), height)


def centre_crop_box(
    resized: tuple[int, int], box: tuple[int, int]
) -> tuple[int, int, int, int]:
    """在 ``resized`` 尺寸的图上居中切出 ``box`` 尺寸的那一块。

    ``fill`` 模式的第二步。单独成一个函数是因为它必须和 resize 分开做：
    ``compute_target_size`` 只回答「缩到多大」，裁剪是另一件事 ——
    把它塞回那个函数会让一个「算尺寸」的纯函数开始返回裁剪框。

    ``min`` 是防御性的：缩放系数取大值之后结果必然不小于框，
    但四舍五入有可能让某一边正好差 1 像素。差 1 像素就切到图外，
    与其让 Pillow 用黑边补齐，不如切到边界为止。
    """
    resized_width, resized_height = resized
    box_width, box_height = box
    return (
        max(0, (resized_width - box_width) // 2),
        max(0, (resized_height - box_height) // 2),
        min(box_width, resized_width),
        min(box_height, resized_height),
    )


def _positive_or_none(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise ValidationError(f"{label}必须是大于 0 的整数")
    return value


def _clamp(width: int, height: int) -> tuple[int, int]:
    """保证结果至少 1 像素，并且不因四舍五入溢出到 0。"""
    return max(1, int(width)), max(1, int(height))
