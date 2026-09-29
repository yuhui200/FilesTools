"""图片格式转换。

格式转换本身在 Pillow 里就是「换个 format 参数存一遍」，真正需要处理的是
**色彩模式**：不同格式支持的通道数不同，直接存会报错或产生错误结果。
本模块负责把这些差异抹平，真正的编码交给 compressors.encoder。
"""

from __future__ import annotations

from PIL import Image

from compressors.encoder import flatten_alpha, normalize_format

# 各输出格式支持的色彩模式
#
# 这张表是**实测**的结果，不是各格式规范的理论上限：
#
# * ``bmp`` 不含 ``RGBA``：32 位 BMP 存得下 alpha，但读回来时 Pillow 与
#   多数查看器都只当 RGB 用 —— 留着那条通道等于让用户看到一张和预期
#   不同的图，所以带透明的一律在 ``prepare_for_format`` 里合成到白底。
# * ``gif`` 收 ``RGBA``：Pillow 的 GIF 写入器会把 RGBA 量化成带 1 位
#   透明度的调色板，且**不透明像素的颜色保持不变**（实测过；手写
#   量化反而会把半透明像素整个抹掉）。
# * ``tiff`` 最宽：CMYK / I;16 / F 都是 TIFF 的原生模式，不需要降级。
_SUPPORTED_MODES: dict[str, tuple[str, ...]] = {
    "jpeg": ("RGB", "L"),
    "png": ("1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"),
    "webp": ("RGB", "RGBA", "L"),
    "bmp": ("1", "L", "P", "RGB"),
    "gif": ("P", "L", "RGB", "RGBA"),
    "tiff": ("1", "L", "LA", "P", "RGB", "RGBA", "CMYK", "I", "I;16", "F"),
    "ico": ("RGB", "RGBA"),
    # HEIC 支持透明：**实测** RGBA 原样往返（L / LA / P 会被它自己提升成
    # RGBA / RGB）。所以它与 webp / gif / ico 同一类，而不是像 JPEG / BMP
    # 那样要把透明通道压到白底上。放进 ``_SUPPORTED_MODES`` 是必须的 ——
    # 不在这里的格式会被下面那行 ``raise ValueError`` 直接打回。
    "heif": ("RGB", "RGBA", "L"),
}

# 用户可读的格式名，用于提示文案
FORMAT_LABELS: dict[str, str] = {
    "jpeg": "JPG",
    "png": "PNG",
    "webp": "WEBP",
    "bmp": "BMP",
    "gif": "GIF",
    "tiff": "TIFF",
    "ico": "ICO",
    "heif": "HEIC",
}

#: 第七阶段的三个目标格式，**冻结**。
#:
#: ``/api/convert``（旧格式转换页面）沿用它，所以它的行为一字不变 ——
#: ``tests/test_convert.py::test_convert_rejects_invalid_format`` 钉住了
#: 「旧接口拒绝 ``gif``」这件事，那不是历史包袱，而是**向后兼容**：
#: 旧前端的目标下拉里只有这三个，多出来的格式对它没有意义，
#: 悄悄放开只会让旧的自动化脚本拿到它与预期不同的结果。
#:
#: 统一转换中心**不**走这个白名单：它的目标由注册表逐格登记，
#: 校验走 ``conversion.options`` + ``routers/conversion_params``。
LEGACY_TARGET_FORMATS: tuple[str, ...] = ("jpeg", "png", "webp")


def label_for(fmt: str) -> str:
    """取格式的展示名，例如 jpeg -> JPG。"""
    return FORMAT_LABELS.get(fmt, fmt.upper())


def parse_target_format(raw: str | None) -> str:
    """解析**旧接口**提交的目标格式，非法值直接报错（由路由层转成 400）。

    只认 :data:`LEGACY_TARGET_FORMATS`。别拿它校验统一转换中心的目标 ——
    那里该用注册表，见 ``LEGACY_TARGET_FORMATS`` 的说明。
    """
    fmt = normalize_format(raw)
    if fmt is None or fmt not in LEGACY_TARGET_FORMATS:
        allowed = "、".join(label_for(item) for item in LEGACY_TARGET_FORMATS)
        raise ValueError(f"不支持的目标格式，请选择 {allowed}")
    return fmt


def is_same_format(source_format: str, target_format: str) -> bool:
    """判断源格式与目标格式是否相同（jpeg 与 jpg 视为相同）。"""
    return normalize_format(source_format) == normalize_format(target_format)


def prepare_for_format(img: Image.Image, target_format: str) -> Image.Image:
    """把图片调整成目标格式支持的色彩模式。

    - 转 JPG / BMP：透明通道合成到白底，CMYK / 16 位等模式统一转 RGB
    - 转 WEBP / GIF / ICO / HEIC：保留透明（WEBP / ICO / HEIC 原生支持；
      GIF 由 Pillow 量化成 1 位透明度），CMYK 等不支持的模式转成 RGBA
    - 转 PNG / TIFF：支持的模式最多，只有 CMYK 这类需要转换
    """
    supported = _SUPPORTED_MODES.get(target_format)
    if supported is None:
        raise ValueError(f"不支持的目标格式：{target_format}")

    if img.mode in supported:
        return img

    if target_format in ("jpeg", "bmp"):
        # 这两种格式的 alpha 都留不住：合成到白底比丢掉通道更接近直觉
        return flatten_alpha(img)

    if target_format in ("webp", "gif", "ico", "heif"):
        # 带透明信息的模式保留 alpha，其余转成 RGB
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            return img.convert("RGBA")
        return img.convert("RGB")

    # PNG / TIFF：CMYK、F 等模式转成 RGB 后仍是无损的
    return img.convert("RGB")


def same_format_note(source_format: str, target_format: str) -> str | None:
    """源格式与目标格式相同时给出说明，避免用户误以为做了转换。"""
    if not is_same_format(source_format, target_format):
        return None
    return (
        f"当前图片已经是 {label_for(target_format)} 格式，无需转换，已原样返回原文件。"
    )
