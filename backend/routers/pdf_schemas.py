"""PDF 接口的响应模型。

六个 PDF 功能的结果形态差别很大（一个 PDF、一堆图片、一个 ZIP），
但用户要看到的东西是同一套：生成了什么、多大、多少页、省了多少。
因此所有 PDF 接口共用 :class:`PdfResultResponse`，前端只需要一套渲染逻辑。

第六阶段的 PDF → Word 产出的是一份 DOCX，用户还要额外知道
「这份 PDF 是文字版还是扫描版」「文字是怎么拿到的」。这些字段
用子类加上去，前端照旧复用同一个结果面板。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.pdf_service import PdfInput, PdfResult

__all__ = [
    "PdfInputResponse",
    "PdfFileResponse",
    "PdfResultResponse",
    "DocumentResultResponse",
    "PdfToWordProgressResponse",
    "build_pdf_response",
    "build_document_response",
]


class PdfInputResponse(BaseModel):
    """上传一份 PDF 之后的回执。"""

    input_id: str = Field(description="后续操作的凭据，也是随机 token")
    filename: str
    size: int = Field(description="字节数")
    page_count: int
    thumbnail_base: str = Field(
        description="页面缩略图地址前缀，取第 N 页（从 0 开始）时拼上 /N"
    )


class PdfFileResponse(BaseModel):
    """结果中的一个文件。"""

    filename: str
    size: int
    page_count: int | None = None
    preview_url: str | None = Field(
        default=None, description="图片类结果的预览地址，不消耗下载令牌"
    )


class PdfResultResponse(BaseModel):
    """PDF 处理接口的统一响应。"""

    job_id: str
    download_url: str = Field(description="下载地址；结果多于一个文件时为 ZIP")
    filename: str = Field(description="下载时的文件名")
    size: int = Field(description="下载内容的字节数")
    media_type: str
    archived: bool = Field(default=False, description="结果是否打包成了 ZIP")
    archive_filename: str | None = None

    page_count: int | None = Field(default=None, description="结果 PDF 的页数")
    original_size: int | None = Field(default=None, description="原文件字节数")
    original_pages: int | None = Field(default=None, description="原 PDF 的页数")
    saved_bytes: int | None = None
    saved_percent: float | None = None

    files: list[PdfFileResponse] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="需要主动告诉用户的说明")


def build_input_response(info: PdfInput) -> PdfInputResponse:
    return PdfInputResponse(
        input_id=info.input_id,
        filename=info.filename,
        size=info.size,
        page_count=info.page_count,
        thumbnail_base=f"/api/pdf/input/{info.input_id}/thumb",
    )


def build_pdf_response(result: PdfResult) -> PdfResultResponse:
    """把服务层的结果转换成接口响应。"""
    job = result.job
    return PdfResultResponse(
        job_id=job.job_id,
        download_url=f"/api/download/{job.job_id}",
        filename=job.filename,
        size=job.size,
        media_type=job.media_type,
        archived=result.archived,
        archive_filename=result.archive_filename,
        page_count=result.page_count,
        original_size=result.original_size,
        original_pages=result.original_pages,
        saved_bytes=result.saved_bytes,
        saved_percent=result.saved_percent,
        files=[
            PdfFileResponse(
                filename=output.filename,
                size=output.size,
                page_count=output.page_count,
                preview_url=(
                    f"/api/preview/{job.job_id}/{index}" if output.previewable else None
                ),
            )
            for index, output in enumerate(result.outputs)
        ],
        notes=result.notes,
    )


#: 文档形态 -> 结果页上显示的中文。前端不自己拼这些字，
#: 免得同一句话在后端 notes 和前端页面上有两种说法。
_DOCUMENT_KIND_LABELS = {
    "text": "文字 PDF",
    "scan": "扫描 PDF",
    "mixed": "混合 PDF",
}

#: 识别方式 -> 结果页文案
_EXTRACTION_LABELS = {
    "text": "直接提取文字层",
    "ocr": "OCR 识别",
    "mixed": "直接提取 + OCR 识别",
}


class DocumentResultResponse(PdfResultResponse):
    """PDF → Word 的响应。在通用结果之上补出「这份 PDF 是什么、文字怎么来的」。"""

    document_kind: str = Field(description="text / scan / mixed")
    document_kind_label: str = Field(description="结果页直接显示的中文")
    extraction_method: str = Field(description="text / ocr / mixed")
    extraction_label: str
    page_kinds: list[str] = Field(
        default_factory=list, description="每一页的形态，顺序与页序一致"
    )
    text_pages: int = 0
    ocr_pages: int = 0
    ocr_languages: list[str] = Field(
        default_factory=list,
        description="内置 OCR 模型覆盖的字符集，只读；不是可切换的语言包",
    )


class PdfToWordProgressResponse(BaseModel):
    """PDF → Word 的阶段进度。

    **只有阶段状态，没有百分比。** 唯一能诚实算出来的百分比是 OCR 期间的
    「第几页 / 共几页」，所以 ``page`` / ``page_count`` 只在那个阶段有值，
    其余阶段是 null —— 与其编一个 50%、70%，不如让前端老实显示「正在分析 PDF…」。
    """

    stage: str = Field(description="analyzing / detecting / extracting / ocr / writing / verifying")
    page: int | None = Field(default=None, description="OCR 正在处理第几页（从 1 开始）")
    page_count: int | None = None


def build_document_response(result) -> DocumentResultResponse:
    """把 PDF → Word 的结果转换成接口响应。"""
    base = build_pdf_response(result)
    return DocumentResultResponse(
        **base.model_dump(),
        document_kind=result.document_kind,
        document_kind_label=_DOCUMENT_KIND_LABELS.get(
            result.document_kind, result.document_kind
        ),
        extraction_method=result.extraction_method,
        extraction_label=_EXTRACTION_LABELS.get(
            result.extraction_method, result.extraction_method
        ),
        page_kinds=list(result.page_kinds),
        text_pages=result.text_pages,
        ocr_pages=result.ocr_pages,
        ocr_languages=list(result.ocr_languages),
    )
