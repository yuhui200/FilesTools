"""上传文件的接收与校验。

压缩、格式转换、尺寸调整三个功能共用这一层，保证：

- 三个功能共享**同一个线程池**，并发上限是全局的，不会因为多开接口而翻倍；
- 落盘、校验、超时、清理的逻辑只有一份，安全要求不会因为新增功能而漏掉。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from fastapi import UploadFile

from config import settings
from services.job_store import Job, job_store
from utils.errors import ProcessingError, ProcessingTimeoutError, ServerBusyError
from utils.files import new_token, remove_file, save_upload_limited
from utils.validation import validate_image_upload

T = TypeVar("T")

# 图片处理是 CPU 密集型的，放到独立线程池，并限制并发数。
# 这里是全局唯一的线程池，所有接口共用。
_executor = ThreadPoolExecutor(max_workers=settings.MAX_WORKERS, thread_name_prefix="filetools-img")


def shutdown_executor() -> None:
    """服务关闭时释放线程池。"""
    _executor.shutdown(wait=False, cancel_futures=True)


@dataclass(slots=True)
class UploadInfo:
    """一张上传图片的基本信息。"""

    filename: str
    size: int
    width: int
    height: int
    format: str


async def intake_upload(
    upload: UploadFile,
    work_dir: Path,
    *,
    max_bytes: int | None = None,
) -> tuple[UploadInfo, Path]:
    """把一张上传的图片落盘并完成三层校验。

    返回 (图片信息, 落盘路径)。无论成功还是失败，都会关闭上传流；
    失败时删除已写入的半成品文件，临时目录由调用方统一清理。
    """
    original_filename = upload.filename or "image"
    source = work_dir / f"{new_token()}.upload"

    try:
        size = await save_upload_limited(upload, source, max_bytes=max_bytes)
        width, height, detected_format = validate_image_upload(source, original_filename)
        return (
            UploadInfo(
                filename=original_filename,
                size=size,
                width=width,
                height=height,
                format=detected_format,
            ),
            source,
        )
    except Exception:
        remove_file(source)
        raise
    finally:
        await upload.close()


def validate_image_file(path: Path, original_filename: str, size: int) -> UploadInfo:
    """对已经落盘的文件做完整校验（扩展名 / 文件头 / 真正解码）。

    与 :func:`intake_upload` 的区别：这里不再重复落盘。
    批量任务在**上传阶段**只做便宜的扩展名检查并把文件存下来，
    真正的解码留到队列里做 —— 慢操作不占着上传请求，
    坏图也会作为「这一个文件失败」被记录，而不是让整批上传失败。
    """
    width, height, detected_format = validate_image_upload(path, original_filename)
    return UploadInfo(
        filename=original_filename,
        size=size,
        width=width,
        height=height,
        format=detected_format,
    )


class _PoolCall:
    """提交给线程池的一次调用，外加一个「真的开工了没有」的标记。

    这个标记存在的唯一理由：超时可能发生在**两个完全不同的时刻** ——
    活儿干了一半没干完，和活儿根本还没轮到。前者是「这个文件太大」，
    后者是「服务器同时在干的活太多」，对用户该说的话不一样
    （一个要拆小文件，另一个只要等一会儿）。没有这个标记就只能笼统报超时。
    """

    __slots__ = ("_fn", "_args", "_kwargs", "started")

    def __init__(self, fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        #: 线程**真的开始执行**时才置位。写在 worker 线程、读在事件循环线程；
        #: 读到的若是过期的 False，最坏情况也只是退回原来那句「处理超时」，
        #: 不会把慢文件误报成服务器忙。
        self.started = False

    def __call__(self) -> Any:
        self.started = True
        return self._fn(*self._args, **self._kwargs)


async def run_in_pool(
    fn: Callable[..., T],
    *args: Any,
    timeout: int | None = None,
    timeout_message: str | None = None,
    **kwargs: Any,
) -> T:
    """在线程池里执行 CPU 密集型任务，并施加超时限制。"""
    limit = settings.PROCESS_TIMEOUT_SECONDS if timeout is None else timeout
    call = _PoolCall(fn, args, kwargs)
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_executor, call),
            timeout=limit,
        )
    except asyncio.TimeoutError as exc:
        if not call.started:
            # 线程池连开工都没轮到它。**这不是这个文件的问题**：
            # 它一个字节都还没被处理过。报「处理超时」会让用户去拆分一个
            # 本来完全正常的文件，而真正该做的是等一会儿再试。
            raise ServerBusyError("服务器正忙，请稍后重试") from exc
        message = timeout_message or f"处理超时（超过 {limit} 秒），请换一张更小的图片重试"
        raise ProcessingTimeoutError(message) from exc


def media_type_for(fmt: str) -> str:
    """图片格式对应的 MIME 类型。

    这份表与 ``conversion.registry.MEDIA_TYPE_BY_TARGET`` 是同一个答案的
    两种写法（一个按编码层内部名，一个按线上词汇），由
    ``tests/test_conversion_registry.py`` 逐项对账 —— 漏一格的后果很具体：
    下载响应头变成 ``application/octet-stream``，浏览器把它当未知二进制，
    预览与「在新标签打开」都会失效。
    """
    return {
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "bmp": "image/bmp",
        "gif": "image/gif",
        "tiff": "image/tiff",
        "ico": "image/x-icon",
        # 内部名是 ``heif``（Pillow 报的容器名），MIME 用 ``image/heic`` ——
        # 那是手机与浏览器实际认的那个。与 ``registry.MEDIA_TYPE_BY_TARGET``
        # 的对账由 ``test_conversion_registry`` 逐项钉住。
        "heif": "image/heic",
        "zip": "application/zip",
    }.get(fmt, "application/octet-stream")


def resolve_output_path(job: Job) -> Path:
    """下载前确认结果文件仍然存在。"""
    if not job.path.is_file():
        job_store.discard(job.job_id)
        raise ProcessingError("结果文件已过期或被清理，请重新处理")
    return job.path


def resolve_item_path(job: Job, index: int) -> tuple[Path, str, str]:
    """取出批量任务中某一张结果文件，用于预览。

    只允许访问登记在本任务里的文件，且再次确认路径确实位于任务目录内，
    避免任何形式的路径穿越。
    """
    items = job.extra.get("items") or []
    for item in items:
        if item.get("index") != index:
            continue
        path = Path(item["path"]).resolve()
        if path.parent != job.directory.resolve() or not path.is_file():
            break
        return path, item.get("filename", "result"), item.get("media_type", "application/octet-stream")
    raise ProcessingError("结果文件已过期或被清理，请重新处理")
