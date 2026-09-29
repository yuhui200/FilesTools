"""PDF 批量任务：PDF → 图片（第四阶段 §2）。

上传仍然走第三阶段的 ``/api/pdf/upload``：用户一次上传、拿到 input_id、
看到页面缩略图，再决定导出哪些页。任务本身只接收 input_id 列表 ——
文件已经在服务器上了，没必要为了「批量」让浏览器再传一遍。

**保持第三阶段的行为不变**：只处理一份 PDF 时，结果文件名与打包名
和第三阶段完全一致（``page-01.jpg`` / ``pdf_pages.zip``）；
处理多份时才给每份的页面加上来源文件名前缀，否则 ZIP 里的 page-01.jpg
会互相覆盖。
"""

from __future__ import annotations

from pathlib import Path

from config import settings
from pdf.pdf_to_image import RenderOptions, media_type_for
from routers.pdf_schemas import build_pdf_response
from services.batch_service import unique_name
from services.intake import run_in_pool
from services.job_store import Job
from services.pdf_service import PdfOutput, load_input, register_pdf_result
from services.pdf_tools import render_pages_into
from services.queue_service import (
    STATE_DONE,
    STATE_FAILED,
    TaskGroup,
    TaskItem,
    task_queue,
)
from utils.errors import FileToolsError, JobNotFoundError, ValidationError
from utils.files import create_temp_dir, new_token, sanitize_stem

__all__ = ["PDF_TOOL_LABEL", "submit_pdf_to_images"]

PDF_TOOL_LABEL = "PDF 转图片"

ARCHIVE_STEM = "pdf_pages"

# 逐页渲染 + 编码比单纯读取慢，给足时间（100 页 × 300 dpi 是真实的重活）
PDF_RENDER_TIMEOUT_SECONDS = 300


async def submit_pdf_to_images(
    *,
    input_ids: list[str],
    options: RenderOptions,
    pages_raw: str | None,
) -> TaskGroup:
    """把若干份已上传的 PDF 逐份导出成图片。"""
    if not input_ids:
        raise ValidationError("请至少上传一个 PDF 文件")
    if len(input_ids) > settings.MAX_BATCH_FILES:
        raise ValidationError(f"一次最多处理 {settings.MAX_BATCH_FILES} 个文件")

    work_dir = create_temp_dir()
    group = TaskGroup(
        group_id=new_token(),
        tool="pdf.to-images",
        label=PDF_TOOL_LABEL,
        directory=work_dir,
        items=_build_items(input_ids),
        handler=_make_handler(options, pages_raw),
        finalizer=_make_finalizer(options, single=len(input_ids) == 1),
    )
    return await task_queue.submit(group)


def _build_items(input_ids: list[str]) -> list[TaskItem]:
    """把 input_id 变成任务项。

    input_id 失效（用户放太久、文件已被清理）在这里就记成这个文件的失败原因，
    其余文件照常处理 —— 与「单个文件失败不影响整批」保持一致。
    """
    items: list[TaskItem] = []
    for index, input_id in enumerate(input_ids):
        try:
            source = load_input(input_id)
        except FileToolsError as exc:
            items.append(
                TaskItem(
                    index=index,
                    filename=f"第 {index + 1} 份 PDF",
                    size=0,
                    state=STATE_FAILED,
                    error_code=exc.code,
                    error_message=exc.message,
                )
            )
            continue
        items.append(
            TaskItem(
                index=index,
                filename=source.filename,
                size=source.size,
                payload={"input_id": input_id},
            )
        )
    return items


