"""临时文件的创建、安全命名与清理。

安全约定：
- 落盘文件名一律使用随机 token，绝不使用用户提供的文件名；
- 保留原始文件名只用于「下载时显示」和「生成结果文件名」，且经过清洗；
- 所有中间产物都放在系统临时目录下的随机子目录里，不对外暴露路径。
"""

from __future__ import annotations

import logging
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import UploadFile

from config import settings
from utils.errors import FileTooLargeError, ValidationError

logger = logging.getLogger(__name__)

# 文件名允许的字符：中英文、数字、空格、点、横线、下划线、括号
_UNSAFE_CHARS = re.compile(r"[^\w一-鿿.\- ()（）]", flags=re.UNICODE)
_DOTS_ONLY = re.compile(r"^\.+$")

_CHUNK_SIZE = 1024 * 1024  # 1 MB

# 临时目录的统一前缀。清理「孤儿目录」时只认这个前缀，
# 保证不会碰到系统临时目录里别人的东西。
TEMP_PREFIX = "filetools_"

# 面向用户的统一提示文案
TOO_LARGE_MESSAGE = "文件过大，请上传更小的文件。"


def new_token() -> str:
    """生成不可猜测的随机标识，用作文件/任务 ID。"""
    return secrets.token_hex(16)


def create_temp_dir(prefix: str = TEMP_PREFIX) -> Path:
    """在系统临时目录下创建一个隔离的随机子目录。"""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=settings.TEMP_ROOT))
    return path


def remove_dir(path: Path | None) -> None:
    """尽力删除目录，失败不抛异常（清理逻辑不应影响主流程）。"""
    if path is None:
        return
    shutil.rmtree(path, ignore_errors=True)


def remove_file(path: Path | None, *, attempts: int = 4, delay: float = 0.05) -> bool:
    """尽力删除单个文件，失败不抛异常，返回是否删掉了。

    Windows 上刚写完的文件可能被杀毒软件或搜索索引短暂占用，
    这时 ``unlink`` 会抛 ``PermissionError``，几十毫秒后占用就释放了。
    如果不重试，这个清理动作会把真正的错误盖掉 ——
    比如一个损坏的 PDF 本来该提示「文件损坏」，
    结果用户看到的是「另一个程序正在使用此文件」。
    """
    if path is None:
        return True

    for attempt in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt == attempts - 1:
                logger.warning("临时文件删除失败，已交给系统临时目录回收：%s", path)
                return False
            time.sleep(delay)
    return False


def sanitize_stem(filename: str, fallback: str = "file") -> str:
    """把用户提供的文件名清洗成一个安全的「主名」，用于生成下载文件名。"""
    stem = Path(filename).stem
    stem = _UNSAFE_CHARS.sub("_", stem).strip(" ._")
    if not stem or _DOTS_ONLY.match(stem):
        stem = fallback
    # 限制长度，避免生成超长路径
    return stem[:80]


def build_output_name(original_filename: str, suffix: str, extension: str) -> str:
    """生成结果文件名，例如 example_compressed.jpg。"""
    stem = sanitize_stem(original_filename)
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"{stem}{suffix}{ext.lower()}"


async def save_upload_limited(
    upload: UploadFile,
    dest: Path,
    *,
    max_bytes: int | None = None,
) -> int:
    """分块写入上传文件，超过大小上限立即中止并删除。

    返回写入的字节数。不依赖 Content-Length，因此伪造请求头也无法绕过。

    ``max_bytes`` 用于批量请求：此时上限是「整批文件加起来」的额度，
    由调用方在每次写入后扣减剩余额度并传入。
    """
    limit = settings.MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    total = 0
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = await upload.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise FileTooLargeError(TOO_LARGE_MESSAGE)
                fh.write(chunk)
    except FileTooLargeError:
        remove_file(dest)
        raise
    except OSError as exc:
        remove_file(dest)
        raise ValidationError("保存上传文件失败，请重试") from exc

    if total == 0:
        remove_file(dest)
        raise ValidationError("上传的文件为空")

    return total


def human_size(num_bytes: int) -> str:
    """把字节数格式化成人类可读的字符串。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def sweep_orphan_dirs(keep: set[Path] | None = None) -> int:
    """删除临时目录里已经没人认领的 ``filetools_*`` 目录（§14 兜底）。

    正常路径上每个目录都有主：结果文件由 :mod:`services.job_store` 删，
    没处理完的由任务队列删，上传中的由请求自己删。这里兜的是
    **进程被强杀** 的情况 —— 登记表在内存里，重启之后那些目录就没人认领了。

    两道保险，避免误删正在用的文件：

    1. 只删比「最长存活时长」还老一倍的目录。任何在册的文件到这个年纪
       都早该被清掉了，还留着的一定是孤儿；
    2. 跳过调用方给出的 ``keep``（当前登记在册的目录）。

    单个目录删不掉（被杀毒软件占用等）只记一条日志，不影响其它目录。
    """
    root = Path(settings.TEMP_ROOT or tempfile.gettempdir())
    if not root.is_dir():
        return 0

    longest = max(
        settings.JOB_TTL_SECONDS,
        settings.PDF_INPUT_TTL_SECONDS,
        settings.TASK_TTL_SECONDS,
    )
    cutoff = time.time() - longest * 2
    protected = set(keep or ())

    removed = 0
    for entry in root.iterdir():
        if not entry.name.startswith(TEMP_PREFIX) or entry in protected:
            continue
        try:
            if not entry.is_dir() or entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            # 刚好被别的进程删掉了，或者没有权限 —— 都不是问题
            continue
        remove_dir(entry)
        removed += 1

    if removed:
        logger.info("已清理 %d 个无人认领的临时目录", removed)
    return removed
