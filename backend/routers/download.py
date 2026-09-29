"""结果文件的下载与预览接口。

下载是「一次性」的：响应发送完毕后立即删除临时文件并注销令牌。
预览是「只读」的：不消耗令牌，否则用户看完预览就没法下载了。

令牌是 32 位随机十六进制串，且服务端只按令牌查登记表，
不接受任何路径参数，因此用户无法通过它访问服务器上的任意文件。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from conversion.registry import INLINE_PREVIEW_MEDIA_TYPES
from services.intake import resolve_item_path, resolve_output_path
from services.job_store import KIND_RESULT, job_store
from utils.errors import JobNotFoundError, ProcessingError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["download"])


@router.get("/download/{job_id}", summary="下载处理结果")
async def download_result(job_id: str) -> FileResponse:
    job = job_store.get(job_id)
    if job is None or job.kind != KIND_RESULT:
        raise JobNotFoundError("下载链接无效或已过期，请重新处理文件")

    path = resolve_output_path(job)

    def _cleanup() -> None:
        try:
            job_store.discard(job_id)
        except Exception:  # pragma: no cover - 清理失败不影响已发送的响应
            logger.exception("删除临时文件失败：%s", job_id)

    return FileResponse(
        path,
        filename=job.filename,
        media_type=job.media_type,
        background=BackgroundTask(_cleanup),
    )


@router.get("/preview/{job_id}/{index}", summary="预览处理后的图片")
async def preview_result(job_id: str, index: int) -> FileResponse:
    """预览批量结果中的某一张图片。

    预览**不会**消耗下载令牌，用户可以反复查看后再决定是否下载。
    """
    job = job_store.get(job_id)
    if job is None or job.kind != KIND_RESULT:
        raise JobNotFoundError("预览链接无效或已过期，请重新处理文件")

    path, _filename, media_type = resolve_item_path(job, index)
    # 放行的判据是**这张结果到底能不能在浏览器里内联显示**，不是
    # 「MIME 像不像图片」（第十阶段 C §五）。
    #
    # 旧写法是 ``media_type.startswith("image/")``，它把 TIFF 与 HEIC 也放进来了
    # —— 那两个 MIME 确实是 ``image/*``，可没有任何主流浏览器渲染得了它们。
    # 结果是结果卡上挂着一个必定显示成破图的缩略图：接口返回 200，用户看到
    # 一个碎图标。放行清单现在取自 ``registry.INLINE_PREVIEW_MEDIA_TYPES``，
    # 与统一转换中心「发不发 preview_url」用的是**同一张表**，两边不可能一个
    # 说能预览、另一个说不能。
    if media_type not in INLINE_PREVIEW_MEDIA_TYPES:
        raise ProcessingError("该结果不支持预览")

    # 不设置 filename，让浏览器按 inline 渲染而不是触发下载
    return FileResponse(path, media_type=media_type)
