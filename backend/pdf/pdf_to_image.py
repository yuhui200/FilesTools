"""PDF → 图片。

把选定的页面逐页渲染成 JPG / PNG / WEBP。

两个实测得到的结论决定了这里的写法：

- **PyMuPDF 的 Pixmap 不能直接输出 WEBP**（只支持 png/pnm/psd/...），
  所以 JPG 和 WEBP 都借助 Pillow 编码；PNG 直接由 MuPDF 输出，省一次编解码。
- **Pixmap 的样本可能有行填充**（stride 大于 width×通道数），
  用 ``Image.frombytes`` 构造时必须按 stride 重新排布，
  否则图片会整体斜切。这里统一走 :func:`to_pil` 处理。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pymupdf
from PIL import Image

from pdf.loader import render_pixmap
from utils.errors import PdfTooManyPagesError, ValidationError

__all__ = [
    "RenderOptions",
    "RenderedPage",
    "check_selection",
    "extension_for",
    "is_lossy",
    "media_type_for",
    "normalize_format",
    "render_page_image",
]

# 支持导出的图片格式
_JPEG = "jpeg"
_PNG = "png"
_WEBP = "webp"

_FORMAT_ALIASES = {"jpg": _JPEG, "jpeg": _JPEG, "png": _PNG, "webp": _WEBP}

# 各格式的扩展名
_EXTENSIONS = {_JPEG: ".jpg", _PNG: ".png", _WEBP: ".webp"}

# 有损格式才需要质量参数
LOSSY_FORMATS = (_JPEG, _WEBP)

# 质量允许区间。低于 40 的 JPG 基本没法看，高于 95 收益极小。
MIN_QUALITY = 1
MAX_QUALITY = 100


def normalize_format(raw: str | None) -> str:
    """把用户填的格式名统一成内部名。"""
    value = (raw or "").strip().lower()
    fmt = _FORMAT_ALIASES.get(value)
    if fmt is None:
        raise ValidationError("导出格式只支持 JPG、PNG、WEBP")
    return fmt


def extension_for(fmt: str) -> str:
    return _EXTENSIONS[fmt]


def media_type_for(fmt: str) -> str:
    return {_JPEG: "image/jpeg", _PNG: "image/png", _WEBP: "image/webp"}[fmt]


def is_lossy(fmt: str) -> bool:
    return fmt in LOSSY_FORMATS


@dataclass(slots=True)
class RenderOptions:
    """导出参数。"""

    fmt: str = _PNG
    dpi: int = 110
    quality: int | None = None   # 只对 JPG / WEBP 生效

    @property
    def effective_quality(self) -> int | None:
        if not is_lossy(self.fmt):
            return None
        return self.quality


@dataclass(slots=True)
class RenderedPage:
    """一页的渲染结果。"""

    index: int      # 0 开始的页码
    data: bytes
    width: int
    height: int
    clamped: bool   # 是否因像素上限被降采样


def check_selection(pages: list[int], max_pages: int) -> None:
    """导出前的数量检查。"""
    if not pages:
        raise ValidationError("请至少选择一页")
    if len(pages) > max_pages:
        raise PdfTooManyPagesError(
            f"一次最多导出 {max_pages} 页图片，当前选择了 {len(pages)} 页，"
            "请缩小页面范围或分批处理"
        )


def render_page_image(
    doc: pymupdf.Document,
    index: int,
    options: RenderOptions,
) -> RenderedPage:
    """渲染单独一页并编码成目标格式。

    一次只渲染一页是刻意的：几十页的高分辨率图片如果都留在内存里，
    几百 MB 的内存峰值足以把并发请求挤垮，所以由调用方渲染一页写一页。
    """
    pixmap, clamped = render_pixmap(doc, index, dpi=options.dpi)
    data = _encode(pixmap, options.fmt, options.effective_quality)
    return RenderedPage(
        index=index,
        data=data,
        width=pixmap.width,
        height=pixmap.height,
        clamped=clamped,
    )


def _encode(pixmap: pymupdf.Pixmap, fmt: str, quality: int | None) -> bytes:
    """把位图编码成目标格式的字节。"""
    if fmt == _PNG:
        # MuPDF 直接输出 PNG，无损且不用经过 Pillow
        return pixmap.tobytes("png")

    image = to_pil(pixmap)
    buffer = io.BytesIO()
    if fmt == _JPEG:
        image.save(buffer, "JPEG", quality=quality or 90, optimize=True, progressive=True)
    else:
        image.save(buffer, "WEBP", quality=quality or 90, method=4)
    return buffer.getvalue()


def to_pil(pixmap: pymupdf.Pixmap) -> Image.Image:
    """把 Pixmap 转成 Pillow 图片，处理行填充。

    渲染时用了 ``alpha=False``，因此样本是 3 通道（RGB）。
    """
    width, height, channels = pixmap.width, pixmap.height, pixmap.n
    stride = pixmap.stride

    if channels not in (3, 4):
        # 灰度等少见情况：交给 MuPDF 转成 PNG 再读，稳但慢一点
        return Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")

    expected = width * channels
    if stride == expected:
        return Image.frombytes("RGB" if channels == 3 else "RGBA", (width, height), pixmap.samples)

    # 有行填充：逐行把有效像素抠出来
    samples = pixmap.samples
    rows = [samples[y * stride : y * stride + expected] for y in range(height)]
    return Image.frombytes(
        "RGB" if channels == 3 else "RGBA",
        (width, height),
        b"".join(rows),
    )
