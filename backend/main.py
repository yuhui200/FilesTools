"""FileTools 后端入口。

启动：
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from config import settings
from office.txt_to_pdf import available_fonts
from routers import (
    compress,
    conversion,
    convert,
    download,
    metadata,
    office,
    pdf,
    resize,
    system,
    tasks,
)
from services.doc_service import DOC_TARGET_PRESETS
from services.intake import shutdown_executor
from services.job_store import cleanup_worker, job_store
from services.ocr_service import is_available as ocr_available
from services.office_converter import is_available, shutdown_libreoffice
from services.pdf_to_docx import docx_available
from services.progress import progress_store
from services.queue_service import task_queue
from services.worker_pool import pool_sizes, timeout_table
from utils.errors import STATUS_BY_CODE, ErrorCode, FileToolsError
from utils.files import sweep_orphan_dirs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("filetools")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_worker.start()
    # 过期任务记录也交给清理线程（§14）：任务队列依赖 job_store，
    # 由这里注册可以避免两个模块互相 import
    cleanup_worker.add_sweeper("任务记录", task_queue.purge_expired)
    # PDF → Word 的阶段进度（第六阶段 A）。它是内存表，不注册清理的话
    # 进程活多久就漏多久 —— 转换到一半用户关掉页面，那条记录没人会来读。
    cleanup_worker.add_sweeper("转换进度", progress_store.purge_expired)
    # 进程被强杀时留下的无人认领目录（登记表在内存里，重启后没人管它们）
    cleanup_worker.add_sweeper(
        "临时目录",
        lambda: sweep_orphan_dirs(job_store.live_dirs() | task_queue.live_dirs()),
    )
    # 批量任务的 worker 协程（第四阶段）：随服务一起启停
    await task_queue.start()
    logger.info(
        "%s v%s 已启动（上传上限 %d MB，单批最多 %d 个文件，并发 %d）",
        settings.APP_NAME,
        settings.APP_VERSION,
        settings.MAX_UPLOAD_BYTES // (1024 * 1024),
        settings.MAX_BATCH_FILES,
        settings.MAX_WORKERS,
    )
    try:
        yield
    finally:
        # 顺序有讲究：先停队列（它会删掉没处理完的临时目录），
        # 再清结果文件，最后释放线程池
        await task_queue.stop()
        cleanup_worker.stop()
        # 关闭时清空所有未下载的临时文件
        job_store.purge_all()
        shutdown_libreoffice()
        shutdown_executor()
        logger.info("已清理临时文件并释放线程池")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="在线文件处理工具后端：图片压缩、格式转换、尺寸调整、PDF 处理。",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


# ----------------------------------------------------------------------
# 中间件：请求体大小预检 + 基础安全响应头
# ----------------------------------------------------------------------

# multipart 边界和表单字段的额外开销，留 1 MB 余量
_SIZE_SLACK = 1024 * 1024

# 批量接口：单次请求可能带多个文件，上限放宽到整批额度
_BATCH_PATHS = (
    "/api/image/compress",
    "/api/image/convert",
    "/api/image/resize",
    "/api/pdf/from-images",
    "/api/pdf/merge",
    # 统一转换中心（第七阶段）：一次可以传很多个文件，
    # 不列在这里的话会被按单文件上限预检请求体，整批直接 413。
    "/api/conversion/tasks",
)


def _request_body_limit(path: str) -> int:
    """按接口返回请求体上限。"""
    if path in _BATCH_PATHS:
        return settings.MAX_BATCH_TOTAL_BYTES
    return settings.MAX_UPLOAD_BYTES


@app.middleware("http")
async def guard_requests(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit():
        limit = _request_body_limit(request.url.path)
        if int(content_length) > limit + _SIZE_SLACK:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": ErrorCode.FILE_TOO_LARGE,
                        "message": "文件过大，请上传更小的文件。",
                    }
                },
            )

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# ----------------------------------------------------------------------
# 异常处理：统一成 {error: {code, message}}，不泄露内部细节
# ----------------------------------------------------------------------

@app.exception_handler(FileToolsError)
async def handle_business_error(request: Request, exc: FileToolsError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": ErrorCode.INVALID_REQUEST,
                "message": "请求参数不完整或格式不正确",
            }
        },
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # 详细堆栈只写日志，不回传给前端
    logger.exception("未处理的异常：%s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": ErrorCode.SERVER_ERROR,
                "message": "服务器处理失败，请稍后重试",
            }
        },
    )


# ----------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------

app.include_router(compress.router)
app.include_router(convert.router)
app.include_router(metadata.router)
app.include_router(resize.router)
app.include_router(pdf.router)
app.include_router(office.router)
app.include_router(download.router)
app.include_router(tasks.router)
app.include_router(conversion.router)

# 运行状态接口（第八阶段）。**默认开、可以关**：关掉时这里干脆不注册，
# 于是路径不存在（404）。这两个接口只返回槽位与聚合数字，没有文件名、
# 没有任务号、没有路径 —— 但仍然给运维留一个能整个关掉的开关。
if settings.SYSTEM_API_ENABLED:
    app.include_router(system.router)


@app.get("/api/health", tags=["meta"], summary="健康检查")
async def health() -> dict:
    return {"status": "ok", "version": settings.APP_VERSION}


@app.get("/api/config", tags=["meta"], summary="前端可用的限制配置")
async def public_config() -> dict:
    """把服务端限制暴露给前端，避免两边写死不一致。"""
    return {
        "max_upload_bytes": settings.MAX_UPLOAD_BYTES,
        "max_batch_files": settings.MAX_BATCH_FILES,
        "max_batch_total_bytes": settings.MAX_BATCH_TOTAL_BYTES,
        "allowed_image_extensions": sorted(settings.ALLOWED_IMAGE_EXTENSIONS),
        "quality_presets": list(settings.QUALITY_PRESETS),
        "default_quality_value": settings.DEFAULT_QUALITY_VALUE,
        "max_image_edge": settings.MAX_IMAGE_EDGE,
        "output_formats": ["jpg", "png", "webp"],
        "process_timeout_seconds": settings.PROCESS_TIMEOUT_SECONDS,
        # PDF（第三阶段）
        "allowed_pdf_extensions": sorted(settings.ALLOWED_PDF_EXTENSIONS),
        "max_pdf_pages": settings.MAX_PDF_PAGES,
        "pdf_thumbnail_max_pages": settings.PDF_THUMBNAIL_MAX_PAGES,
        "pdf_thumbnail_width": settings.PDF_THUMBNAIL_WIDTH,
        "pdf_render_max_dpi": settings.PDF_RENDER_MAX_DPI,
        "pdf_render_default_dpi": settings.PDF_RENDER_DEFAULT_DPI,
        "pdf_export_max_pages": settings.PDF_EXPORT_MAX_PAGES,
        "pdf_page_sizes": sorted(settings.PDF_PAGE_SIZES),
        "pdf_margins": list(settings.PDF_MARGINS),
        "pdf_compress_levels": list(settings.PDF_COMPRESS_LEVELS),
        "default_pdf_compress_level": settings.DEFAULT_PDF_COMPRESS_LEVEL,
        "pdf_input_ttl_seconds": settings.PDF_INPUT_TTL_SECONDS,
        # 文档转换（第五阶段）
        "allowed_word_extensions": sorted(settings.ALLOWED_WORD_EXTENSIONS),
        "allowed_excel_extensions": sorted(settings.ALLOWED_EXCEL_EXTENSIONS),
        "allowed_powerpoint_extensions": sorted(settings.ALLOWED_POWERPOINT_EXTENSIONS),
        "allowed_text_extensions": sorted(settings.ALLOWED_TEXT_EXTENSIONS),
        "doc_timeout_seconds": settings.OFFICE_CONVERT_TIMEOUT_SECONDS,
        # 缺组件时前端要**在上传之前**就提示，而不是让用户传完 50 MB 才报错
        "doc_conversion_available": is_available(),
        "doc_target_presets": list(DOC_TARGET_PRESETS),
        # TXT 排版选项。字体是**运行时探测本机**得到的，不是写死的列表 ——
        # 服务器上没装的字体不该出现在选项里（PyMuPDF 的内置中文码全部指向
        # 同一个 Droid Sans Fallback，拿它冒充宋体黑体就是个假控件）。
        "txt_fonts": [
            {"value": item.key, "label": item.label} for item in available_fonts()
        ],
        "txt_font_size": {
            "min": settings.TXT_MIN_FONT_SIZE,
            "max": settings.TXT_MAX_FONT_SIZE,
            "default": settings.TXT_DEFAULT_FONT_SIZE,
        },
        "txt_page_sizes": list(settings.TXT_PAGE_SIZES),
        "txt_orientations": list(settings.TXT_ORIENTATIONS),
        # PDF → Word（第六阶段 A）
        #
        # ``pdf_to_word_available`` **只看 python-docx，与 LibreOffice 无关**：
        # PDF → Word 全程不启动 soffice，用 LibreOffice 的可用性去挡这个按钮
        # 会在没装 LibreOffice 的服务器上白白禁掉一个能用的功能。
        "pdf_to_word_available": docx_available(),
        "ocr_available": ocr_available(),
        "ocr_languages": list(settings.OCR_LANGUAGES),
        "pdf_to_word_max_ocr_pages": settings.PDF_TO_WORD_MAX_OCR_PAGES,
        "pdf_to_word_timeout_seconds": settings.PDF_TO_WORD_TIMEOUT_SECONDS,
        # 任务队列（第四阶段）
        #
        # ``queue_workers`` 的语义**一个字都没变**：它仍然是「一个普通批次
        # 同时最多处理几个文件」。第八阶段把它落成了 default 池的大小 ——
        # 既有的四个批量工具与未分类批次全部走那个池。
        "queue_workers": settings.QUEUE_WORKERS,
        # 按资源分池（第八阶段）。**纯追加**：既有字段一个没改、一个没删。
        "worker_pools": pool_sizes(),
        "pool_timeouts": {
            row["pool"]: row["effective_timeout_seconds"] for row in timeout_table()
        },
        "system_api_enabled": settings.SYSTEM_API_ENABLED,
        "task_ttl_seconds": settings.TASK_TTL_SECONDS,
        "file_ttl_seconds": settings.JOB_TTL_SECONDS,
        "batch_timeout_seconds": settings.BATCH_TIMEOUT_SECONDS,
        # 前端按错误码给出中文提示（§13），这里列出服务端可能返回的全部错误码，
        # 供前端自检「有没有漏配某个码的文案」
        "error_codes": sorted(STATUS_BY_CODE),
    }


# ----------------------------------------------------------------------
# 生产模式下顺带托管构建好的前端（frontend/dist 存在时才会挂载）
# ----------------------------------------------------------------------

class SPAStaticFiles(StaticFiles):
    """静态资源 + 单页应用回退：找不到的路径统一返回 index.html。

    这样 /image/compress 之类的前端路由在刷新时也能正常打开。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


_frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount("/", SPAStaticFiles(directory=str(_frontend_dist), html=True), name="frontend")
    logger.info("已挂载前端静态资源：%s", _frontend_dist)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