def _make_handler(options: RenderOptions, pages_raw: str | None):
    """生成处理器：一份 PDF 渲染到一个子目录，逐页落盘。"""

    async def handler(item: TaskItem, group: TaskGroup) -> dict:
        source = _source_of(item)

        # 页面文件直接落在任务目录里（文件名本来就是随机 token，多份 PDF 不会撞名）。
        # 不要再套一层子目录：预览接口会校验结果文件必须位于任务目录内，
        # 隔一层就会被当成越权访问。
        written, total_pages = await run_in_pool(
            render_pages_into,
            group.directory,
            source.path,
            pages_raw,
            options,
            timeout=PDF_RENDER_TIMEOUT_SECONDS,
            timeout_message=(
                f"导出图片超时（超过 {PDF_RENDER_TIMEOUT_SECONDS} 秒），"
                "请减少页数或降低清晰度后重试"
            ),
        )

        # 结果路径与文件名只留在服务内部，不进快照（§18：不泄露服务器路径）
        item.payload = {
            "input_id": item.payload["input_id"] if item.payload else None,
            "entries": [
                {"filename": filename, "path": path}
                for filename, path, _size, _clamped in written
            ],
            "pages": len(written),
            "total_pages": total_pages,
            "clamped": any(clamped for _n, _p, _s, clamped in written),
        }
        return {
            "filename": item.filename,
            "pages": len(written),
            "total_pages": total_pages,
        }

    return handler


def _source_of(item: TaskItem) -> Job:
    input_id = (item.payload or {}).get("input_id")
    if not isinstance(input_id, str):  # pragma: no cover - 只有失败的项没有 input_id
        raise JobNotFoundError("文件已过期，请重新上传 PDF")
    return load_input(input_id)


def _make_finalizer(options: RenderOptions, *, single: bool):
    """生成收尾器：汇总各份 PDF 的图片，多于一页时打包。"""

    async def finalize(group: TaskGroup) -> None:
        outputs: list[PdfOutput] = []
        notes: list[str] = []
        clamped = False
        #: 已经用过的**来源前缀**。见下面 ``unique_name`` 那处注释。
        used_prefixes: set[str] = set()

        for item in group.items:
            if item.state != STATE_DONE or not item.payload:
                continue
            prefix = "" if single else f"{unique_name(_stem(item), used_prefixes)}-"
            for entry in item.payload["entries"]:
                outputs.append(
                    PdfOutput(
                        filename=f"{prefix}{entry['filename']}",
                        path=entry["path"],
                        media_type=media_type_for(options.fmt),
                        previewable=True,
                    )
                )
            clamped = clamped or bool(item.payload["clamped"])

        if not outputs:
            # 全部失败：交给队列把整组标记失败并返回第一个原因
            return

        if clamped:
            notes.append("部分页面尺寸过大，为保证服务器稳定已自动降低渲染精度。")

        first = next(item for item in group.items if item.state == STATE_DONE)
        payload = first.payload or {}

        def _package(work_dir: Path):
            return register_pdf_result(
                work_dir,
                outputs,
                archive_stem=ARCHIVE_STEM,
                # 只处理一份时，原始大小与页数就是这份 PDF 的，
                # 与第三阶段的响应保持完全一致；多份时这些数字没有意义。
                original_size=first.size if single else None,
                original_pages=payload.get("total_pages") if single else None,
                notes=notes,
            )

        # 打包同样在线程池里做（可能是上百张图的 ZIP）
        result = await run_in_pool(
            _package,
            group.directory,
            timeout=settings.BATCH_TIMEOUT_SECONDS,
            timeout_message=(
                f"打包结果超时（超过 {settings.BATCH_TIMEOUT_SECONDS} 秒），"
                "请减少页数后重试"
            ),
        )

        group.job_id = result.job.job_id
        group.download_url = f"/api/download/{result.job.job_id}"
        group.summary = build_pdf_response(result).model_dump()

    return finalize


def _stem(item: TaskItem) -> str:
    """多份 PDF 时用来给结果文件加前缀，避免 ZIP 内重名。

    **前缀本身也要去重**（第十阶段 C §十二）。两份来源文件同名是常事
    —— 用户从两个文件夹各拖一份 ``报告.pdf`` 进来 —— 那时两个前缀都是
    ``报告-``，页面名也都是 ``page-1.png``，于是 ZIP 里出现两个同名的
    成员：``zipfile`` 不报错，解压时**后写的那份覆盖前一份**，用户少拿到
    一份还不知道。

    这里复用 ``services.batch_service.unique_name``，也就是图片那条路
    早就在用的那条规则（``photo.jpg`` → ``photo-2.jpg``）—— 去重规则
    只有一份，两条路不会各自演化出不同的改名方式。
    """
    return sanitize_stem(item.filename, fallback=f"pdf-{item.index + 1}")
