"""PDF 功能的公共流程。

六个 PDF 功能长得不一样，但有三件事完全一致，抽在这里只实现一次：

1. **接收上传的 PDF**：落盘 → 三层校验 → 登记成「输入文件」。
   输入文件有独立的、更长的存活时间，因为用户往往会拿同一份 PDF
   连着做几个操作（先删页再压缩），不能每步都重传一遍。
2. **登记结果**：结果多于一个文件时自动打包 ZIP（单文件不套 ZIP）。
3. **渲染缩略图**：页面选择器要用，统一走 pdf.loader 的参数与像素上限。

所有 PyMuPDF 调用都通过 ``run_in_pool`` 放进线程池，
绝不在事件循环里直接跑，否则一个几十页的 PDF 就能卡住整个服务。
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import UploadFile

from config import settings
from pdf.loader import open_pdf, render_thumbnail, validate_pdf_upload
from services.intake import run_in_pool
from services.job_store import KIND_INPUT, KIND_RESULT, Job, job_store
from utils.errors import (
    JobNotFoundError,
    PdfPageNotFoundError,
    ProcessingError,
    ValidationError,
)
from utils.files import new_token, remove_dir, remove_file, sanitize_stem, save_upload_limited

__all__ = [
    "PdfInput",
    "PdfOutput",
    "PdfResult",
    "ReceivedPdf",
    "DOCX_MEDIA_TYPE",
    "PDF_MEDIA_TYPE",
    "ZIP_MEDIA_TYPE",
    "intake_pdf",
    "receive_pdf",
    "load_input",
    "input_output_stem",
    "render_input_thumbnail",
    "register_pdf_result",
    "build_archive",
    "discard_dir",
]

PDF_MEDIA_TYPE = "application/pdf"
ZIP_MEDIA_TYPE = "application/zip"
#: PDF → Word 的产物类型（第六阶段 A）。下载接口按 job.media_type 原样响应，
#: 所以这里给对了，浏览器就会存成 .docx 而不是猜成别的。
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(slots=True)
class PdfInput:
    """一份已接收并校验通过的 PDF。"""

    input_id: str
    filename: str
    size: int
    page_count: int


@dataclass(slots=True)
class ReceivedPdf:
    """一份刚落盘、校验通过、但还没有登记的 PDF。"""

    filename: str
    size: int
    page_count: int
    path: Path


@dataclass(slots=True)
class PdfOutput:
    """结果中的一个文件。"""

    filename: str          # 展示 / 打包时用的文件名
    path: Path             # 磁盘位置
    media_type: str = PDF_MEDIA_TYPE
    page_count: int | None = None
    previewable: bool = False  # 图片类结果可以走 /api/preview 预览

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:  # pragma: no cover - 结果文件刚写完就读不到属于磁盘异常
            return 0


@dataclass(slots=True)
class PdfResult:
    """一次 PDF 处理的完整结果。"""

    job: Job
    outputs: list[PdfOutput]
    archived: bool
    archive_filename: str | None = None
    page_count: int | None = None       # 主结果的页数
    original_size: int | None = None
    original_pages: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def saved_bytes(self) -> int | None:
        if self.original_size is None:
            return None
        return self.original_size - self.job.size

    @property
    def saved_percent(self) -> float | None:
        if self.original_size is None or self.original_size <= 0:
            return None
        return round((self.original_size - self.job.size) / self.original_size * 100, 1)


# ----------------------------------------------------------------------
# 输入文件
# ----------------------------------------------------------------------

async def receive_pdf(
    upload: UploadFile,
    work_dir: Path,
    *,
    fallback_name: str,
) -> ReceivedPdf:
    """接收一份上传的 PDF 并完成校验，**不登记**，只落盘。

    合并这类「一次性用完就丢」的场景用它：来源文件不需要单独的令牌，
    登记了反而会在结果下载后被一起清理掉，留下一堆指向空目录的记录。
    """
    original_filename = upload.filename or fallback_name
    source = work_dir / f"{new_token()}.upload"

    try:
        size = await save_upload_limited(upload, source)
        page_count = validate_pdf_upload(source, original_filename)
    except Exception:
        remove_file(source)
        raise
    finally:
        await upload.close()

    return ReceivedPdf(
        filename=original_filename, size=size, page_count=page_count, path=source
    )


async def intake_pdf(upload: UploadFile, work_dir: Path, *, fallback_name: str) -> PdfInput:
    """接收一份上传的 PDF，校验后登记为输入文件，返回它的信息。

    登记时用的是随机 token，不是文件名 —— 文件名只用来生成展示名和结果名。
    输入文件有独立且更长的存活时间，因为用户往往拿同一份 PDF 连着做几个操作。
    """
    received = await receive_pdf(upload, work_dir, fallback_name=fallback_name)

    input_id = new_token()
    job_store.add(
        Job(
            job_id=input_id,
            directory=work_dir,
            path=received.path,
            filename=received.filename,
            media_type=PDF_MEDIA_TYPE,
            size=received.size,
            kind=KIND_INPUT,
            ttl=settings.PDF_INPUT_TTL_SECONDS,
        )
    )
    return PdfInput(
        input_id=input_id,
        filename=received.filename,
        size=received.size,
        page_count=received.page_count,
    )


def load_input(input_id: str) -> Job:
    """按 input_id 取出已上传的 PDF，并确认文件还在。

    只接受输入类型的任务：结果令牌不能拿来当输入用。
    """
    job = job_store.get(input_id)
    if job is None or job.kind != KIND_INPUT:
        raise JobNotFoundError("文件已过期，请重新上传 PDF")

    if not job.path.is_file():
        job_store.discard(input_id)
        raise JobNotFoundError("文件已过期，请重新上传 PDF")
    return job


def input_output_stem(job: Job) -> str:
    """用原始文件名生成结果主名，例如 report.pdf -> report。"""
    return sanitize_stem(job.filename, fallback="document")


def render_input_thumbnail(input_id: str, page: int) -> bytes:
    """同步渲染某页缩略图（调用方负责放进线程池）。"""
    job = load_input(input_id)
    if page < 0:
        raise ValidationError("页码必须从 0 开始")

    with open_pdf(job.path) as doc:
        if page >= doc.page_count:
            raise PdfPageNotFoundError(f"第 {page + 1} 页不存在，该 PDF 共 {doc.page_count} 页")
        return render_thumbnail(doc, page)


# ----------------------------------------------------------------------
# 结果登记
# ----------------------------------------------------------------------

def register_pdf_result(
    work_dir: Path,
    outputs: list[PdfOutput],
    *,
    archive_stem: str,
    original_size: int | None = None,
    original_pages: int | None = None,
    page_count: int | None = None,
    notes: list[str] | None = None,
) -> PdfResult:
    """落盘结果并登记下载令牌。

    只有一个结果文件时直接下载它；多于一个时打包 ZIP ——
    和图片批量处理保持一致的取舍：单文件套压缩包只会让人多解压一次。
    """
    if not outputs:
        raise ValidationError("没有生成任何结果文件，请检查参数后重试")

    if len(outputs) == 1:
        only = outputs[0]
        target = only.path
        download_name = only.filename
        media_type = only.media_type
        archived = False
        archive_filename = None
    else:
        archive_path = work_dir / f"{new_token()}.zip"
        archive_filename = f"{archive_stem}.zip"
        build_archive(archive_path, outputs)
        target = archive_path
        download_name = archive_filename
        media_type = ZIP_MEDIA_TYPE
        archived = True

    try:
        size = target.stat().st_size
    except OSError as exc:  # pragma: no cover - 磁盘异常
        raise ProcessingError("生成结果文件失败，请重试") from exc

    job_id = new_token()
    job = job_store.add(
        Job(
            job_id=job_id,
            directory=work_dir,
            path=target,
            filename=download_name,
            media_type=media_type,
            size=size,
            kind=KIND_RESULT,
            extra={
                "items": [
                    {
                        "index": index,
                        "path": str(output.path),
                        "filename": output.filename,
                        "media_type": output.media_type,
                        "page_count": output.page_count,
                        "previewable": output.previewable,
                    }
                    for index, output in enumerate(outputs)
                ]
            },
        )
    )

    return PdfResult(
        job=job,
        outputs=outputs,
        archived=archived,
        archive_filename=archive_filename,
        page_count=page_count,
        original_size=original_size,
        original_pages=original_pages,
        notes=notes or [],
    )


def build_archive(dest: Path, outputs: list[PdfOutput]) -> None:
    """把多个结果打包。

    ZIP_STORED：PDF 和图片本身已经压过了，再 deflate 收益接近于零，
    只会白白拖慢一次几十页的导出。
    """
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED) as archive:
        for output in outputs:
            archive.write(output.path, arcname=output.filename)


def discard_dir(work_dir: Path) -> None:
    """失败时清掉本次的临时目录。"""
    remove_dir(work_dir)
