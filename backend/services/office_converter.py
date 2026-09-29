"""Office / TXT → PDF 的转换实现。

**全项目只有这一个模块调 LibreOffice**（§二十：路由里不许出现转换逻辑）。
对外每种格式一个函数，签名统一是「源文件 → 产出 PDF」：

    convert_word_to_pdf / convert_excel_to_pdf / convert_powerpoint_to_pdf
    convert_txt_to_pdf

Office 三种是同一套 LibreOffice 流程；**TXT 是唯一的例外，走 PyMuPDF 自己排版、
完全不碰 LibreOffice**（LibreOffice 导入纯文本时没有字号 / 页面方向这些控制项，
满足不了用户的要求）。它留在这个模块里是为了让「四种格式各一个入口」这个
对外契约只有一处，调用方按 kind 取函数即可。

设计上必须守住的几条，每一条都对应一个实测过的坑：

1. **单飞**：两个 soffice 进程共用同一份 profile 时，第二个会**静默失败**
   （exit=1、无输出、无日志）。所以用模块级锁把 ``subprocess.run`` 串起来。
2. **锁要有上限**：``acquire(timeout=...)`` 而不是无限等。``asyncio.wait_for``
   只取消协程、不杀线程，一个被取消却还活着的线程若攥着锁不放，
   全局线程池会被它耗光，连图片和 PDF 工具一起拖慢。
3. **内层超时必须严格小于外层**：外层超时不会杀线程，只有内层超时才能让
   正常路径在锁里先把锁释放掉。
4. **``soffice.com`` 优先于 ``soffice.exe``**：两者是同一个 523 KB 启动器，
   只差 PE 的 console-subsystem 位。``.com`` 会阻塞并把输出交给我们，
   ``.exe`` 不会 —— 换掉它会让整个模块的等待逻辑失效。
5. **产物必须复验**：LibreOffice 偶尔会"成功"退出却没有可用输出，
   打开一次确认页数，绝不把坏 PDF 交给用户（§二十二「不要生成损坏文件」）。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from config import settings
from office.loader import decode_text
from office.txt_to_pdf import TxtOptions, build_pdf_from_text
from pdf.loader import open_pdf
from utils.errors import (
    ConverterUnavailableError,
    CorruptedFileError,
    ProcessingError,
    ProcessingTimeoutError,
)
from utils.files import new_token, remove_dir

logger = logging.getLogger(__name__)

__all__ = [
    "BUSY_MESSAGE",
    "CONVERT_FAILED_MESSAGE",
    "MISSING_COMPONENT_MESSAGE",
    "convert_excel_to_pdf",
    "convert_powerpoint_to_pdf",
    "convert_txt_to_pdf",
    "convert_word_to_pdf",
    "find_soffice",
    "is_available",
    "shutdown_libreoffice",
]

# 面向用户的统一文案。stdout / stderr 只进日志，绝不出现在响应里（§十三）。
MISSING_COMPONENT_MESSAGE = "当前服务器缺少 Office 转换组件，请联系管理员。"
CONVERT_FAILED_MESSAGE = "文件转换失败，请尝试重新上传文件。"
BROKEN_OUTPUT_MESSAGE = "转换结果无法读取，请重新上传文件。"
BUSY_MESSAGE = "当前正在转换其它文档，请稍后重试。"

#: LibreOffice 可执行文件的候选名。``.com`` 必须排在 ``.exe`` 前面（见模块说明）。
_LAUNCHER_NAMES = ("soffice.com", "soffice.exe", "soffice")

#: 常见的安装位置。查不到再退回 PATH。
_COMMON_DIRS = (
    r"C:\Program Files\LibreOffice\program",
    r"C:\Program Files (x86)\LibreOffice\program",
    "/usr/bin",
    "/usr/local/bin",
    "/opt/libreoffice/program",
    "/snap/bin",
)

#: 转换用的长驻 profile 目录名前缀。
#: 故意**不含** ``filetools`` —— ``sweep_orphan_dirs`` 只清理 ``filetools_`` 开头的
#: 目录，这里靠前缀本身避开，而不是靠 ``-`` 与 ``_`` 的差别去躲（太脆）。
_PROFILE_PREFIX = "office-converter-profile-"

#: 内层超时比外层少的秒数。留出的这段时间用来走「内层超时 → 杀进程 → 抛异常 →
#: 释放锁」这条路径，保证外层 ``wait_for`` 到时，锁已经还回去了。
_TIMEOUT_MARGIN_SECONDS = 30

_LAUNCHER_LOCK = threading.Lock()
_PROFILE_LOCK = threading.Lock()

#: 只缓存**成功**结果，这样服务器上新装了 LibreOffice 不必重启服务。
_soffice_cache: Path | None = None

#: 长驻 profile 目录，首次真正要转换时才创建。
_profile_dir: Path | None = None


def _launcher_names() -> tuple[str, ...]:
    """按平台给出可执行文件名。Linux 上没有 ``.com`` / ``.exe`` 后缀。"""
    if sys.platform == "win32":
        return _LAUNCHER_NAMES
    return ("soffice", "libreoffice")


def _find_in(directory: Path) -> Path | None:
    for name in _launcher_names():
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def find_soffice() -> Path | None:
    """找到可用的 LibreOffice 启动器，找不到返回 None。

    查找顺序：显式配置 → 常见安装位置 → PATH。

    显式配置了 ``FILETOOLS_LIBREOFFICE_PATH`` 却不存在时**直接判定为找不到**，
    不会偷偷改用自动找到的另一个：部署时路径写错却一直用着别处的版本，
    比直接报「缺少组件」难查得多。
    """
    global _soffice_cache
    if _soffice_cache is not None:
        return _soffice_cache

    configured = (settings.LIBREOFFICE_PATH or "").strip()
    if configured:
        target = Path(configured)
        found = _find_in(target) if target.is_dir() else (target if target.is_file() else None)
        if found is None:
            logger.warning("FILETOOLS_LIBREOFFICE_PATH 指向的位置没有可执行文件：%s", configured)
            return None
    else:
        found = None
        for raw in _COMMON_DIRS:
            directory = Path(raw)
            if directory.is_dir():
                found = _find_in(directory)
                if found is not None:
                    break
        if found is None:
            for name in _launcher_names():
                located = shutil.which(name)
                if located:
                    found = Path(located)
                    break

    if found is not None:
        _soffice_cache = found
        logger.info("已找到 Office 转换组件：%s", found)
    return found


def is_available() -> bool:
    """服务器上能不能做 Office 转换。前端在上传之前就会问这个。"""
    return find_soffice() is not None


def _require_soffice() -> Path:
    binary = find_soffice()
    if binary is None:
        raise ConverterUnavailableError(MISSING_COMPONENT_MESSAGE)
    return binary


def _soffice_timeout() -> int:
    """内层 soffice 的超时（秒），保证严格小于外层 ``run_in_pool`` 的超时。"""
    outer = settings.OFFICE_CONVERT_TIMEOUT_SECONDS
    return max(1, min(outer - 10, int(outer * 0.8)))


def _profile() -> Path:
    """转换用的 profile 目录，进程内只建一次。

    LibreOffice 每次启动都要读 profile，共用一个预热好的能省下大约 3 秒
    （实测冷启动 10.7 秒、预热后 7.9 秒）。
    """
    global _profile_dir
    with _PROFILE_LOCK:
        if _profile_dir is None or not _profile_dir.is_dir():
            _profile_dir = Path(
                tempfile.mkdtemp(prefix=_PROFILE_PREFIX, dir=settings.TEMP_ROOT)
            )
        return _profile_dir


def _reset_profile() -> None:
    """丢掉当前的 profile，下次转换会重建一个。

    soffice 被强杀时会在 profile 里留下 ``.lock``，此后每次启动都直接退出、
    什么也不产出。不重建的话，**一次强杀会让这个功能永久坏掉**。
    """
    global _profile_dir
    with _PROFILE_LOCK:
        remove_dir(_profile_dir)
        _profile_dir = None


def shutdown_libreoffice() -> None:
    """服务关闭时清掉长驻 profile 目录。"""
    _reset_profile()


def _profile_is_locked(profile: Path) -> bool:
    """profile 里有没有 soffice 被强杀留下的锁文件。"""
    try:
        return (profile / ".lock").exists()
    except OSError:  # pragma: no cover - 目录刚被删掉
        return False


def _run_soffice(binary: Path, source: Path, outdir: Path) -> subprocess.CompletedProcess:
    """调一次 LibreOffice。调用方负责持锁。"""
    profile = _profile()
    command = [
        str(binary),
        "--headless",
        "--norestore",
        # 每个进程用独立的 profile 实例，避免和用户自己开的 LibreOffice 抢
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(outdir),
        str(source),
    ]

    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_soffice_timeout(),
        # Windows 上必须带这个标志，否则会闪出一个控制台窗口
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _collect_output(outdir: Path, stem: str) -> Path | None:
    """在输出目录里找转换产物。

    LibreOffice 正常按 ``<源文件主名>.pdf`` 命名，但不同版本对特殊字符的
    处理不一致，所以先按名字找、找不到再退回到「目录里唯一的 PDF」。
    """
    expected = outdir / f"{stem}.pdf"
    if expected.is_file() and expected.stat().st_size > 0:
        return expected

    candidates = [
        item for item in outdir.glob("*.pdf") if item.is_file() and item.stat().st_size > 0
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _verify_output(path: Path) -> int:
    """打开转换产物确认它真的是能读的 PDF，返回页数。"""
    try:
        with open_pdf(path) as doc:
            pages = doc.page_count
    except Exception as exc:
        # 任何打不开的情况都算「产物不可用」，不能把字节直接发给用户
        logger.warning("转换产物无法打开：%s（%s）", path, exc)
        raise CorruptedFileError(BROKEN_OUTPUT_MESSAGE) from exc

    if pages <= 0:
        raise CorruptedFileError(BROKEN_OUTPUT_MESSAGE)
    return pages


def _convert_with_libreoffice(source: Path, work_dir: Path) -> tuple[Path, int]:
    """把一份 Office 文档转成 PDF，返回 (产物路径, 页数)。

    产物会被移进 ``work_dir`` 并用随机名，原始上传文件名只用来生成展示名，
    不参与磁盘路径（§十八 防路径穿越）。
    """
    binary = _require_soffice()
    outdir = Path(tempfile.mkdtemp(prefix="office-out-", dir=str(work_dir)))
    output: Path | None = None

    try:
        for attempt in range(2):
            if not _LAUNCHER_LOCK.acquire(timeout=settings.OFFICE_LOCK_WAIT_SECONDS):
                raise ProcessingError(BUSY_MESSAGE)
            try:
                try:
                    proc = _run_soffice(binary, source, outdir)
                except subprocess.TimeoutExpired as exc:
                    # subprocess.run 已经杀掉子进程了；这里只负责翻译成业务异常。
                    # 超时几乎总是「文档太复杂」，所以提示用户换个文件而不是重试。
                    raise ProcessingTimeoutError(
                        f"文档转换超时（超过 {_soffice_timeout()} 秒），"
                        "请尝试拆分后再转换。"
                    ) from exc
                except OSError as exc:
                    logger.warning("无法启动 Office 转换组件：%s", exc)
                    raise ConverterUnavailableError(MISSING_COMPONENT_MESSAGE) from exc
            finally:
                _LAUNCHER_LOCK.release()

            # stdout / stderr 只进日志：里面有服务器路径和内部细节（§十三）
            if proc.stdout.strip() or proc.stderr.strip():
                logger.debug(
                    "soffice 输出（exit=%s）：%s / %s",
                    proc.returncode,
                    proc.stdout.strip()[:500],
                    proc.stderr.strip()[:500],
                )

            output = _collect_output(outdir, source.stem)
            if output is not None:
                break

            # 没有产出 + profile 里残留 .lock = 上次 soffice 被强杀过，
            # 这个 profile 已经废了，重建一次再试。只重试一次。
            if attempt == 0 and _profile_is_locked(_profile()):
                logger.warning("检测到残留的 profile 锁，重建后重试一次")
                _reset_profile()
                continue
            break

        if output is None:
            # 退出码非 0 基本都是读不了源文件（损坏 / 加密 / 伪装成 Office 格式）
            if proc.returncode != 0:
                raise CorruptedFileError(
                    "无法读取该 Office 文件，请检查文件是否损坏。"
                )
            raise ProcessingError(CONVERT_FAILED_MESSAGE)

        pages = _verify_output(output)
    except BaseException:
        remove_dir(outdir)
        raise

    dest = work_dir / f"{new_token()}.pdf"
    try:
        shutil.move(str(output), str(dest))
    except OSError as exc:  # pragma: no cover - 磁盘异常
        remove_dir(outdir)
        logger.warning("移动转换产物失败：%s", exc)
        raise ProcessingError(CONVERT_FAILED_MESSAGE) from exc

    remove_dir(outdir)
    return dest, pages


# ----------------------------------------------------------------------
# 对外接口：每种格式一个函数
#
# LibreOffice 自己按文件内容挑导入过滤器，所以这几个函数目前的实现完全一样。
# 仍然分开写，是因为它们是用户指定的对外契约、也是将来加格式专属选项
# （比如只导出选中的工作表）的接缝；调用方按 kind 取函数，
# 不必在别处再写一遍「哪种格式走哪条路」。
# ----------------------------------------------------------------------


def convert_word_to_pdf(source: Path, work_dir: Path) -> Path:
    """把 Word 文档转成 PDF。

    ``source`` 必须带**真实扩展名**：LibreOffice 靠文件名判断导入过滤器，
    命名成 ``<token>.upload`` 会静默转换不出任何东西。
    """
    pdf_path, _ = _convert_with_libreoffice(source, work_dir)
    return pdf_path


def convert_excel_to_pdf(source: Path, work_dir: Path) -> Path:
    """把 Excel 表格转成 PDF。

    转换组件会把工作簿里的工作表依次排进同一个 PDF，一页接一页；
    **隐藏的工作表不会导出**（实测：两张可见 + 一张隐藏 → 只出 2 页）。
    调用方可以先用 ``office.loader.count_excel_sheets`` 数一下，
    把这件事如实告诉用户。
    """
    pdf_path, _ = _convert_with_libreoffice(source, work_dir)
    return pdf_path


def convert_powerpoint_to_pdf(source: Path, work_dir: Path) -> Path:
    """把 PowerPoint 演示文稿转成 PDF。

    一页幻灯片对应 PDF 的一页；**隐藏的幻灯片不会导出**
    （实测：两张可见 + 一张隐藏 → 只出 2 页）。调用方可以先用
    ``office.loader.count_powerpoint_slides`` 数一下总页数，
    把「没导出的那几页」如实告诉用户。
    """
    pdf_path, _ = _convert_with_libreoffice(source, work_dir)
    return pdf_path


def convert_txt_to_pdf(
    source: Path,
    work_dir: Path,
    *,
    options: TxtOptions,
) -> tuple[Path, list[str]]:
    """把纯文本排成 PDF，返回 (产物路径, 排版说明)。

    这是四个转换函数里**唯一不经过 LibreOffice** 的一个：纯文本的
    字体、字号、页面大小、页面方向都要用户能选，而 LibreOffice 导入
    .txt 时没有这些控制项，所以用 PyMuPDF 自己排（见 office/txt_to_pdf.py）。

    返回值比其他三个多一个说明列表：排版时会算出页数、实际用的字体字号，
    这些要如实告诉用户，由调用方并进结果的说明里。
    """
    # 解码用与校验阶段同一个函数，避免出现「校验说能解、排版却解不开」的裂缝
    text, _encoding = decode_text(source)
    result = build_pdf_from_text(text, options)

    dest = work_dir / f"{new_token()}.pdf"
    try:
        dest.write_bytes(result.data)
    except OSError as exc:  # pragma: no cover - 磁盘异常
        logger.warning("写入排版结果失败：%s", exc)
        raise ProcessingError(CONVERT_FAILED_MESSAGE) from exc
    return dest, result.notes
