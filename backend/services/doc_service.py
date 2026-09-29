"""文档转换的流程编排（上传 → 转换 → 可选压缩 → 登记结果）。

和 ``services/pdf_tools.py`` 的分工一样：路由只解析表单，
真正干活的在 ``services/office_converter.py``，这里负责把它们串起来，
并守住三条流程上的约定：

1. **落盘前先做能做的检查**。缺转换组件、扩展名不对、格式对不上号，
   这些都能在写盘之前判出来 —— 为一个注定失败的请求把 50 MB 写进磁盘
   既浪费又是无谓的攻击面。
2. **上传文件必须保留真实扩展名**。LibreOffice 靠文件名挑导入过滤器，
   学 ``receive_pdf`` 存成 ``<token>.upload`` 会静默转换不出任何东西
   （不报错、没输出、查起来极费劲）。
3. **失败一律清掉整个临时目录**。用 ``except BaseException`` 是为了连
   超时抛出的 ``CancelledError`` 一起兜住，否则超时的请求会留下垃圾目录。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from fastapi import UploadFile

from config import settings
from office.loader import (
    KIND_EXCEL,
    KIND_LABELS,
    KIND_POWERPOINT,
    KIND_TEXT,
    KIND_WORD,
    UNSUPPORTED_OFFICE_MESSAGE,
    OfficeInput,
    count_excel_sheets,
    count_powerpoint_slides,
    detect_kind,
    validate_office_upload,
)
from office.txt_to_pdf import TxtOptions
from pdf.compressor import compress_pdf
from pdf.loader import open_pdf
from services.intake import run_in_pool
from services.office_converter import (
    MISSING_COMPONENT_MESSAGE,
    convert_excel_to_pdf,
    convert_powerpoint_to_pdf,
    convert_txt_to_pdf,
    convert_word_to_pdf,
    is_available,
)
from services.pdf_service import (
    PDF_MEDIA_TYPE,
    PdfOutput,
    PdfResult,
    register_pdf_result,
)
from services.pdf_tools import PDF_COMPRESS_TIMEOUT_SECONDS
from utils.errors import (
    ConverterUnavailableError,
    UnsupportedTypeError,
)
from utils.files import human_size, new_token, remove_file, sanitize_stem, save_upload_limited

logger = logging.getLogger(__name__)

__all__ = ["convert_document", "receive_office"]

#: 文档类别 -> 转换实现。四个函数签名不完全相同：Office 三种是
#: 「(源文件, 工作目录) -> PDF 路径」，TXT 还要排版选项、并且多返回一段说明，
#: 所以 TXT 在 convert_document 里单独走一条分支。
_CONVERTERS: dict[str, Callable[[Path, Path], Path]] = {
    KIND_WORD: convert_word_to_pdf,
    KIND_EXCEL: convert_excel_to_pdf,
    KIND_POWERPOINT: convert_powerpoint_to_pdf,
}

#: 「最大文件大小」的可选档位，前端照这个渲染单选项。
#: 比 PDF 压缩页少了 20 MB：一份文档转出来的 PDF 极少超过 20 MB，
#: 留着只会把选项列表撑长。一律走 pdf_params 的白名单校验。
DOC_TARGET_PRESETS: tuple[str, ...] = (
    "none",
    "500kb",
    "1mb",
    "2mb",
    "5mb",
    "10mb",
    "custom",
)


async def receive_office(
    upload: UploadFile,
    work_dir: Path,
    *,
    expected_kind: str | None = None,
) -> tuple[OfficeInput, Path]:
    """接收一份上传的文档，校验后落盘，返回 (文档信息, 磁盘路径)。

    ``expected_kind`` 是接口自己声明的类别（``/api/office/word-to-pdf`` 传 word）。
    对不上时给出「你传错了地方」的提示，比笼统的「格式不支持」有用得多。
    """
    original_filename = upload.filename or "document"
    kind = detect_kind(original_filename)
    suffix = Path(original_filename).suffix.lower()

    # ---- 落盘之前能判的都判掉 ----
    if kind is None:
        await upload.close()
        raise UnsupportedTypeError(UNSUPPORTED_OFFICE_MESSAGE)

    if expected_kind is not None and kind != expected_kind:
        await upload.close()
        raise UnsupportedTypeError(
            f"这个功能只能转换「{KIND_LABELS[expected_kind]}」，"
            f"你上传的是「{KIND_LABELS.get(kind, '其它格式')}」。"
            "请回到对应的转换页面，或重新选择文件。"
        )

    if kind != KIND_TEXT and not is_available():
        # 纯文本走 PyMuPDF 排版，不需要 LibreOffice
        await upload.close()
        raise ConverterUnavailableError(MISSING_COMPONENT_MESSAGE)

    # 保留真实扩展名：LibreOffice 靠它判断导入过滤器
    source = work_dir / f"{new_token()}{suffix}"

    try:
        await save_upload_limited(upload, source)
        info = validate_office_upload(source, original_filename)
    except Exception:
        remove_file(source)
        raise
    finally:
        await upload.close()

    return info, source


async def convert_document(
    info: OfficeInput,
    source: Path,
    work_dir: Path,
    *,
    target_bytes: int | None = None,
    txt_options: TxtOptions | None = None,
) -> PdfResult:
    """把一份已落盘的文档转成 PDF，登记后返回结果。

    ``target_bytes`` 不为空时，转换完再尽力压到目标大小以内 ——
    LibreOffice 无法指定输出大小，所以这件事只能转完再做。
    TXT 不看这个参数：一份纯文本排出来的 PDF 谈目标大小没有意义
    （用户能控制的是字号和页面，不是体积），前端也不显示这个选项。
    """
    if info.kind == KIND_TEXT:
        pdf_path, kind_notes = await _convert_text(source, work_dir, txt_options)
        pages = _page_count(pdf_path)
        # TXT 的说明来自排版本身（页数、实际用的字体字号），不需要再读原文件
        remove_file(source)
    else:
        converter = _CONVERTERS.get(info.kind)
        if converter is None:  # pragma: no cover - 路由已按类别限死，走不到这里
            raise UnsupportedTypeError(UNSUPPORTED_OFFICE_MESSAGE)

        timeout = settings.OFFICE_CONVERT_TIMEOUT_SECONDS
        pdf_path = await run_in_pool(
            converter,
            source,
            work_dir,
            timeout=timeout,
            timeout_message=f"文档转换超时（超过 {timeout} 秒），请换一份更小的文档重试",
        )

        pages = _page_count(pdf_path)

        # 计数要在删掉原文件之前做
        kind_notes = _kind_notes(info.kind, source, pages)

        # 转换完成，原始文档就没用了。立刻删掉：结果目录会一直留到用户下载或超时，
        # 没必要让用户的原始文档在旁边多躺这段时间（§十二）。
        remove_file(source)

    notes = [f"已转换为 PDF，共 {pages} 页。", *kind_notes]

    if target_bytes is not None and info.kind != KIND_TEXT:
        notes.extend(
            await _compress_to_target(pdf_path, target_bytes, pages)
        )
        pages = _page_count(pdf_path)

    stem = sanitize_stem(info.filename, fallback="converted")
    return register_pdf_result(
        work_dir,
        [
            PdfOutput(
                filename=f"{stem}.pdf",
                path=pdf_path,
                media_type=PDF_MEDIA_TYPE,
                page_count=pages,
            )
        ],
        archive_stem=stem,
        # original_size 用上传的文档大小，这样前端能并排显示「原文件 / 结果」
        original_size=info.size,
        page_count=pages,
        notes=notes,
    )


async def _convert_text(
    source: Path, work_dir: Path, options: TxtOptions | None
) -> tuple[Path, list[str]]:
    """TXT 排版。不走 LibreOffice，所以超时用的是排版自己的上限。"""
    timeout = settings.TXT_CONVERT_TIMEOUT_SECONDS
    return await run_in_pool(
        convert_txt_to_pdf,
        source,
        work_dir,
        options=options or TxtOptions(),
        timeout=timeout,
        timeout_message=f"文本排版超时（超过 {timeout} 秒），请换一份更短的文本重试",
    )


#: 类别 -> 补充说明的构造器。签名是「(原文件, 转换出的页数) -> 说明列表」；
#: 用不上的参数由各构造器自己忽略。
_KindNotes = Callable[[Path, int], list[str]]


def _excel_notes(source: Path, pages: int) -> list[str]:
    """Excel：说清有几个工作表、隐藏表不会导出。

    用户看不到隐藏表的内容时最该知道的就是这件事，
    所以数得出来就报数字，数不出来（旧版 .xls）就只说行为、不报数字。
    """
    counted = count_excel_sheets(source)
    if counted is None:
        return ["表格里的工作表会一起排进这份 PDF；隐藏的工作表不会导出。"]

    visible, hidden = counted
    notes = [f"表格里有 {visible} 个工作表，已一起排进这份 PDF。"]
    if hidden > 0:
        notes.append(f"另有 {hidden} 个隐藏的工作表，转换时不会导出。")
    return notes


def _powerpoint_notes(source: Path, pages: int) -> list[str]:
    """PowerPoint：说清总页数，以及「没导出的那几页」是怎么回事。

    没导出的页数直接用「总数 − 导出页数」算，不去逐个翻幻灯片部件找隐藏标记：
    实测隐藏幻灯片就是不被导出，两个数字一减就是这个差额，
    多数一遍只是多开 N 次 zip。两个数都报出来，用户自己对得上。
    """
    total = count_powerpoint_slides(source)
    if total is None:
        return ["一页幻灯片对应 PDF 的一页；隐藏的幻灯片不会导出。"]

    if total == pages:
        # 导出页数等于总数，说明一页隐藏的都没有
        return [f"演示文稿里共有 {total} 页幻灯片，已全部排进这份 PDF。"]

    return [
        f"演示文稿里共有 {total} 页幻灯片，这份 PDF 有 {pages} 页；"
        "隐藏的幻灯片不会被导出。"
    ]


_KIND_NOTES: dict[str, _KindNotes] = {
    KIND_EXCEL: _excel_notes,
    KIND_POWERPOINT: _powerpoint_notes,
}


def _kind_notes(kind: str, source: Path, pages: int) -> list[str]:
    """按类别补一条说明；这一类没有要补的就返回空列表。"""
    builder = _KIND_NOTES.get(kind)
    return builder(source, pages) if builder is not None else []


def _page_count(path: Path) -> int:
    """读一次 PDF 拿页数。

    转换器已经复验过产物可读，这里的再次打开是廉价的（只读文件尾），
    但要与压缩后的文件保持一致，所以压缩之后再读一次。
    """
    with open_pdf(path) as doc:
        return doc.page_count


async def _compress_to_target(
    pdf_path: Path, target_bytes: int, pages: int
) -> list[str]:
    """尽力把转换产物压到目标大小以内，返回要如实告诉用户的说明。

    达不到就照实说 —— 压缩器自己会给出「已用到最高压缩档位，仍未达到…」，
    这里不替它圆场，也不谎报成功。
    """
    if pdf_path.stat().st_size <= target_bytes:
        return [
            f"转换结果已经只有 {human_size(pdf_path.stat().st_size)}，"
            "不超过目标大小，因此没有再做压缩。"
        ]

    outcome = await run_in_pool(
        compress_pdf,
        pdf_path,
        level=settings.DEFAULT_PDF_COMPRESS_LEVEL,
        target_bytes=target_bytes,
        timeout=PDF_COMPRESS_TIMEOUT_SECONDS,
        timeout_message=(
            f"压缩超时（超过 {PDF_COMPRESS_TIMEOUT_SECONDS} 秒），"
            "请换用更小的目标大小重试"
        ),
    )

    # kept_original 表示压缩后反而更大，文件保持原样（压缩器已经写明了）
    if not outcome.kept_original:
        pdf_path.write_bytes(outcome.data)

    return list(outcome.notes)
