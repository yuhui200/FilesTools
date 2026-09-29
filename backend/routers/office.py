"""文档转换接口（第五、六阶段）。

第五阶段：Word / Excel / PowerPoint / TXT → PDF。每个类别一个接口，
路由只做两件事：解析表单、把活交给 ``services/doc_service.py``。

1. Word         POST /api/office/word-to-pdf
2. Excel        POST /api/office/excel-to-pdf
3. PowerPoint   POST /api/office/powerpoint-to-pdf
4. TXT          POST /api/office/txt-to-pdf

第六阶段 A：反方向的第一块 —— PDF → Word。

5. PDF          POST /api/office/pdf-to-word
6. 进度         GET  /api/office/pdf-to-word/progress/{progress_id}

前四个是做 PDF，第五个是**从 PDF 出来**，方向相反但同属「文档转换」，
所以继续放在这个 router 里，不另起一个。

五个接口一律是**一发式**：上传和转换在同一次请求里完成，
服务器不留用户的原始文档。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile

from config import settings
from office.loader import KIND_EXCEL, KIND_POWERPOINT, KIND_TEXT, KIND_WORD
from office.txt_to_pdf import TxtOptions
from routers.pdf_params import parse_target_size, parse_txt_options
from routers.pdf_schemas import (
    DocumentResultResponse,
    PdfResultResponse,
    PdfToWordProgressResponse,
    build_document_response,
    build_pdf_response,
)
from services.doc_service import convert_document, receive_office
from services.pdf_to_docx import convert_pdf_to_docx
from services.progress import progress_store
from utils.errors import JobNotFoundError
from utils.files import create_temp_dir, remove_dir

router = APIRouter(prefix="/api/office", tags=["office"])

#: 「最大文件大小」的档位说明。
#: 只共享这段文字、不共享 ``Form(...)`` 对象本身 —— 同一个 FieldInfo 实例给两个路由用
#: 不是 FastAPI 的既定用法，省下的那点重复不值得冒这个险。
_TARGET_DESC = "最大文件大小：none / 500kb / 1mb / 2mb / 5mb / 10mb / custom"
_TARGET_MB_DESC = "自定义最大文件大小（MB）"

_FONT_DESC = "TXT 字体，取值来自 /api/config 的 txt_fonts"
_FONT_SIZE_DESC = (
    f"TXT 字号（{settings.TXT_MIN_FONT_SIZE}–{settings.TXT_MAX_FONT_SIZE}）"
)
_PAGE_SIZE_DESC = f"TXT 页面大小：{' / '.join(settings.TXT_PAGE_SIZES)}"
_ORIENTATION_DESC = f"TXT 页面方向：{' / '.join(settings.TXT_ORIENTATIONS)}"


async def _convert_endpoint(
    file: UploadFile,
    kind: str,
    target: str,
    target_mb: str | None,
    txt_options: TxtOptions | None = None,
) -> PdfResultResponse:
    """文档接口共用的流程：建临时目录 → 收文件 → 转换 → 出错清干净。

    「最大文件大小」是**尽力而为**：LibreOffice 不能指定输出大小，
    只能转完再压；达不到时结果里的 notes 会如实写明，不会谎报成功。
    TXT 不用这个参数（排版选项才决定它的产物），传了也会忽略。
    """
    work_dir = create_temp_dir()
    try:
        info, source = await receive_office(file, work_dir, expected_kind=kind)
        result = await convert_document(
            info,
            source,
            work_dir,
            target_bytes=parse_target_size(target, target_mb),
            txt_options=txt_options,
        )
    except BaseException:
        # 连超时抛出的 CancelledError 一起兜住，否则超时的请求会留下垃圾目录
        remove_dir(work_dir)
        raise
    return build_pdf_response(result)


@router.post("/word-to-pdf", response_model=PdfResultResponse, summary="Word 转 PDF")
async def word_to_pdf_endpoint(
    file: UploadFile = File(..., description="Word 文档（.docx / .doc）"),
    target: str = Form("none", description=_TARGET_DESC),
    target_mb: str | None = Form(None, description=_TARGET_MB_DESC),
) -> PdfResultResponse:
    """把 Word 文档转成 PDF，可选压到指定大小以内。"""
    return await _convert_endpoint(file, KIND_WORD, target, target_mb)


@router.post("/excel-to-pdf", response_model=PdfResultResponse, summary="Excel 转 PDF")
async def excel_to_pdf_endpoint(
    file: UploadFile = File(..., description="Excel 表格（.xlsx / .xls）"),
    target: str = Form("none", description=_TARGET_DESC),
    target_mb: str | None = Form(None, description=_TARGET_MB_DESC),
) -> PdfResultResponse:
    """把 Excel 表格转成 PDF，可选压到指定大小以内。

    工作簿里的工作表会依次排进同一份 PDF；隐藏的工作表不会导出，
    结果里的 notes 会如实说明。
    """
    return await _convert_endpoint(file, KIND_EXCEL, target, target_mb)


@router.post(
    "/powerpoint-to-pdf", response_model=PdfResultResponse, summary="PowerPoint 转 PDF"
)
async def powerpoint_to_pdf_endpoint(
    file: UploadFile = File(..., description="PowerPoint 演示文稿（.pptx / .ppt）"),
    target: str = Form("none", description=_TARGET_DESC),
    target_mb: str | None = Form(None, description=_TARGET_MB_DESC),
) -> PdfResultResponse:
    """把 PowerPoint 演示文稿转成 PDF，可选压到指定大小以内。

    一页幻灯片对应 PDF 的一页；隐藏的幻灯片不会导出，
    结果里的 notes 会如实说明。
    """
    return await _convert_endpoint(file, KIND_POWERPOINT, target, target_mb)


@router.post("/txt-to-pdf", response_model=PdfResultResponse, summary="TXT 转 PDF")
async def txt_to_pdf_endpoint(
    file: UploadFile = File(..., description="文本文件（.txt）"),
    font: str | None = Form(None, description=_FONT_DESC),
    font_size: str | None = Form(None, description=_FONT_SIZE_DESC),
    page_size: str | None = Form(None, description=_PAGE_SIZE_DESC),
    orientation: str | None = Form(None, description=_ORIENTATION_DESC),
) -> PdfResultResponse:
    """把纯文本按所选排版排成 PDF。

    这一路**不用 LibreOffice**（它导入 .txt 时没有字号、页面方向这些控制项），
    由 PyMuPDF 直接排版，所以结果里会写明实际用的字体、字号和页面。

    没有 ``target`` 参数：纯文本排出来的 PDF 大小由字号和页面决定，
    压到某个目标大小这件事对它没有意义，前端也不显示这个选项。
    """
    return await _convert_endpoint(
        file,
        KIND_TEXT,
        "none",
        None,
        txt_options=parse_txt_options(
            font=font,
            font_size=font_size,
            page_size=page_size,
            orientation=orientation,
        ),
    )


# ----------------------------------------------------------------------
# 第六阶段 A：PDF → Word
# ----------------------------------------------------------------------


@router.post(
    "/pdf-to-word", response_model=DocumentResultResponse, summary="PDF 转 Word"
)
async def pdf_to_word_endpoint(
    file: UploadFile = File(..., description="PDF 文件（.pdf）"),
    progress_id: str | None = Form(
        None, description="前端生成的任务标识，用于轮询阶段进度；可选"
    ),
) -> DocumentResultResponse:
    """把 PDF 转成 Word（.docx）。

    文字版 PDF 直接提取文字层；扫描版需要 OCR，**逐页判断**而不是整份判断 ——
    封面是图、正文是字的 PDF 不该被整份送去识别。

    转换过程会同步返回，``progress_id`` 只是让前端能显示真实阶段，
    不传也不影响结果。
    """
    work_dir = create_temp_dir()
    try:
        result = await convert_pdf_to_docx(file, work_dir, progress_id=progress_id)
    except BaseException:
        # 连超时抛出的 CancelledError 一起兜住，否则超时的请求会留下垃圾目录
        remove_dir(work_dir)
        raise
    return build_document_response(result)


@router.get(
    "/pdf-to-word/progress/{progress_id}",
    response_model=PdfToWordProgressResponse,
    summary="查询 PDF 转 Word 的阶段进度",
)
async def pdf_to_word_progress_endpoint(progress_id: str) -> PdfToWordProgressResponse:
    """查询一次转换走到哪一步了。

    只返回阶段名和 OCR 的页码 —— 不返回文件名、大小或任何内容，
    所以就算猜到别人的 id，也看不出这是谁的文件。

    id 形状不合法一律当作「不存在」，返回 404 而不是参数错误：
    这不是用户需要修的东西，前端把 404 当成「进度不可用」静默处理。
    """
    state = progress_store.get(progress_id)
    if state is None:
        raise JobNotFoundError("进度信息不存在或已过期")
    return PdfToWordProgressResponse(
        stage=state["stage"],
        page=state.get("page"),
        page_count=state.get("page_count"),
    )
