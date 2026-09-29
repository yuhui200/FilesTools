"""PDF 压缩。

思路很直接：**把页面里的图片重新编码**。PDF 的体积绝大部分来自图片，
文档结构本身压不出多少东西 —— 实测一份已经用 deflate 存过的 PDF，
只做无损清理（deflate + garbage + clean）收益是 0.0%，
而把图片限制到 150 DPI、质量 72 能降到 44.8%。所以压缩等级
本质上是「图片分辨率上限多少、重编码质量多少」。

关于 ``rewrite_images`` 的一个实测结论（踩过坑，记在这里）：

- ``dpi_target`` 不是「缩放到这个 DPI」，而是**半衰的下限** ——
  MuPDF 只会把图片反复对半缩小，只要再缩一半仍不低于 ``dpi_target`` 就继续缩。
  所以传 ``dpi_target=150`` 时，一张 290 DPI 的图**一像素都不会动**
  （145 < 150，再缩就过分了），只有传 ``dpi_target=75`` 才会缩到 145 DPI。
- ``dpi_threshold`` 只决定「谁够格被降采样」，**不影响重编码**：
  分辨率低于阈值的图片照样会按 ``quality`` 重新编码一遍。

因此这里的用法是 ``dpi_threshold=档位``、``dpi_target=档位 / 2``，
等价于一句可以写进提示文案的保证：**结果里没有图片超过档位的 DPI**。

关于「目标最大文件大小」：绝不为了凑数字造假。
做法是从所选等级开始逐档加强（降 DPI、降质量），哪一档先达标就停；
全都达不到就给出最小的一档，并在提示里**如实说明没有达标**。
如果连压缩前的原始文件都比压缩结果小，就直接保留原文件 ——
用户点「压缩」之后拿到一个更大的文件是最糟糕的结果。

``rewrite_images`` 是不可逆的原地修改，所以每一档都必须从原始文件
重新打开一次，不能在同一个文档上反复压。这些调用是 CPU 密集型的，
调用方必须通过 ``services.intake.run_in_pool`` 放进线程池执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from config import settings
from pdf.loader import open_pdf
from utils.errors import ValidationError
from utils.files import human_size

__all__ = [
    "LEVEL_LABELS",
    "TARGET_PRESETS_MB",
    "CompressResult",
    "compress_pdf",
    "level_label",
]

# 档位的中文名，提示文案和前端共用同一套说法
LEVEL_LABELS = {"light": "轻度", "balanced": "平衡", "strong": "高压缩"}

# 「目标最大文件大小」的档位 -> 多少 MB，None 表示不限制
TARGET_PRESETS_MB: dict[str, float | None] = {
    "none": None,
    # 前三档给文档转换用：一份 Word / Excel 转出来的 PDF 通常只有几百 KB，
    # 5 MB 起的档位对它等于「不限制」，等于没得选。
    "500kb": 0.5,
    "1mb": 1.0,
    "2mb": 2.0,
    "5mb": 5.0,
    "10mb": 10.0,
    "20mb": 20.0,
}

# 逐档加强时用的下限。再低就不是「压缩」而是「糊掉」了：
# 48 DPI 大概相当于屏幕上看个轮廓，只适合纯文字稿的最后手段。
_FLOOR_DPI = 48
_FLOOR_QUALITY = 35

# 从所选等级往下加强的档位表（不含等级本身）
_STRONGER_STEPS: tuple[tuple[int, int], ...] = (
    (110, 60),
    (80, 45),
    (60, 38),
    (_FLOOR_DPI, _FLOOR_QUALITY),
)

_MB = 1024 * 1024


@dataclass(slots=True)
class CompressResult:
    """一次压缩的结果。"""

    data: bytes
    original_size: int
    page_count: int
    level: str
    dpi: int | None = None          # 实际生效的档位，保留原文件时为 None
    quality: int | None = None
    target_bytes: int | None = None
    kept_original: bool = False     # 压缩反而更大，保留了原文件
    notes: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def saved_bytes(self) -> int:
        return self.original_size - self.size

    @property
    def saved_percent(self) -> float:
        if self.original_size <= 0:
            return 0.0
        return round(self.saved_bytes / self.original_size * 100, 1)

    @property
    def reached_target(self) -> bool:
        """是否达到目标大小（没有设目标时永远算达到）。"""
        if self.target_bytes is None:
            return True
        return self.size <= self.target_bytes


def level_label(level: str) -> str:
    return LEVEL_LABELS.get(level, level)


def compress_pdf(
    path: Path,
    *,
    level: str = "",
    target_bytes: int | None = None,
    max_attempts: int = 5,
) -> CompressResult:
    """压缩 PDF，返回压缩后的字节与过程说明。

    ``max_attempts`` 只影响「为达到目标大小而逐档加强」的次数：
    不设目标时永远只压一次（用所选等级），设了目标才会多试几档。
    """
    chosen = level or settings.DEFAULT_PDF_COMPRESS_LEVEL
    if chosen not in settings.PDF_COMPRESS_LEVELS:
        raise ValidationError(
            "压缩等级不正确，可选："
            + "、".join(LEVEL_LABELS.get(name, name) for name in settings.PDF_COMPRESS_LEVELS)
        )

    original_size = path.stat().st_size
    best: tuple[bytes, int, int, int] | None = None  # (数据, 页数, dpi, 质量)

    for dpi, quality in _ladder(chosen, target_bytes, max_attempts):
        data, page_count = _rewrite(path, dpi, quality)
        if best is None or len(data) < len(best[0]):
            best = (data, page_count, dpi, quality)
        if target_bytes is not None and len(data) <= target_bytes:
            break

    assert best is not None  # _ladder 至少给出一档
    data, page_count, dpi, quality = best

    kept_original = len(data) >= original_size
    if kept_original:
        data = path.read_bytes()

    result = CompressResult(
        data=data,
        original_size=original_size,
        page_count=page_count,
        level=chosen,
        dpi=None if kept_original else dpi,
        quality=None if kept_original else quality,
        target_bytes=target_bytes,
        kept_original=kept_original,
    )
    result.notes = _notes(result)
    return result


def _ladder(
    level: str,
    target_bytes: int | None,
    max_attempts: int,
) -> list[tuple[int, int]]:
    """本次要尝试的档位。

    不设目标时只有一档 —— 用户选了「轻度」就该得到轻度的结果，
    不该被偷偷压得更狠。
    """
    base = settings.PDF_COMPRESS_LEVELS[level]
    if target_bytes is None:
        return [base]

    steps = [base]
    for step in _STRONGER_STEPS:
        if step[0] >= base[0] or step[1] >= base[1]:
            continue  # 不比所选等级更强，跳过
        steps.append(step)
    return steps[: max(1, max_attempts)]


def _rewrite(path: Path, dpi: int, quality: int) -> tuple[bytes, int]:
    """按给定档位重新编码页面图片，返回 (字节, 页数)。

    每次都从原始文件重新打开：``rewrite_images`` 原地生效，
    在压过的文档上再压一遍只会越压越糊，也不是「多试一档」的意思。
    """
    with open_pdf(path) as doc:
        page_count = doc.page_count
        try:
            # 阈值 = 档位，目标 = 档位的一半：dpi_target 是「对半缩的下限」，
            # 取一半才能保证结果不超过档位（见模块开头的实测说明）
            doc.rewrite_images(
                dpi_threshold=dpi,
                dpi_target=max(1, dpi // 2),
                quality=quality,
            )
        except Exception as exc:  # pragma: no cover - 库内部的意外失败
            raise ValidationError("压缩失败，文件中的图片可能已损坏") from exc

        _subset_fonts(doc)
        data = doc.tobytes(deflate=True, garbage=4, clean=True)

    return data, page_count


def _subset_fonts(doc: pymupdf.Document) -> None:
    """字体子集化：只保留真正用到的字形。

    对内嵌了整套中文字体的 PDF 收益很大；对没内嵌字体的 PDF 是无操作。
    失败不影响压缩本身（有些字体的子集化会报错），所以吞掉异常。
    """
    try:
        doc.subset_fonts()
    except Exception:  # pragma: no cover - 取决于具体字体文件
        pass


def _target_label(target_bytes: int) -> str:
    """目标大小的显示文案。

    整 MB 时保持「5 MB」这种紧凑写法（PDF 压缩页沿用至今的说法）；
    不足 1 MB 时改按 KB 显示 —— 文档转换有 500 KB 这一档，
    写成「不超过 0.488281 MB」没人看得懂。
    """
    if target_bytes % _MB == 0:
        return f"{target_bytes // _MB:g} MB"
    return human_size(target_bytes)


def _notes(result: CompressResult) -> list[str]:
    notes: list[str] = []

    if result.kept_original:
        notes.append(
            "这份 PDF 已经很紧凑，压缩后反而更大，因此保留了原文件（未做任何改动）。"
        )
    else:
        notes.append(
            f"压缩等级「{level_label(result.level)}」：页面图片按质量 "
            f"{result.quality} 重新编码，分辨率不超过 {result.dpi} DPI。"
        )

    if result.target_bytes is not None:
        label = _target_label(result.target_bytes)
        if result.reached_target:
            notes.append(f"已达到目标大小（不超过 {label}）。")
        else:
            notes.append(
                f"已用到最高压缩档位，仍未达到 {label}"
                f"（当前 {human_size(result.size)}）。"
                "这份 PDF 的内容已经高度压缩，无法再明显缩小。"
            )

    return notes
