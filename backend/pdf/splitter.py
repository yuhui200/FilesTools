"""PDF 拆分。

三种方式（第三阶段 §7），区别只在「怎么分组」，
分成组之后的抽取、命名、打包完全共用一条路径：

- ``every``    每页一个 PDF：30 页 → 30 个单页文件，多于一个时自动打包；
- ``ranges``   按范围拆分：``1-5`` / ``6-10`` / ``11-20`` → 每个范围一份 ``part-NN.pdf``；
- ``selected`` 自定义页面：``1,3,5,8`` → 合成一份 ``selected-pages.pdf``。

两个刻意的取舍：

- **`every` 与 `ranges` 的页序按页码升序**，与 :mod:`pdf.pages` 的全局约定一致；
  用户写 ``3,1`` 得到的是第 1、3 页，而不是跟着输入顺序走。
  只有 ``selected``（自定义页面）例外：它是「用户排好序的一份 PDF」，
  页序完全跟着输入走（第四阶段 §11 支持拖拽排序，最终输出必须按照新顺序）。
- **不悄悄丢掉没被范围覆盖的页，但也不报错**：少拆了页是用户可能有意为之，
  所以照做，再用一条提示说明哪些页没进结果 —— 静默缺页比报错更难发现。

PyMuPDF 的调用是 CPU 密集型的，调用方必须通过 ``services.intake.run_in_pool``
放进线程池执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from config import settings
from pdf.loader import open_pdf
from pdf.pages import (
    format_page_list,
    parse_page_sequence,
    parse_range_groups,
    part_labels,
)
from utils.errors import PdfTooManyPagesError, ValidationError

__all__ = [
    "MODE_EVERY",
    "MODE_RANGES",
    "MODE_SELECTED",
    "SPLIT_MODES",
    "SPLIT_MODE_LABELS",
    "SplitPart",
    "SplitResult",
    "split_pdf",
]

MODE_EVERY = "every"
MODE_RANGES = "ranges"
MODE_SELECTED = "selected"

SPLIT_MODES = (MODE_EVERY, MODE_RANGES, MODE_SELECTED)

# 面向用户的方式名，报错和日志都用它，避免把内部值暴露出去
SPLIT_MODE_LABELS = {
    MODE_EVERY: "每页一个 PDF",
    MODE_RANGES: "按范围拆分",
    MODE_SELECTED: "自定义页面",
}

# 拆出来的文件统一叫 part-01.pdf …… 与 §7 的示例一致
_PART_STEM = "part"
_SELECTED_FILENAME = "selected-pages.pdf"


@dataclass(slots=True)
class SplitPart:
    """拆分结果中的一份 PDF。"""

    filename: str
    data: bytes
    pages: list[int]  # 0 开始的页码


@dataclass(slots=True)
class SplitResult:
    """一次拆分的完整结果。"""

    parts: list[SplitPart]
    total_pages: int
    notes: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        """结果各份加起来的页数（范围重叠时会大于原文件页数）。"""
        return sum(len(part.pages) for part in self.parts)


def split_pdf(
    path: Path,
    *,
    mode: str,
    value: str | None = None,
    max_parts: int | None = None,
) -> SplitResult:
    """按指定方式拆分 PDF，返回每一份的字节。

    ``value`` 的含义随 ``mode`` 变：``ranges`` 是每行一个范围的多段文本，
    ``selected`` 是单段页面范围，``every`` 不需要它。
    """
    if mode not in SPLIT_MODES:
        raise ValidationError(
            "拆分方式不正确，可选：" + "、".join(SPLIT_MODE_LABELS[m] for m in SPLIT_MODES)
        )

    limit = settings.MAX_PDF_PAGES if max_parts is None else max_parts

    with open_pdf(path) as doc:
        total_pages = doc.page_count
        groups = _build_groups(mode, value, total_pages)
        _check_part_count(groups, limit)
        names = _part_names(mode, len(groups))
        parts = [_extract(doc, pages, name) for pages, name in zip(groups, names)]

    return SplitResult(
        parts=parts,
        total_pages=total_pages,
        notes=_notes(mode, groups, total_pages),
    )


# ----------------------------------------------------------------------
# 分组
# ----------------------------------------------------------------------

def _build_groups(mode: str, value: str | None, total_pages: int) -> list[list[int]]:
    if mode == MODE_EVERY:
        return [[index] for index in range(total_pages)]
    if mode == MODE_RANGES:
        return parse_range_groups(value, total_pages)
    # selected：用户排好序的一份 PDF，页序就是输入顺序
    return [parse_page_sequence(value, total_pages)]


def _check_part_count(groups: list[list[int]], limit: int) -> None:
    """限制拆出来的份数。

    只有「按范围拆分」可能超出：每页一个最多就是总页数，
    而范围可以写成几百个互相重叠的小段，份数不受页数约束。
    """
    if len(groups) <= limit:
        return
    raise PdfTooManyPagesError(
        f"一次最多拆出 {limit} 个文件，当前填写了 {len(groups)} 段。"
        "请合并其中一些范围后重试。"
    )


def _part_names(mode: str, count: int) -> list[str]:
    if mode == MODE_SELECTED:
        return [_SELECTED_FILENAME]
    return [f"{_PART_STEM}-{label}.pdf" for label in part_labels(count)]


# ----------------------------------------------------------------------
# 抽取
# ----------------------------------------------------------------------

def _extract(doc: pymupdf.Document, pages: list[int], filename: str) -> SplitPart:
    """按页码逐个 insert_pdf 出一份新 PDF。

    用 ``insert_pdf(from_page, to_page)`` 而不是 ``select()``：
    目标页序完全由这里的循环决定，不依赖库的排序行为，
    也不会在抄页的过程中改动源文档。
    """
    part = pymupdf.open()
    try:
        for index in pages:
            part.insert_pdf(doc, from_page=index, to_page=index)
        data = part.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        part.close()

    return SplitPart(filename=filename, data=data, pages=pages)


# ----------------------------------------------------------------------
# 提示
# ----------------------------------------------------------------------

def _notes(mode: str, groups: list[list[int]], total_pages: int) -> list[str]:
    notes: list[str] = []

    if mode == MODE_EVERY:
        notes.append(f"已把 {total_pages} 页拆成 {len(groups)} 个单页 PDF。")
        return notes

    if mode == MODE_SELECTED:
        notes.append(f"已选出 {len(groups[0])} 页，合成为一份 PDF。")
        return notes

    notes.append(f"已按 {len(groups)} 个范围拆成 {len(groups)} 个文件。")

    covered = {page for group in groups for page in group}
    missing = [page for page in range(total_pages) if page not in covered]
    if missing:
        notes.append(f"第 {format_page_list(missing)} 页不在任何范围里，不会出现在结果中。")

    if sum(len(group) for group in groups) != len(covered):
        notes.append("部分范围互相重叠，重叠的页面会同时出现在多个文件中。")

    return notes
