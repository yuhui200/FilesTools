"""统一转换中心接口（第七阶段 §二十八）。

六个端点：

======================  ==================================================
``GET  /capabilities``  服务器当前真的能做什么（§七）
``POST /tasks``         提交一批同源同目标的文件
``GET  /tasks/{id}``    整批状态 + 每项状态（§八）
``GET  /tasks/{id}/progress``  轻量进度，轮询用
``POST /tasks/{id}/cancel``    请求取消整批（协作式，§十五）
``POST /tasks/{task_id}/retry`` 重试单个失败项（§十六）
======================  ==================================================

**前六个阶段的路由一个都没有改动**：这里是新增入口，不是替换。
图片 / PDF / Office 的既有页面继续走它们原来的接口。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Query, UploadFile, status

from compressors.resizer import ResizeRequest
from routers.conversion_params import resolve_options
from routers.conversion_schemas import (
    ConversionBatchCreatedResponse,
    ConversionBatchStatusResponse,
    ConversionCapabilitiesResponse,
    ConversionProgressResponse,
    ConversionTaskRef,
    build_batch_status,
    split_task_id,
    task_id_for,
)
from routers.params import (
    parse_edge,
    parse_keep_aspect,
    parse_quality,
    parse_quality_value,
    parse_target_bytes,
)
from services.conversion_service import ConversionOptions, conversion_capabilities
from services.queue_service import task_queue
from tasks.conversion_tasks import submit_conversion_batch
from utils.errors import TaskNotFoundError, ValidationError

router = APIRouter(prefix="/api/conversion", tags=["conversion"])

_TARGET_DESC = "这一批要转成什么，取值见 /api/conversion/capabilities"
_QUALITY_PRESET_DESC = "图片质量档位；留空表示使用默认值"
_QUALITY_VALUE_DESC = "自定义图片质量（1-100）；留空表示不指定"
_TARGET_BYTES_DESC = "目标最大字节数；留空表示不限制。图片按重编码逼近，文档转完再压"
_WIDTH_DESC = "结果图片宽度（像素）；留空表示不缩放"
_HEIGHT_DESC = "结果图片高度（像素）；留空表示不缩放"
_KEEP_ASPECT_DESC = "缩放时是否保持比例，默认 true"
_PROGRESS_IDS_DESC = (
    "与 files 顺序对齐的进度 id（可选）。只有 PDF 转 Word 会用它上报真实页码"
)
_OPTIONS_DESC = (
    "第九阶段的统一参数，JSON 对象（扁平点号键，如 "
    '{"resize.mode":"small"}）。键的取值范围见 capabilities 里的 options_schema；'
    "留空表示只用上面那些扁平字段"
)
_CAPABILITY_ID_DESC = (
    "前端选中的那条能力的 ID（如 image.jpg-to-png，可选）。"
    "声明后参数只按这一条能力的 schema 校验，多出来的键会明确报错"
)


@router.get(
    "/capabilities",
    response_model=ConversionCapabilitiesResponse,
    summary="查询支持哪些转换",
)
async def capabilities_endpoint(
    source_type: str | None = Query(None, description="只看以这种格式为输入的转换"),
    target_type: str | None = Query(None, description="只看能转成这种格式的转换"),
    category: str | None = Query(None, description="只看某一类：image / document / pdf"),
    operation_type: str | None = Query(
        None, description="只看某一类操作：conversion（1→1）/ operation（工具）"
    ),
) -> ConversionCapabilitiesResponse:
    """返回**服务器当前真的能做的**转换，以及缺组件时的原因说明。

    不可用的能力直接从矩阵里消失（例如没装 LibreOffice 就没有 Office 转 PDF），
    同时 ``notes`` 里会说明为什么 —— 能力被悄悄藏起来、界面上什么都不说，
    用户只会以为网站坏了。

    **这是前端唯一的能力来源**（§十五）：``conversions[]`` 给出每一条转换的
    稳定 ID、分类、资源池与参数描述，前端不需要（也不允许）自己维护一份
    能力矩阵。四个查询参数会同时收窄整个响应。
    """
    return ConversionCapabilitiesResponse(
        **conversion_capabilities(
            source_type=source_type,
            target_type=target_type,
            category=category,
            operation_type=operation_type,
        )
    )


@router.post(
    "/tasks",
    response_model=ConversionBatchCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交一批转换任务",
)
async def create_tasks_endpoint(
    files: list[UploadFile] = File(description="要转换的文件，可多个"),
    target_type: str = Form(description=_TARGET_DESC),
    quality_preset: str | None = Form(None, description=_QUALITY_PRESET_DESC),
    quality_value: str | None = Form(None, description=_QUALITY_VALUE_DESC),
    target_bytes: str | None = Form(None, description=_TARGET_BYTES_DESC),
    width: str | None = Form(None, description=_WIDTH_DESC),
    height: str | None = Form(None, description=_HEIGHT_DESC),
    keep_aspect: str | None = Form(None, description=_KEEP_ASPECT_DESC),
    progress_ids: list[str] = Form(default=[], description=_PROGRESS_IDS_DESC),
    options: str | None = Form(None, description=_OPTIONS_DESC),
    capability_id: str | None = Form(None, description=_CAPABILITY_ID_DESC),
):
    """收下一批文件，立刻返回任务号；转换在后台队列里逐个进行。

    返回 202（与另外五个批量接口同一约定）：任务已经受理，但还没开始跑。

    每个文件的真实格式由服务端按内容判定，**不采信客户端的分组** ——
    客户端的分组只用于界面排版。判定不通过的文件会作为一项失败出现在结果里，
    并带上具体原因，**不会让整批失败**（§五）。

    ``options`` 与 ``capability_id`` 是第九阶段新增的**可选**字段（§四十五）：
    不传时参数完全由上面那九个扁平字段决定，行为与第七阶段逐字节相同；
    传了则先过五层校验链，覆盖同名的扁平值，再交给同一条执行路径。
    """
    parsed = resolve_options(
        raw_options=options,
        raw_capability_id=capability_id,
        target_type=target_type,
        base=ConversionOptions(
            quality_preset=_parse_quality_preset(quality_preset),
            quality_value=parse_quality_value(quality_value),
            target_bytes=parse_target_bytes(target_bytes),
            resize=_parse_resize(width, height, keep_aspect),
        ),
    )

    group = await submit_conversion_batch(
        uploads=files,
        target_type=target_type,
        options=parsed.options,
        options_payload=parsed.payload,
        progress_ids=list(progress_ids) or None,
    )

    return ConversionBatchCreatedResponse(
        batch_id=group.group_id,
        total=len(group.items),
        tasks=[
            ConversionTaskRef(
                task_id=task_id_for(group.group_id, item.index),
                index=item.index,
                filename=item.filename,
                status="queued" if not item.is_terminal() else "failed",
            )
            for item in group.items
        ],
        status_url=f"/api/conversion/tasks/{group.group_id}",
        progress_url=f"/api/conversion/tasks/{group.group_id}/progress",
        cancel_url=f"/api/conversion/tasks/{group.group_id}/cancel",
    )


@router.get(
    "/tasks/{batch_id}",
    response_model=ConversionBatchStatusResponse,
    summary="查询整批转换状态",
)
async def batch_status_endpoint(batch_id: str) -> ConversionBatchStatusResponse:
    """整批状态 + 每一项的状态、真实进度、失败原因与下载地址。

    某一项失败是任务的结果，不是这次查询的错误 —— 和第四阶段一样，
    只要任务还在，**轮询永远返回 200**。
    """
    group = task_queue.get(batch_id)
    if group is None:
        raise TaskNotFoundError("任务不存在或已过期，请重新提交")
    return build_batch_status(group)


@router.get(
    "/tasks/{batch_id}/progress",
    response_model=ConversionProgressResponse,
    summary="查询整批进度（轻量）",
)
async def batch_progress_endpoint(batch_id: str) -> ConversionProgressResponse:
    """只回计数，不回逐项明细，供高频轮询使用。"""
    group = task_queue.get(batch_id)
    if group is None:
        raise TaskNotFoundError("任务不存在或已过期，请重新提交")
    full = build_batch_status(group)
    return ConversionProgressResponse(
        batch_id=full.batch_id,
        status=full.status,
        cancelling=full.cancelling,
        progress=full.progress,
        total=full.total,
        queued=full.queued,
        processing=full.processing,
        completed=full.completed,
        failed=full.failed,
        cancelled=full.cancelled,
    )


@router.post(
    "/tasks/{batch_id}/cancel",
    response_model=ConversionBatchStatusResponse,
    summary="请求取消整批转换",
)
async def cancel_batch_endpoint(batch_id: str) -> ConversionBatchStatusResponse:
    """请求取消。**协作式，不是立刻停**（§十五）。

    还在排队的文件会立刻取消；**已经在转换的那个停不下来** ——
    重活在线程池里（LibreOffice / OCR），``asyncio`` 取消不了线程。
    它的状态会如实显示成 ``cancelling``，等它自己跑完之后再变成 ``cancelled``。
    返回的是请求之后的最新状态，前端据此如实展示，不假装已经停了。
    """
    group = await task_queue.cancel(batch_id)
    if group is None:
        raise TaskNotFoundError("任务不存在或已过期，请重新提交")
    return build_batch_status(group)


@router.post(
    "/tasks/{task_id}/retry",
    response_model=ConversionBatchStatusResponse,
    summary="重试单个失败的文件",
)
async def retry_task_endpoint(task_id: str) -> ConversionBatchStatusResponse:
    """把某一项失败的文件重新排进队列。

    只有失败项能重试，且**每项最多一次**（§十六）—— 上限在服务端强制，
    不靠前端自觉。排队中 / 已完成 / 已取消的项、以及源文件已经被清理的项
    都会返回 ``TASK_NOT_RETRYABLE``（409）。

    刻意**不做自动重试**：LibreOffice 与 OCR 都是重活，
    自动重试一个必然失败的任务只会把它们打爆。
    """
    parsed = split_task_id(task_id)
    if parsed is None:
        raise ValidationError("任务号格式不对，请从提交结果里取 task_id")

    batch_id, index = parsed
    await task_queue.retry(batch_id, index)

    group = task_queue.get(batch_id)
    if group is None:  # pragma: no cover - retry 已确认它存在且未过期
        raise TaskNotFoundError("任务不存在或已过期，请重新提交")
    return build_batch_status(group)


# ----------------------------------------------------------------------
# 参数解析
#
# 全部复用既有的解析器，白名单与错误文案因此和另外十四个工具完全一致。
# ----------------------------------------------------------------------

def _parse_quality_preset(raw: str | None) -> str | None:
    """质量档位。

    留空返回 ``None``（编码器用自己的默认质量），**不是**回退到某个档位 ——
    这两件事在 `run_pipeline` 里结果不同：档位会走目标大小搜索，
    而不指定档位只是按默认质量编一次。
    """
    if raw is None or not raw.strip():
        return None
    return parse_quality(raw)


def _parse_resize(
    width: str | None, height: str | None, keep_aspect: str | None
) -> ResizeRequest | None:
    """尺寸调整。两条边都没填就不缩放（返回 None，而不是空对象）。"""
    parsed_width = parse_edge(width, "宽度")
    parsed_height = parse_edge(height, "高度")
    if parsed_width is None and parsed_height is None:
        return None
    return ResizeRequest(
        width=parsed_width,
        height=parsed_height,
        keep_aspect=parse_keep_aspect(keep_aspect),
    )
