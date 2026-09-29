"""批量处理的公共部分（第四阶段）。

图片与 PDF 的批量任务共用这一层，保证下面三件事只有一份实现：

1. **上传接收**：逐个流式落盘，边收边算总大小，超过整批上限立刻中断
   （不会先把 300 MB 写完再报错），并且无论成功失败都关闭上传流。
2. **失败文件的表示**：单文件失败不影响整批，失败原因按统一错误码记录。
3. **结果打包与登记**：多于一个结果打成一个 ZIP，只有一个就直接给文件本身；
   打包用 ``ZIP_STORED``（图片本身已压缩，再 deflate 几乎没收益却明显更慢）。

上传阶段只做「便宜的」检查（扩展名白名单 + 单文件大小），
真正的解码 / 解析留给队列里的任务 —— 这样上传请求能立刻返回，
用户在界面上看到的「等待中 → 处理中」才是真实发生的顺序，
而不是一个等整批处理完才动弹的假进度。
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from config import settings
from services.intake import media_type_for
from services.job_store import Job, job_store
from services.queue_service import STATE_FAILED, TaskItem
from utils.errors import ErrorCode, FileToolsError, ProcessingError, ValidationError
from utils.files import new_token, save_upload_limited

__all__ = [
    "ResultEntry",
    "unique_name",
    "receive_uploads",
    "register_batch",
]


def unique_name(name: str, used: set[str]) -> str:
    """避免同名文件在 ZIP 里互相覆盖（photo.jpg → photo-2.jpg）。"""
    if name not in used:
        used.add(name)
        return name
    stem = Path(name).stem
    extension = Path(name).suffix
    counter = 2
    while f"{stem}-{counter}{extension}" in used:
        counter += 1
    unique = f"{stem}-{counter}{extension}"
    used.add(unique)
    return unique


async def _close_remaining(uploads: list[UploadFile], done: int) -> None:
    """中断接收时把还没读的上传流关掉，否则客户端会一直等。"""
    for upload in uploads[done:]:
        try:
            await upload.close()
        except Exception:  # pragma: no cover - 关闭失败不影响报错
            pass


async def receive_uploads(
    uploads: list[UploadFile],
    work_dir: Path,
    *,
    allowed_extensions: set[str],
    invalid_type_message: str,
    total_limit_message: str,
) -> list[TaskItem]:
    """把上传的文件逐个流式落盘，返回与上传顺序一一对应的任务项。

    - 扩展名不在白名单：这一项直接标记为失败（``INVALID_FILE_TYPE``），
      其余文件照常处理 —— 与前三阶段「单张失败不影响整批」的行为一致；
    - 单个文件超过 ``MAX_UPLOAD_BYTES``：同上，标记为 ``FILE_TOO_LARGE``；
    - 整批加起来超过 ``MAX_BATCH_TOTAL_BYTES``：整批拒绝（这是请求级门槛，
      处理到一半才发现超限的话，前面白做、后面还得回滚）。
    """
    items: list[TaskItem] = []
    total_bytes = 0

    for index, upload in enumerate(uploads):
        filename = upload.filename or f"文件 {index + 1}"
        suffix = Path(filename).suffix.lower()

        if suffix not in allowed_extensions:
            await upload.close()
            items.append(
                TaskItem(
                    index=index,
                    filename=filename,
                    size=0,
                    state=STATE_FAILED,
                    error_code=ErrorCode.INVALID_FILE_TYPE,
                    error_message=invalid_type_message,
                )
            )
            continue

        destination = work_dir / f"{new_token()}{suffix}"
        try:
            size = await save_upload_limited(upload, destination)
        except FileToolsError as exc:
            items.append(
                TaskItem(
                    index=index,
                    filename=filename,
                    size=0,
                    state=STATE_FAILED,
                    error_code=exc.code,
                    error_message=exc.message,
                )
            )
            continue
        except Exception:  # pragma: no cover - 磁盘异常等
            items.append(
                TaskItem(
                    index=index,
                    filename=filename,
                    size=0,
                    state=STATE_FAILED,
                    error_code=ErrorCode.PROCESSING_FAILED,
                    error_message="保存上传文件失败，请重试",
                )
            )
            continue
        finally:
            await upload.close()

        total_bytes += size
        if total_bytes > settings.MAX_BATCH_TOTAL_BYTES:
            await _close_remaining(uploads, index + 1)
            raise ValidationError(total_limit_message)

        items.append(TaskItem(index=index, filename=filename, size=size, source=destination))

    return items


# ----------------------------------------------------------------------
# 结果登记
# ----------------------------------------------------------------------


@dataclass(slots=True)
class ResultEntry:
    """一个已生成的结果文件。"""

    index: int
    filename: str
    path: Path
    media_type: str


def register_batch(
    directory: Path,
    entries: list[ResultEntry],
    *,
    archive_stem: str,
) -> tuple[str, str, str | None]:
    """登记下载令牌，返回 ``(job_id, download_url, archive_filename)``。

    只有一个结果时直接给这个文件（单文件套一层 ZIP 没有意义），
    多于一个时打包，并把每个成员登记进 ``Job.extra`` 供预览使用。
    """
    if not entries:
        raise ProcessingError("没有生成任何结果文件，请检查参数后重试")

    single = len(entries) == 1
    if single:
        only = entries[0]
        archive_path, archive_filename = only.path, only.filename
        media_type = only.media_type
    else:
        archive_filename = f"{archive_stem}.zip"
        archive_path = directory / f"{new_token()}.zip"
        _write_zip(archive_path, entries)
        media_type = media_type_for("zip")

    job_id = new_token()
    job_store.add(
        Job(
            job_id=job_id,
            directory=directory,
            path=archive_path,
            filename=archive_filename,
            media_type=media_type,
            size=archive_path.stat().st_size,
            extra={
                "items": [
                    {
                        "index": entry.index,
                        "path": str(entry.path),
                        "filename": entry.filename,
                        "media_type": entry.media_type,
                    }
                    for entry in entries
                ]
            },
        )
    )
    return job_id, f"/api/download/{job_id}", None if single else archive_filename


def _write_zip(destination: Path, entries: list[ResultEntry]) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for entry in entries:
            archive.write(entry.path, arcname=entry.filename)
