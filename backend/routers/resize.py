"""图片尺寸调整接口（第四阶段：改为异步任务）。

支持指定宽度、高度或两者同时指定；勾选「保持宽高比例」时按原图比例自动联动，
一次最多处理 MAX_BATCH_FILES 张图片，可附带目标大小控制。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile

from compressors.pipeline import PipelineOptions
from compressors.resizer import ResizeRequest
from routers.params import (
    parse_edge,
    parse_keep_aspect,
    parse_quality_value,
    parse_target_bytes,
)
from routers.tasks import TaskCreatedResponse, build_task_response
from tasks.image_tasks import submit_image_batch
from utils.errors import ValidationError

router = APIRouter(prefix="/api/image", tags=["image"])

OUTPUT_SUFFIX = "_resized"
ARCHIVE_STEM = "resized_images"


@router.post(
    "/resize",
    response_model=TaskCreatedResponse,
    status_code=202,
    summary="图片尺寸调整（批量）",
)
async def resize_images_endpoint(
    files: list[UploadFile] = File(..., description="待调整的图片，可多选"),
    width: str | None = Form(None, description="目标宽度（像素），留空表示按高度等比推导"),
    height: str | None = Form(None, description="目标高度（像素），留空表示按宽度等比推导"),
    keep_aspect: str | None = Form(None, description="是否保持宽高比例，默认 true"),
    quality_value: str | None = Form(None, description="图片质量 1-100，留空使用默认值 80"),
    target_bytes: str | None = Form(None, description="目标大小上限（字节），留空表示不限制"),
) -> TaskCreatedResponse:
    """提交尺寸调整任务，立刻返回任务号。"""
    target_width = parse_edge(width, "宽度")
    target_height = parse_edge(height, "高度")
    if target_width is None and target_height is None:
        raise ValidationError("请至少填写宽度或高度")

    options = PipelineOptions(
        quality_value=parse_quality_value(quality_value),
        target_bytes=parse_target_bytes(target_bytes),
        resize=ResizeRequest(
            width=target_width,
            height=target_height,
            keep_aspect=parse_keep_aspect(keep_aspect),
        ),
    )
    group = await submit_image_batch(
        tool="image.resize",
        options=options,
        uploads=files,
        suffix=OUTPUT_SUFFIX,
        archive_stem=ARCHIVE_STEM,
    )
    return build_task_response(group)
