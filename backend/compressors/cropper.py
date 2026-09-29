"""裁剪的几何计算（第十阶段 A §二十–§二十二）。

与 ``resizer.py`` 分工一致：这里只算「从原图上切哪一块」，不动像素 ——
真正的切割由 ``loader.crop_image`` 完成。

## 裁剪框的语义

用户给的是 ``x / y / width / height``（左上角 + 宽高，像素）。这组参数
**是权威**：服务端不会替用户重新猜一个框。

``aspect_ratio`` 是**辅助**，不是第二个框。它的作用只有一个：当用户选了
``1:1`` / ``4:3`` / ``3:2`` / ``16:9`` 时，让 ``height`` 跟着比例走 ——
``height = width / ratio``（四舍五入）。这样「画一个框 + 选 1:1」得到的
一定是正方形，而不用前端把比例算准了再提交（前端算的和服务端算的
一旦差一个像素，用户会看到一张不是正方形的「1:1 裁剪」）。

height 因此被改动过时，会记一条说明 —— 改了用户的输入却不说，
用户拿到一张高度与自己填的数字不同的图，只会以为功能坏了。

## 越界一律拒绝，不做钳制

``x + width > 原图宽`` 时不「自动缩到边缘」：那样用户会拿到一张位置
与他指定不同的图，而且**看不出**哪里不对（尺寸还是他填的数字）。
拒绝时把原图尺寸一起说出来 —— 用户才知道自己该改成多少。
"""

from __future__ import annotations

from dataclasses import dataclass

from utils.errors import ValidationError

#: 比例档位 -> (宽, 高)。``free`` 表示不限制，``custom`` 由前端自由填写。
CROP_RATIOS: tuple[tuple[str, tuple[int, int] | None], ...] = (
    ("free", None),
    ("1:1", (1, 1)),
    ("4:3", (4, 3)),
    ("3:2", (3, 2)),
    ("16:9", (16, 9)),
)

CROP_RATIO_VALUES: tuple[str, ...] = tuple(name for name, _ in CROP_RATIOS) + ("custom",)

CROP_RATIO_LABELS: dict[str, str] = {
    "free": "自由裁剪",
    "1:1": "1:1 正方形",
    "4:3": "4:3",
    "3:2": "3:2",
    "16:9": "16:9",
    "custom": "自定义比例",
}

CROP_RATIO_BY_NAME: dict[str, tuple[int, int] | None] = dict(CROP_RATIOS)


@dataclass(slots=True)
class CropRequest:
    """用户提交的裁剪参数。

    ``ratio`` 取值见 :data:`CROP_RATIO_VALUES`；``custom_ratio`` 只在
    ``ratio == "custom"`` 时有意义（形如 ``"7:5"``），由调用方解析成一对
    正整数后放进 ``custom_ratio`` —— 这里不解析字符串，避免与选项层
    各解析一遍。
    """

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    ratio: str = "free"
    custom_ratio: tuple[int, int] | None = None


@dataclass(slots=True)
class CropBox:
    """算好的裁剪框（像素，左上角 + 宽高）。"""

    x: int
    y: int
    width: int
    height: int
    #: 因为比例档而调整过高度时的如实说明；没调整就是 None。
    note: str | None = None


def parse_custom_ratio(raw: str | None) -> tuple[int, int] | None:
    """把 ``"7:5"`` / ``"7/5"`` / ``"7x5"`` 解析成 ``(7, 5)``。

    只接受两个正整数。``"0:1"``、``"1:0"``、``"-3:2"`` 一律拒绝 ——
    零与负数不是比例，是笔误。
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    for separator in (":", "/", "x", "×"):
        if separator in text:
            left, _, right = text.partition(separator)
            break
    else:
        raise ValidationError("自定义比例要写成「宽:高」，例如 7:5")
    try:
        width = int(left.strip())
        height = int(right.strip())
    except ValueError as exc:
        raise ValidationError("自定义比例要写成「宽:高」，例如 7:5") from exc
    if width <= 0 or height <= 0:
        raise ValidationError("自定义比例的宽和高都必须是大于 0 的整数")
    return width, height


def ratio_of(request: CropRequest) -> tuple[int, int] | None:
    """这次裁剪要求的宽高比；``free`` 返回 None。"""
    if request.ratio == "custom":
        if request.custom_ratio is None:
            raise ValidationError("选择了自定义比例，但没有填写比例数值")
        return request.custom_ratio
    return CROP_RATIO_BY_NAME.get(request.ratio, None)


def compute_crop_box(
    original_width: int,
    original_height: int,
    request: CropRequest,
) -> CropBox:
    """算出实际要切的框，并把越界、非正数一律拒绝。"""
    if original_width <= 0 or original_height <= 0:
        raise ValidationError("原图尺寸异常，无法裁剪")

    width = _positive(request.width, "裁剪宽度")
    height = _positive(request.height, "裁剪高度")
    x = _non_negative(request.x, "裁剪起点 X")
    y = _non_negative(request.y, "裁剪起点 Y")

    note: str | None = None
    ratio = ratio_of(request)
    if ratio is not None:
        ratio_width, ratio_height = ratio
        expected = max(1, round(width * ratio_height / ratio_width))
        if expected != height:
            note = (
                f"已按所选比例把裁剪高度从 {height} 像素调整为 {expected} 像素，"
                "以保证结果符合比例。"
            )
            height = expected

    if x + width > original_width or y + height > original_height:
        raise ValidationError(
            f"裁剪区域超出了图片范围：图片是 {original_width}×{original_height} 像素，"
            f"而裁剪框需要 ({x}, {y}) 起 {width}×{height} 像素"
        )

    return CropBox(x=x, y=y, width=width, height=height, note=note)


def _positive(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{label}必须是整数")
    if value <= 0:
        raise ValidationError(f"{label}必须是大于 0 的整数")
    return value


def _non_negative(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{label}必须是整数")
    if value < 0:
        raise ValidationError(f"{label}不能是负数")
    return value


__all__ = [
    "CROP_RATIO_BY_NAME",
    "CROP_RATIO_LABELS",
    "CROP_RATIO_VALUES",
    "CROP_RATIOS",
    "CropBox",
    "CropRequest",
    "compute_crop_box",
    "parse_custom_ratio",
    "ratio_of",
]
