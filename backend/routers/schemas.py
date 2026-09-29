"""接口的响应模型与视图转换。

压缩、格式转换、尺寸调整三个接口共用这些结构，前端也只需要一套解析逻辑。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from conversion.registry import INLINE_PREVIEW_MEDIA_TYPES
from services.intake import media_type_for
from services.transform_service import BatchSummary, ItemSummary


class FileInfo(BaseModel):
    """一个文件的基本信息。"""

    filename: str
    size: int = Field(description="字节数")
    width: int | None = None
    height: int | None = None
    format: str | None = None


class ItemResponse(BaseModel):
    """批量处理中单个文件的结果。"""

    index: int = Field(description="对应上传顺序，从 0 开始")
    original: FileInfo
    result: FileInfo
    preview_url: str | None = Field(
        default=None,
        description=(
            "结果图片的预览地址，不消耗下载令牌；"
            "**浏览器渲染不了的格式（TIFF / HEIC / ICO）为 null** —— "
            "那里没有可预览的东西，给一个地址只会点出一张破图"
        ),
    )
    saved_bytes: int
    saved_percent: float
    target_met: bool = True
    quality_used: int | None = None
    scale: float = 1.0
    untouched: bool = False
    note: str | None = None


class FailureResponse(BaseModel):
    """批量处理中单个文件的失败原因。"""

    index: int
    filename: str
    code: str
    message: str


class BatchResponse(BaseModel):
    """批量处理接口的响应。"""

    job_id: str
    download_url: str = Field(description="下载地址；结果多于一个文件时为 ZIP")
    items: list[ItemResponse]
    failures: list[FailureResponse] = Field(default_factory=list)
    original_total: int
    result_total: int
    saved_bytes: int
    saved_percent: float
    target_bytes: int | None = None
    archived: bool = Field(default=False, description="结果是否打包成了 ZIP")
    archive_filename: str | None = None


# ----------------------------------------------------------------------
# 视图转换
# ----------------------------------------------------------------------

def _to_file_info(item: ItemSummary, *, result: bool) -> FileInfo:
    if result:
        return FileInfo(
            filename=item.result_filename,
            size=item.result_size,
            width=item.result_width,
            height=item.result_height,
            format=item.result_format,
        )
    return FileInfo(
        filename=item.original.filename,
        size=item.original.size,
        width=item.original.width,
        height=item.original.height,
        format=item.original.format,
    )


def build_batch_response(job_id: str, summary: BatchSummary) -> BatchResponse:
    """把服务层的摘要转换成接口响应。"""
    return BatchResponse(
        job_id=job_id,
        download_url=f"/api/download/{job_id}",
        items=[
            ItemResponse(
                index=item.index,
                original=_to_file_info(item, result=False),
                result=_to_file_info(item, result=True),
                preview_url=(
                    f"/api/preview/{job_id}/{item.index}"
                    # 与 ``routers/download.py`` 那条路由**同一张表**：这边发得出
                    # 地址、那边就一定放行，反之亦然。任一边单独改，得到的都是
                    # 一个点了报错（或显示破图）的缩略图（第十阶段 C §五）。
                    if media_type_for(item.result_format or "")
                    in INLINE_PREVIEW_MEDIA_TYPES
                    else None
                ),
                saved_bytes=item.saved_bytes,
                saved_percent=item.saved_percent,
                target_met=item.target_met,
                quality_used=item.quality_used,
                scale=item.scale,
                untouched=item.untouched,
                note=item.note,
            )
            for item in summary.items
        ],
        failures=[
            FailureResponse(
                index=failure.index,
                filename=failure.filename,
                code=failure.code,
                message=failure.message,
            )
            for failure in summary.failures
        ],
        original_total=summary.original_total,
        result_total=summary.result_total,
        saved_bytes=summary.saved_bytes,
        saved_percent=summary.saved_percent,
        target_bytes=summary.target_bytes,
        archived=summary.archived,
        archive_filename=summary.archive_filename,
    )
