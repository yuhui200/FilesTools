"""PDF 各功能的业务流程。

每个功能一个函数，结构都是同一套：

    接收并校验 → 放进线程池计算 → 落盘 → 登记下载令牌

路由器只负责解析表单，真正的流程都在这里，方便单独测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import UploadFile

from config import settings
from pdf.compressor import compress_pdf
from pdf.image_to_pdf import ImageSource, PdfLayout, build_pdf_from_images
from pdf.loader import open_pdf
from pdf.merger import MergeSource, merge_pdfs
from pdf.page_editor import EditResult, delete_pages, extract_pages
from pdf.pages import page_labels, parse_page_range
from pdf.pdf_to_image import (
    RenderOptions,
    check_selection,
    extension_for,
    render_page_image,
)
from pdf.splitter import split_pdf
from services.intake import UploadInfo, intake_upload, run_in_pool
from services.pdf_service import (
    PDF_MEDIA_TYPE,
    PdfOutput,
    PdfResult,
    discard_dir,
    input_output_stem,
    load_input,
    receive_pdf,
    register_pdf_result,
)
from utils.errors import FileToolsError, ProcessingError, ValidationError
from utils.files import create_temp_dir, new_token, sanitize_stem

__all__ = [
    "images_to_pdf",
    "render_pages_into",
    "merge_pdf_uploads",
    "split_pdf_input",
    "delete_pdf_pages",
    "extract_pdf_pages",
    "compress_pdf_input",
]

# 图片排版的超时时间比单张图片处理宽一些：20 张大图要逐张解码再嵌入
PDF_BUILD_TIMEOUT_SECONDS = 180

# 合并：读几份 PDF 再重写一遍，比单份处理慢，但远快于渲染
PDF_MERGE_TIMEOUT_SECONDS = 180

# 拆分：每页一个时会有几百次「新建文档 + 抄一页 + 序列化」，
# 单次都很快，但次数多，给的时间比合并宽一些
PDF_SPLIT_TIMEOUT_SECONDS = 240

# 页面删除 / 提取：只重建一次文档，比拆分轻得多
PDF_EDIT_TIMEOUT_SECONDS = 120

# 压缩：可能要在同一个文档上试好几档（每档都重新编码全部图片），
# 大文件最慢，给的时间也最长
PDF_COMPRESS_TIMEOUT_SECONDS = 300


async def images_to_pdf(uploads: list[UploadFile], layout: PdfLayout) -> PdfResult:
    """把一批图片按上传顺序排版成一份 PDF。"""
    if not uploads:
        raise ValidationError("请至少上传一张图片")
    if len(uploads) > settings.MAX_BATCH_FILES:
        raise ValidationError(f"一次最多处理 {settings.MAX_BATCH_FILES} 个文件")

    work_dir = create_temp_dir()
    try:
        images, total_bytes = await _intake_images(uploads, work_dir)

        built = await run_in_pool(
            build_pdf_from_images,
            images,
            layout,
            timeout=PDF_BUILD_TIMEOUT_SECONDS,
            timeout_message=(
                f"生成 PDF 超时（超过 {PDF_BUILD_TIMEOUT_SECONDS} 秒），请减少图片数量后重试"
            ),
        )

        output_path = work_dir / f"{new_token()}.pdf"
        output_path.write_bytes(built.data)

        return register_pdf_result(
            work_dir,
            [
                PdfOutput(
                    filename=_output_name(images),
                    path=output_path,
                    media_type=PDF_MEDIA_TYPE,
                    page_count=len(images),
                )
            ],
            archive_stem="images",
            original_size=total_bytes,
            page_count=len(images),
            notes=built.notes,
        )
    except BaseException:
        # 包含超时取消：任何异常都必须清掉本次的临时目录
        discard_dir(work_dir)
        raise


async def _intake_images(
    uploads: list[UploadFile],
    work_dir: Path,
) -> tuple[list[ImageSource], int]:
    """逐张落盘并校验，返回 (图片列表, 总字节数)。

    与图片批量处理一致：整批大小上限是请求级门槛，必须在开始排版之前判定，
    不能排到一半才发现超限；但单张校验失败会直接中断 ——
    少了一张的 PDF 是错的，不能「跳过继续」。
    """
    images: list[ImageSource] = []
    total_bytes = 0
    failure: FileToolsError | None = None

    for index, upload in enumerate(uploads):
        try:
            info, source = await intake_upload(upload, work_dir)
        except FileToolsError as exc:
            failure = exc
            break
        except Exception as exc:  # pragma: no cover - 兜底
            failure = ProcessingError("处理失败，请重试")
            failure.__cause__ = exc
            break

        total_bytes += info.size
        if total_bytes > settings.MAX_BATCH_TOTAL_BYTES:
            failure = ValidationError(
                f"一次最多处理 {settings.MAX_BATCH_TOTAL_BYTES // (1024 * 1024)} MB 的图片，请分批上传"
            )
            break

        images.append(_to_source(info, source))

    if failure is not None:
        # 上传流必须关掉，否则客户端会一直等
        await _close_remaining(uploads)
        raise failure

    return images, total_bytes


async def _close_remaining(uploads: list[UploadFile]) -> None:
    for upload in uploads:
        try:
            await upload.close()
        except Exception:  # pragma: no cover - 关闭失败不影响报错
            pass


def _to_source(info: UploadInfo, source: Path) -> ImageSource:
    return ImageSource(
        filename=info.filename,
        data=source.read_bytes(),
        width=info.width,
        height=info.height,
    )


def _output_name(images: list[ImageSource]) -> str:
    """单张图片沿用它的名字，多张图片统一叫 images.pdf。"""
    if len(images) == 1:
        return f"{sanitize_stem(images[0].filename, fallback='image')}.pdf"
    return "images.pdf"


# ----------------------------------------------------------------------
# PDF → 图片
# ----------------------------------------------------------------------

def render_pages_into(
    work_dir: Path,
    source_path: Path,
    pages_raw: str | None,
    options: RenderOptions,
) -> tuple[list[tuple[str, Path, int, bool]], int]:
    """渲染选定页面并逐页落盘，返回 (文件信息列表, 原 PDF 总页数)。

    整个过程都在线程池里跑：打开 PDF、解析范围、渲染、写盘。
    渲染一页就写一页，内存里始终只有当前这一页，
    否则 100 页 × 300 dpi 的图片足以把内存吃光。
    """
    with open_pdf(source_path) as doc:
        total_pages = doc.page_count
        pages = parse_page_range(pages_raw, total_pages)
        check_selection(pages, settings.PDF_EXPORT_MAX_PAGES)

        extension = extension_for(options.fmt)
        written: list[tuple[str, Path, int, bool]] = []

        for label, index in zip(page_labels(pages), pages):
            rendered = render_page_image(doc, index, options)
            path = work_dir / f"{new_token()}{extension}"
            path.write_bytes(rendered.data)
            written.append(
                (f"page-{label}{extension}", path, len(rendered.data), rendered.clamped)
            )

    return written, total_pages


# ----------------------------------------------------------------------
# PDF 合并
# ----------------------------------------------------------------------

async def merge_pdf_uploads(uploads: list[UploadFile]) -> PdfResult:
    """按上传顺序把多份 PDF 合并成一份。

    顺序就是用户在前端拖出来的顺序，因此这里绝不能自己排序 ——
    「合并」这个功能的价值就在于页序完全可控。
    """
    if not uploads:
        raise ValidationError("请至少上传一个 PDF 文件")
    if len(uploads) > settings.MAX_BATCH_FILES:
        raise ValidationError(f"一次最多合并 {settings.MAX_BATCH_FILES} 个文件")

    work_dir = create_temp_dir()

    try:
        sources, total_bytes = await _intake_pdfs(uploads, work_dir)

        merged = await run_in_pool(
            merge_pdfs,
            sources,
            timeout=PDF_MERGE_TIMEOUT_SECONDS,
            timeout_message=(
                f"合并超时（超过 {PDF_MERGE_TIMEOUT_SECONDS} 秒），请减少文件数量后重试"
            ),
        )

        output_path = work_dir / f"{new_token()}.pdf"
        output_path.write_bytes(merged.data)

        return register_pdf_result(
            work_dir,
            [
                PdfOutput(
                    filename="merged.pdf",
                    path=output_path,
                    media_type=PDF_MEDIA_TYPE,
                    page_count=merged.page_count,
                )
            ],
            archive_stem="merged",
            original_size=total_bytes,
            page_count=merged.page_count,
            notes=[f"已按你排定的顺序合并 {merged.sources} 个文件，共 {merged.page_count} 页。"],
        )
    except BaseException:
        discard_dir(work_dir)
        raise


async def _intake_pdfs(
    uploads: list[UploadFile],
    work_dir: Path,
) -> tuple[list[MergeSource], int]:
    """逐份落盘并校验上传的 PDF，返回 (来源列表, 总字节数)。

    与图片一样：整批大小上限是请求级门槛，先判定再合并；
    任何一份不合法都直接中断 —— 缺了一份的合并结果没有意义。
    """
    sources: list[MergeSource] = []
    total_bytes = 0
    failure: FileToolsError | None = None

    for upload in uploads:
        try:
            received = await receive_pdf(upload, work_dir, fallback_name="document.pdf")
        except FileToolsError as exc:
            failure = exc
            break
        except Exception as exc:  # pragma: no cover - 兜底
            failure = ProcessingError("处理失败，请重试")
            failure.__cause__ = exc
            break

        total_bytes += received.size
        if total_bytes > settings.MAX_BATCH_TOTAL_BYTES:
            failure = ValidationError(
                f"一次最多合并 {settings.MAX_BATCH_TOTAL_BYTES // (1024 * 1024)} MB 的文件，请分批上传"
            )
            break

        sources.append(MergeSource(filename=received.filename, path=received.path))

    if failure is not None:
        await _close_remaining(uploads)
        raise failure

    return sources, total_bytes


# ----------------------------------------------------------------------
# PDF 拆分
# ----------------------------------------------------------------------

async def split_pdf_input(input_id: str, *, mode: str, value: str | None) -> PdfResult:
    """按指定方式拆分已上传的 PDF。

    拆出来的每一份都是独立的 PDF，多于一份时自动打包 ZIP；
    只拆出一份（例如「按范围拆分」只填了一个范围）就直接给这份 PDF。
    """
    source = load_input(input_id)
    work_dir = create_temp_dir()

    try:
        result = await run_in_pool(
            split_pdf,
            source.path,
            mode=mode,
            value=value,
            timeout=PDF_SPLIT_TIMEOUT_SECONDS,
            timeout_message=(
                f"拆分超时（超过 {PDF_SPLIT_TIMEOUT_SECONDS} 秒），"
                "请减少拆分的份数后重试"
            ),
        )

        outputs: list[PdfOutput] = []
        for part in result.parts:
            path = work_dir / f"{new_token()}.pdf"
            path.write_bytes(part.data)
            outputs.append(
                PdfOutput(
                    filename=part.filename,
                    path=path,
                    media_type=PDF_MEDIA_TYPE,
                    page_count=len(part.pages),
                )
            )

        return register_pdf_result(
            work_dir,
            outputs,
            archive_stem=f"{input_output_stem(source)}_parts",
            original_size=source.size,
            original_pages=result.total_pages,
            page_count=result.page_count,
            notes=result.notes,
        )
    except BaseException:
        discard_dir(work_dir)
        raise


# ----------------------------------------------------------------------
# PDF 页面删除 / 提取
# ----------------------------------------------------------------------

async def delete_pdf_pages(input_id: str, *, pages_raw: str | None) -> PdfResult:
    """删掉选中的页面，生成一份新的 PDF。"""
    return await _edit_pages(
        input_id,
        pages_raw=pages_raw,
        editor=delete_pages,
        output_name=lambda stem: f"{stem}_edited.pdf",
        timeout_message="删除页面超时，请减少页数后重试",
    )


async def extract_pdf_pages(input_id: str, *, pages_raw: str | None) -> PdfResult:
    """只保留选中的页面，生成一份新的 PDF。"""
    return await _edit_pages(
        input_id,
        pages_raw=pages_raw,
        editor=extract_pages,
        output_name=lambda _stem: "selected_pages.pdf",
        timeout_message="提取页面超时，请减少页数后重试",
    )


async def _edit_pages(
    input_id: str,
    *,
    pages_raw: str | None,
    editor: Callable[..., EditResult],
    output_name: Callable[[str], str],
    timeout_message: str,
) -> PdfResult:
    """删除与提取共用的流程：读入 → 重建 → 落盘 → 登记。

    两个功能的差别只有「保留哪些页」（在 editor 里）和结果叫什么名字，
    流程本身抄两遍只会让超时提示、清理逻辑这些细节走偏。
    """
    source = load_input(input_id)
    work_dir = create_temp_dir()

    try:
        edited = await run_in_pool(
            editor,
            source.path,
            pages_raw,
            timeout=PDF_EDIT_TIMEOUT_SECONDS,
            timeout_message=timeout_message,
        )

        output_path = work_dir / f"{new_token()}.pdf"
        output_path.write_bytes(edited.data)

        return register_pdf_result(
            work_dir,
            [
                PdfOutput(
                    filename=output_name(input_output_stem(source)),
                    path=output_path,
                    media_type=PDF_MEDIA_TYPE,
                    page_count=edited.page_count,
                )
            ],
            archive_stem="edited",
            original_size=source.size,
            original_pages=edited.source_pages,
            page_count=edited.page_count,
            notes=edited.notes,
        )
    except BaseException:
        discard_dir(work_dir)
        raise


# ----------------------------------------------------------------------
# PDF 压缩
# ----------------------------------------------------------------------

async def compress_pdf_input(
    input_id: str,
    *,
    level: str,
    target_bytes: int | None,
) -> PdfResult:
    """按等级（可选目标大小）压缩 PDF。

    结果的 original_size 用的是**原文件大小**，因此响应里的
    saved_bytes / saved_percent 就是 §5 要显示的「节省了多少」，
    不需要前端自己算 —— 前端算的话，两边口径迟早会不一致。
    """
    source = load_input(input_id)
    work_dir = create_temp_dir()

    try:
        compressed = await run_in_pool(
            compress_pdf,
            source.path,
            level=level,
            target_bytes=target_bytes,
            timeout=PDF_COMPRESS_TIMEOUT_SECONDS,
            timeout_message=(
                f"压缩超时（超过 {PDF_COMPRESS_TIMEOUT_SECONDS} 秒），"
                "请换用更低的压缩等级后重试"
            ),
        )

        output_path = work_dir / f"{new_token()}.pdf"
        output_path.write_bytes(compressed.data)

        return register_pdf_result(
            work_dir,
            [
                PdfOutput(
                    filename=f"{input_output_stem(source)}_compressed.pdf",
                    path=output_path,
                    media_type=PDF_MEDIA_TYPE,
                    page_count=compressed.page_count,
                )
            ],
            archive_stem="compressed",
            original_size=compressed.original_size,
            original_pages=compressed.page_count,
            page_count=compressed.page_count,
            notes=compressed.notes,
        )
    except BaseException:
        discard_dir(work_dir)
        raise
