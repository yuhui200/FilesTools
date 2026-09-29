"""图片压缩接口（第四阶段：改为批量 + 异步任务）。

请求体与前三阶段一致（多选文件 + 质量档位 + 目标大小），
变化在于：接口立刻返回 ``group_id``，处理在后台队列里逐个文件进行，
前端轮询 ``/api/tasks/{group_id}`` 拿真实进度与每个文件的结果（§3 / §4）。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile

from compressors.pipeline import PipelineOptions
from config import settings
from routers.params import parse_quality, parse_target_bytes
from routers.tasks import TaskCreatedResponse, build_task_response
from tasks.image_tasks import submit_image_batch

router = APIRouter(prefix="/api/image", tags=["image"])

# 结果文件名的后缀：report.jpg -> report_compressed.jpg
OUTPUT_SUFFIX = "_compressed"
ARCHIVE_STEM = "compressed_images"


@router.post(
    "/compress",
    response_model=TaskCreatedResponse,
    status_code=202,
    summary="按目标大小压缩图片（批量）",
)
async def compress_images_endpoint(
    files: list[UploadFile] = File(..., description="待压缩的图片，可多选"),
    quality: str = Form(settings.DEFAULT_QUALITY_PRESET, description="high / balanced / strong"),
    target_bytes: str | None = Form(None, description="目标大小上限（字节），留空表示只按质量压缩"),
) -> TaskCreatedResponse:
    """提交压缩任务，立刻返回任务号。"""
    options = PipelineOptions(
        quality_preset=parse_quality(quality),
        target_bytes=parse_target_bytes(target_bytes),
    )
    group = await submit_image_batch(
        tool="image.compress",
        options=options,
        uploads=files,
        suffix=OUTPUT_SUFFIX,
        archive_stem=ARCHIVE_STEM,
    )
    return build_task_response(group)
