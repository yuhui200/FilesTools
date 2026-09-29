"""PDF 合并。

按给定顺序把多份 PDF 接成一份。顺序完全由调用方决定 ——
用户在前端拖出来的顺序就是最终页序，这里不做任何排序。

两个细节是刻意的：

- **每一份来源都要单独校验**：一份损坏的 PDF 不能因为「反正要合并」
  就蒙混过关，否则用户拿到一份缺页的合并结果，比直接报错更难发现。
- **合并后的总页数也要卡上限**：单独看每份都不超标，合起来几千页
  同样能把服务拖垮，所以限制必须落在结果上。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from config import settings
from pdf.loader import open_pdf
from utils.errors import ValidationError

__all__ = ["MergeSource", "MergeResult", "merge_pdfs"]


@dataclass(slots=True)
class MergeSource:
    """一份待合并的 PDF。"""

    filename: str
    path: Path


@dataclass(slots=True)
class MergeResult:
    """合并结果。"""

    data: bytes
    page_count: int
    sources: int


def merge_pdfs(sources: list[MergeSource], *, max_pages: int | None = None) -> MergeResult:
    """按顺序合并，返回合并后的 PDF 字节。"""
    if not sources:
        raise ValidationError("请至少上传一个 PDF 文件")

    limit = settings.MAX_PDF_PAGES if max_pages is None else max_pages

    merged = pymupdf.open()
    try:
        total = 0
        for source in sources:
            with open_pdf(source.path) as doc:
                total += doc.page_count
                if total > limit:
                    raise ValidationError(
                        f"合并后共 {total} 页，超过一次最多处理 {limit} 页的限制。"
                        "请减少文件数量，或先用「PDF 拆分」分成几份再合并。"
                    )
                # links/annots 默认就带过来：合并后的目录和批注不该丢
                merged.insert_pdf(doc)

        data = merged.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        merged.close()

    return MergeResult(data=data, page_count=total, sources=len(sources))
