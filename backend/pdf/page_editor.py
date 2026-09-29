"""PDF 页面删除与提取。

第三阶段 §8、§9 是两个功能，但它们的内核是同一件事：
**给定「保留哪几页」，重建一份 PDF**。区别只是选择的方向相反 ——

- 删除页面：用户在缩略图上点掉几页，保留的是「总页数 - 点掉的页」；
- 提取页面：用户点中几页，保留的就是点中的这几页。

所以这里只有一个 ``keep`` 参数，两个入口函数各自把用户的选择翻过来。
写两套循环、两套边界检查，迟早会出现「删除时能删到只剩一页，
提取时却允许零页」这类不一致。

页序：**删除**保留的是文档原有的先后顺序（用户只是挑掉几页），
**提取**则完全按用户排定的顺序输出（第四阶段 §11 支持拖拽排序，
用户把第 5 页拖到最前面，结果的第一页就是原来的第 5 页）。
所以这里两个入口用的解析函数不同：删除用 :func:`parse_page_range`（升序去重），
提取用 :func:`parse_page_sequence`（保留书写顺序）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from pdf.loader import open_pdf
from pdf.pages import format_page_list, parse_page_range, parse_page_sequence
from utils.errors import ValidationError

__all__ = [
    "EditResult",
    "delete_pages",
    "extract_pages",
    "describe_pages",
]

# 提示里最多列出多少页的页码，超出就只说数量 ——
# 「已删除 1,2,3,…,180 页」这种提示没人看得下去
_MAX_LISTED_PAGES = 20


@dataclass(slots=True)
class EditResult:
    """一次页面编辑的结果。"""

    data: bytes
    page_count: int              # 结果文件的页数
    source_pages: int            # 原文件页数
    changed: list[int] = field(default_factory=list)  # 被删掉 / 被提取的页（0 基）
    notes: list[str] = field(default_factory=list)


def delete_pages(path: Path, pages_raw: str | None) -> EditResult:
    """删掉指定页面，保留其余所有页。"""
    with open_pdf(path) as doc:
        source_pages = doc.page_count
        removed = parse_page_range(pages_raw, source_pages)

        if len(removed) >= source_pages:
            # 全删掉等于生成一份空 PDF，没有意义，也会让后续功能拿到 0 页的文件
            raise ValidationError("不能删除全部页面，请至少保留一页")

        keep = [index for index in range(source_pages) if index not in set(removed)]
        data = _rebuild(doc, keep)

    return EditResult(
        data=data,
        page_count=len(keep),
        source_pages=source_pages,
        changed=removed,
        notes=[
            f"已删除 {len(removed)} 页（{describe_pages(removed)}），保留 {len(keep)} 页。"
        ],
    )


def extract_pages(path: Path, pages_raw: str | None) -> EditResult:
    """只保留指定页面，按用户给定的顺序合成一份新的 PDF。"""
    with open_pdf(path) as doc:
        source_pages = doc.page_count
        keep = parse_page_sequence(pages_raw, source_pages)
        data = _rebuild(doc, keep)

    return EditResult(
        data=data,
        page_count=len(keep),
        source_pages=source_pages,
        changed=keep,
        notes=[
            f"已提取 {len(keep)} 页（{describe_pages(keep)}），合成为一份 PDF。"
        ],
    )


def _rebuild(doc: pymupdf.Document, keep: list[int]) -> bytes:
    """按保留的页码顺序重建 PDF。

    用 ``select`` 而不是逐页 ``insert_pdf``：它由库直接处理页表、
    资源去重和交叉引用，比新建文档再抄一遍更快，也不容易漏掉注释。
    ``select`` 本身支持任意顺序与重复页，页序与传入的 ``keep`` 完全一致
    （这一点由 ``tests`` 里的顺序用例守着）。
    """
    if not keep:
        raise ValidationError("没有保留任何页面，请至少选择一页")

    try:
        doc.select(keep)
        return doc.tobytes(deflate=True, garbage=4, clean=True)
    except Exception as exc:  # pragma: no cover - 库内部的意外失败
        raise ValidationError("生成新的 PDF 失败，文件可能已损坏") from exc


def describe_pages(pages: list[int]) -> str:
    """把页码列表写成提示文案。

    顺序有意义（提取）时就按原样列出，不去压缩成 ``1-3`` ——
    提示里的页序应当和结果文件的页序一致，否则用户会以为排序没生效。
    过长时只说数量。
    """
    if len(pages) > _MAX_LISTED_PAGES:
        return f"共 {len(pages)} 页"
    if pages == sorted(set(pages)):
        return format_page_list(pages)
    return ", ".join(str(page + 1) for page in pages)
