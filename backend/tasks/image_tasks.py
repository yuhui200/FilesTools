"""图片类批量任务：压缩 / 格式转换 / 尺寸调整（第四阶段 §2）。

三个功能共用同一条流水线，也就共用同一套任务处理器与收尾器，
差别只有「传给流水线的参数」和「结果文件叫什么名字」，
因此这里只有一个入口 :func:`submit_image_batch`，三个路由各自调用它。

一次请求的时间线：

    POST 到达 → 逐个流式落盘（便宜的检查：扩展名、单文件大小、整批总量）
             → 建任务组 → 立刻返回 group_id
    队列     → 逐文件校验、处理、写结果（慢活都在这里）
    收尾     → 文件名去重、打包、登记一次性下载令牌
"""

from __future__ import annotations

from fastapi import UploadFile

from compressors.pipeline import PipelineOptions
from config import settings
from routers.schemas import build_batch_response
from services.batch_service import receive_uploads
from services.intake import run_in_pool
from services.queue_service import TaskGroup, task_queue
from services.transform_service import finalize_image_batch, make_image_handler
from utils.errors import ValidationError
from utils.files import create_temp_dir, new_token, remove_dir

__all__ = ["IMAGE_TOOLS", "submit_image_batch"]

# 工具标识 → 中文名。中文名用于「最近处理」记录与任务标题。
IMAGE_TOOLS: dict[str, str] = {
    "image.compress": "图片压缩",
    "image.convert": "图片格式转换",
    "image.resize": "图片尺寸调整",
}

def _invalid_type_message() -> str:
    """「支持哪些图片格式」这句话 —— **从白名单派生**，不手抄。

    这句话原先写死成「只支持 JPG / PNG / WEBP 格式的图片」。第九阶段给白名单
    加了 BMP / GIF / TIFF、第十阶段 A 又加了 HEIC / HEIF，**这句话一次都没跟着
    改**：用户传一个合法的 ``.tiff`` 被拒时，会被告知一个与他无关的更小集合，
    于是以为自己手里的格式真的不行。

    抄一份清单就有一次漏改的机会，所以这里直接读
    :data:`config.settings.ALLOWED_IMAGE_EXTENSIONS` —— 它正是
    ``submit_image_batch`` 交给 :func:`receive_uploads` 的那份放行清单。
    两者同源，「说的是什么」与「收的是什么」就不可能分家。
    """
    formats = " / ".join(
        sorted(extension.lstrip(".").upper() for extension in settings.ALLOWED_IMAGE_EXTENSIONS)
    )
    return f"只支持 {formats} 格式的图片"


INVALID_TYPE_MESSAGE = _invalid_type_message()


def _total_limit_message() -> str:
    limit_mb = settings.MAX_BATCH_TOTAL_BYTES // (1024 * 1024)
    return f"一次最多处理 {limit_mb} MB 的文件，请分批上传"


async def submit_image_batch(
    *,
    tool: str,
    options: PipelineOptions,
    uploads: list[UploadFile],
    suffix: str,
    archive_stem: str,
) -> TaskGroup:
    """接收一批图片，建好任务组并交给队列，立即返回。

    参数校验（数量、总量）都在这里同步完成 —— 这类错误必须立刻以 4xx 返回，
    让用户改完重传；真正耗时的解码与压缩交给队列，
    它们的结果会作为「这一个文件失败」被记录下来（§4）。
    """
    if not uploads:
        raise ValidationError("请至少上传一个文件")
    if len(uploads) > settings.MAX_BATCH_FILES:
        raise ValidationError(f"一次最多处理 {settings.MAX_BATCH_FILES} 个文件")

    work_dir = create_temp_dir()
    try:
        items = await receive_uploads(
            uploads,
            work_dir,
            allowed_extensions=settings.ALLOWED_IMAGE_EXTENSIONS,
            invalid_type_message=INVALID_TYPE_MESSAGE,
            total_limit_message=_total_limit_message(),
        )
    except BaseException:
        # 上传中断（含整批超限）：本次临时目录不会有别人接手，就地清掉
        remove_dir(work_dir)
        raise

    group = TaskGroup(
        group_id=new_token(),
        tool=tool,
        label=IMAGE_TOOLS.get(tool, "图片处理"),
        directory=work_dir,
        items=items,
        handler=make_image_handler(options, suffix),
        finalizer=_make_finalizer(options, archive_stem),
    )
    return await task_queue.submit(group)


def _make_finalizer(options: PipelineOptions, archive_stem: str):
    """生成收尾器：去重、打包、登记令牌，并把响应体存进任务组。"""

    async def finalize(group: TaskGroup) -> None:
        # 打包要在**线程池**里做：50 个结果整块复制是纯磁盘操作，
        # 放在事件循环里会让所有轮询请求一起卡住。
        summary = await run_in_pool(
            finalize_image_batch,
            group,
            options,
            archive_stem,
            timeout=settings.BATCH_TIMEOUT_SECONDS,
            timeout_message=(
                f"打包结果超时（超过 {settings.BATCH_TIMEOUT_SECONDS} 秒），"
                "请减少文件数量后重试"
            ),
        )
        if summary is None:
            # 一个都没成功：不给下载地址，交给队列标记整组失败
            return
        job_id = group.job_id
        if job_id is None:  # pragma: no cover - 有结果就一定有令牌
            return
        group.summary = build_batch_response(job_id, summary).model_dump()

    return finalize
