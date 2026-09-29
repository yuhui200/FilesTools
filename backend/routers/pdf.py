"""PDF 工具接口。

按第三阶段的开发顺序逐个补齐：

1. 图片 → PDF    POST /api/pdf/from-images
2. PDF → 图片    POST /api/pdf/to-images
3. PDF 合并      POST /api/pdf/merge
4. PDF 拆分      POST /api/pdf/split
5. PDF 页面删除  POST /api/pdf/delete-pages
6. PDF 页面提取  POST /api/pdf/extract-pages
7. PDF 压缩      POST /api/pdf/compress

另外两个辅助接口：
- POST /api/pdf/upload                    上传一份 PDF，拿到 input_id
- GET  /api/pdf/input/{input_id}/thumb/{page}  页面缩略图（不消耗下载令牌）
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import Response

from routers.pdf_params import (
    parse_compress_level,
    parse_layout,
    parse_page_range_text,
    parse_render_options,
    parse_required_page_range,
    parse_split_mode,
    parse_split_value,
    parse_target_size,
)
from routers.pdf_schemas import (
    PdfInputResponse,
    PdfResultResponse,
    build_input_response,
    build_pdf_response,
)
from routers.tasks import TaskCreatedResponse, build_task_response
from services.intake import run_in_pool
from services.pdf_service import intake_pdf, render_input_thumbnail
from services.pdf_tools import (
    compress_pdf_input,
    delete_pdf_pages,
    extract_pdf_pages,
    images_to_pdf,
    merge_pdf_uploads,
    split_pdf_input,
)
from tasks.pdf_tasks import submit_pdf_to_images
from utils.errors import ValidationError
from utils.files import create_temp_dir, remove_dir

router = APIRouter(prefix="/api/pdf", tags=["pdf"])

# 缩略图渲染很快，但仍然放进线程池，且给一个短超时
THUMBNAIL_TIMEOUT_SECONDS = 20


@router.post("/upload", response_model=PdfInputResponse, summary="上传 PDF")
async def upload_pdf_endpoint(
    file: UploadFile = File(..., description="待处理的 PDF 文件"),
) -> PdfInputResponse:
    """接收一份 PDF 并校验，返回后续操作要用的 input_id。

    上传一次可以连续做多个操作（先看缩略图、再删页、再压缩），
    不必每一步都重传文件。
    """
    work_dir = create_temp_dir()
    try:
        info = await intake_pdf(file, work_dir, fallback_name="document.pdf")
    except BaseException:
        remove_dir(work_dir)
        raise
    return build_input_response(info)


@router.get("/input/{input_id}/thumb/{page}", summary="PDF 页面缩略图")
async def input_thumbnail_endpoint(input_id: str, page: int) -> Response:
    """渲染指定页的缩略图（PNG）。

    不消耗下载令牌，可以反复请求 —— 页面选择器每翻一页都会调用它。
    """
    data = await run_in_pool(
        render_input_thumbnail,
        input_id,
        page,
        timeout=THUMBNAIL_TIMEOUT_SECONDS,
        timeout_message="渲染缩略图超时，请稍后重试",
    )
    return Response(content=data, media_type="image/png")


@router.post("/from-images", response_model=PdfResultResponse, summary="图片转 PDF")
async def from_images_endpoint(
    files: list[UploadFile] = File(..., description="图片，顺序即页面顺序"),
    page_size: str = Form("auto", description="页面大小：auto / a4 / a5 / letter / custom"),
    orientation: str = Form("auto", description="页面方向：auto / portrait / landscape"),
    fit: str = Form("contain", description="适应方式：contain 保持比例 / fill 填充页面"),
    margin: str = Form("none", description="页边距：none / small / medium / large"),
    custom_width_mm: str | None = Form(None, description="自定义页面宽度（毫米）"),
    custom_height_mm: str | None = Form(None, description="自定义页面高度（毫米）"),
) -> PdfResultResponse:
    """把多张图片合并成一份 PDF，一张图片一页。"""
    if not files:
        raise ValidationError("请至少上传一张图片")

    layout = parse_layout(
        page_size=page_size,
        orientation=orientation,
        fit=fit,
        margin=margin,
        custom_width_mm=custom_width_mm,
        custom_height_mm=custom_height_mm,
    )
    result = await images_to_pdf(files, layout)
    return build_pdf_response(result)


@router.post(
    "/to-images",
    response_model=TaskCreatedResponse,
    status_code=202,
    summary="PDF 转图片（可批量）",
)
async def to_images_endpoint(
    input_ids: list[str] = Form(..., description="上传 PDF 时拿到的 input_id，可多选"),
    target_format: str = Form("png", description="导出格式：jpg / png / webp"),
    pages: str | None = Form(None, description="页面范围：all / 1-3 / 1,3,5 / 2-6"),
    quality: str | None = Form(None, description="图片质量 1-100，仅 JPG / WEBP 有效"),
    resolution: str = Form("high", description="清晰度：standard / high / ultra"),
) -> TaskCreatedResponse:
    """把若干份 PDF 的指定页面导出成图片，提交后台任务并立刻返回任务号。

    页面范围对所有 PDF 生效；每份 PDF 的结果先各自渲染，
    最后合成一个 ZIP（只有一张图时直接给这张图）。
    """
    options = parse_render_options(
        target_format=target_format,
        quality=quality,
        resolution=resolution,
    )
    group = await submit_pdf_to_images(
        input_ids=input_ids,
        options=options,
        pages_raw=parse_page_range_text(pages),
    )
    return build_task_response(group)


@router.post("/merge", response_model=PdfResultResponse, summary="PDF 合并")
async def merge_endpoint(
    files: list[UploadFile] = File(..., description="多个 PDF，顺序即合并后的页序"),
) -> PdfResultResponse:
    """按上传顺序把多个 PDF 合并成一份。"""
    if not files:
        raise ValidationError("请至少上传一个 PDF 文件")

    result = await merge_pdf_uploads(files)
    return build_pdf_response(result)


@router.post("/split", response_model=PdfResultResponse, summary="PDF 拆分")
async def split_endpoint(
    input_id: str = Form(..., description="上传 PDF 时拿到的 input_id"),
    mode: str = Form("every", description="拆分方式：every / ranges / selected"),
    pages: str | None = Form(
        None,
        description="ranges 为每行一个范围；selected 为 1,3,5,8；every 不需要",
    ),
) -> PdfResultResponse:
    """把 PDF 拆成多份：每页一个、按范围分、或只留选中的页。"""
    split_mode = parse_split_mode(mode)
    result = await split_pdf_input(
        input_id,
        mode=split_mode,
        value=parse_split_value(pages, split_mode),
    )
    return build_pdf_response(result)


@router.post("/delete-pages", response_model=PdfResultResponse, summary="PDF 页面删除")
async def delete_pages_endpoint(
    input_id: str = Form(..., description="上传 PDF 时拿到的 input_id"),
    pages: str = Form("", description="要删除的页面，例如 2,4,7-9"),
) -> PdfResultResponse:
    """删掉指定页面，其余页面按原顺序生成一份新 PDF。

    ``pages`` 用带默认值的声明而不是必填：FastAPI 对空的必填字段会直接抛
    422，用户只能看到「请求参数不完整」；交给下面的解析函数才能给出
    「请先选择要删除的页面」这样明确的提示。
    """
    result = await delete_pdf_pages(
        input_id,
        pages_raw=parse_required_page_range(pages, "请先选择要删除的页面"),
    )
    return build_pdf_response(result)


@router.post("/extract-pages", response_model=PdfResultResponse, summary="PDF 页面提取")
async def extract_pages_endpoint(
    input_id: str = Form(..., description="上传 PDF 时拿到的 input_id"),
    pages: str = Form("", description="要提取的页面，例如 1,3,5,8"),
) -> PdfResultResponse:
    """只保留指定页面，合成一份新的 PDF。"""
    result = await extract_pdf_pages(
        input_id,
        pages_raw=parse_required_page_range(pages, "请先选择要提取的页面"),
    )
    return build_pdf_response(result)


@router.post("/compress", response_model=PdfResultResponse, summary="PDF 压缩")
async def compress_endpoint(
    input_id: str = Form(..., description="上传 PDF 时拿到的 input_id"),
    level: str = Form("balanced", description="压缩等级：light / balanced / strong"),
    target: str = Form("none", description="目标大小：none / 5mb / 10mb / 20mb / custom"),
    target_mb: str | None = Form(None, description="自定义目标大小（MB）"),
) -> PdfResultResponse:
    """压缩 PDF：重新编码页面图片，可选压到指定大小以内。"""
    result = await compress_pdf_input(
        input_id,
        level=parse_compress_level(level),
        target_bytes=parse_target_size(target, target_mb),
    )
    return build_pdf_response(result)
