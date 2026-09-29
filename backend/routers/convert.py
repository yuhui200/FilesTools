"""图片格式转换接口（第四阶段：改为异步任务）。

支持 JPG / PNG / WEBP 之间任意互转，可附带质量设置与目标大小控制，
一次最多处理 MAX_BATCH_FILES 张图片。

接口立刻返回任务号，处理在后台队列里逐个文件进行 ——
一张坏图只会让「它自己」失败，同批其它文件照常出结果（§4）。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile

from compressors.pipeline import PipelineOptions
from routers.params import (
    parse_format,
    parse_quality_value,
    parse_target_bytes,
)
from routers.tasks import TaskCreatedResponse, build_task_response
from tasks.image_tasks import submit_image_batch

router = APIRouter(prefix="/api/image", tags=["image"])

# 结果文件名的后缀。格式转换靠扩展名就能看出来，不再额外加后缀。
OUTPUT_SUFFIX = ""
ARCHIVE_STEM = "converted_images"


@router.post(
    "/convert",
    response_model=TaskCreatedResponse,
    status_code=202,
    summary="图片格式转换（批量）",
)
async def convert_images_endpoint(
    files: list[UploadFile] = File(..., description="待转换的图片，可多选"),
    target_format: str = Form(..., description="目标格式：jpg / png / webp"),
    quality_value: str | None = Form(None, description="图片质量 1-100，留空使用默认值 80"),
    quality_preset: str | None = Form(None, description="质量档位，与 quality_value 二选一"),
    target_bytes: str | None = Form(None, description="目标大小上限（字节），留空表示不限制"),
) -> TaskCreatedResponse:
    """提交格式转换任务，立刻返回任务号。"""
    options = PipelineOptions(
        target_format=parse_format(target_format),
        quality_preset=quality_preset or None,
        quality_value=parse_quality_value(quality_value),
        target_bytes=parse_target_bytes(target_bytes),
    )
    group = await submit_image_batch(
        tool="image.convert",
        options=options,
        uploads=files,
        suffix=OUTPUT_SUFFIX,
        archive_stem=ARCHIVE_STEM,
    )
    return build_task_response(group)
