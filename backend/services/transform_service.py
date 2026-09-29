"""图片批量处理：压缩 / 格式转换 / 尺寸调整。

三个功能共用同一条流水线（``compressors/pipeline.py``），区别只在传给流水线的参数，
因此这里也只有一套实现：

    队列取出一个文件 → 三层校验 → 跑流水线 → 结果落盘 → 记录摘要
    整批处理完 → 文件名去重 → 打包 ZIP → 登记一次性下载令牌

第四阶段把「整批一次算完」改成了「逐文件进出队列」，
好处是每个文件都有真实的状态与失败原因（§3 / §4），
而且**单张失败不影响整批**这条规则是天然成立的 —— 每个文件本来就是独立任务。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from compressors.encoder import extension_for
from compressors.pipeline import PipelineOptions, run_pipeline
from services.batch_service import ResultEntry, register_batch, unique_name
from services.intake import UploadInfo, media_type_for, run_in_pool, validate_image_file
from services.queue_service import STATE_DONE, TaskGroup, TaskItem
from utils.errors import ErrorCode, ProcessingError
from utils.files import build_output_name, new_token

__all__ = [
    "ItemSummary",
    "ItemFailure",
    "BatchSummary",
    "make_image_handler",
    "finalize_image_batch",
    "process_image_item",
]


@dataclass(slots=True)
class ItemSummary:
    """单个文件的结果摘要。"""

    index: int
    original: UploadInfo
    result_filename: str
    result_size: int
    result_width: int
    result_height: int
    result_format: str
    quality_used: int | None
    scale: float
    target_met: bool
    saved_bytes: int
    saved_percent: float
    untouched: bool
    note: str | None
    # 结果文件在临时目录中的位置，只供服务内部使用，不返回给前端
    path: Path | None = None


@dataclass(slots=True)
class ItemFailure:
    """单个文件的失败原因。"""

    index: int
    filename: str
    code: str
    message: str


@dataclass(slots=True)
class BatchSummary:
    """整批处理的摘要。"""

    items: list[ItemSummary]
    failures: list[ItemFailure]
    original_total: int
    result_total: int
    saved_bytes: int
    saved_percent: float
    target_bytes: int | None
    archived: bool              # 是否打包成了 ZIP
    archive_filename: str | None


def _percent(saved: int, original: int) -> float:
    if original <= 0:
        return 0.0
    return round(saved / original * 100, 1)


async def process_image_item(
    item: TaskItem,
    group: TaskGroup,
    options: PipelineOptions,
    suffix: str,
) -> dict:
    """处理一个文件：校验 → 流水线 → 结果落盘。

    返回给前端的单文件摘要；内部数据（结果文件路径、完整的 ItemSummary）
    放在 ``item.payload`` 里，不进快照。
    """
    if item.source is None:  # pragma: no cover - 队列里一定有源文件
        raise ProcessingError("上传的文件已被清理，请重新上传")

    # 上传阶段只查了扩展名，真正的解码校验在这里做：
    # 坏图会以 CORRUPTED_FILE 记在这一个文件上，不影响同批的其它文件。
    info = validate_image_file(item.source, item.filename, item.size)

    result = await run_in_pool(run_pipeline, item.source, options)

    extension = extension_for(result.format)
    result_path = group.directory / f"{new_token()}{extension}"
    result_path.write_bytes(result.data)

    saved = info.size - len(result.data)
    summary = ItemSummary(
        index=item.index,
        original=info,
        # 真实的文件名去重在收尾时统一做（同名文件要一起看才知道谁该加后缀）
        result_filename=build_output_name(info.filename, suffix, extension),
        result_size=len(result.data),
        result_width=result.width,
        result_height=result.height,
        result_format=result.format,
        quality_used=result.quality_used,
        scale=result.scale,
        target_met=result.target_met,
        saved_bytes=saved,
        saved_percent=_percent(saved, info.size),
        untouched=result.untouched,
        note=result.note,
        path=result_path,
    )
    item.payload = {"summary": summary, "path": result_path}

    return {
        "filename": summary.result_filename,
        "original": {
            "filename": info.filename,
            "size": info.size,
            "width": info.width,
            "height": info.height,
            "format": info.format,
        },
        "result": {
            "filename": summary.result_filename,
            "size": summary.result_size,
            "width": summary.result_width,
            "height": summary.result_height,
            "format": summary.result_format,
        },
        "saved_bytes": summary.saved_bytes,
        "saved_percent": summary.saved_percent,
        "target_met": summary.target_met,
        "quality_used": summary.quality_used,
        "scale": summary.scale,
        "untouched": summary.untouched,
        "note": summary.note,
    }


def make_image_handler(options: PipelineOptions, suffix: str):
    """生成队列用的处理器。"""

    async def handler(item: TaskItem, group: TaskGroup) -> dict:
        return await process_image_item(item, group, options, suffix)

    return handler


def finalize_image_batch(
    group: TaskGroup, options: PipelineOptions, archive_stem: str
) -> BatchSummary | None:
    """整批收尾：文件名去重、打包、登记令牌。

    返回整批摘要（一个都没成功时返回 None）；转换成接口响应由 ``tasks/`` 负责，
    这样 services 层不必反向依赖 routers。

    这是**同步**函数：打包 50 个结果要整块复制几百 MB，调用方必须把它
    放进线程池，别让事件循环停下来等磁盘。
    """
    entries: list[ResultEntry] = []
    summaries: list[ItemSummary] = []
    failures: list[ItemFailure] = []
    used_names: set[str] = set()
    original_total = 0
    result_total = 0

    for item in group.items:
        if item.state != STATE_DONE or not item.payload:
            failures.append(
                ItemFailure(
                    index=item.index,
                    filename=item.filename,
                    code=item.error_code or ErrorCode.PROCESSING_FAILED,
                    message=item.error_message or "处理失败，请重试",
                )
            )
            continue

        summary: ItemSummary = item.payload["summary"]
        path = summary.path
        if path is None:  # pragma: no cover - 内部一致性检查
            continue

        summary = replace(summary, result_filename=unique_name(summary.result_filename, used_names))
        summaries.append(summary)
        entries.append(
            ResultEntry(
                index=item.index,
                filename=summary.result_filename,
                path=path,
                media_type=media_type_for(summary.result_format),
            )
        )
        original_total += summary.original.size
        result_total += summary.result_size

    if not entries:
        # 全部失败：交给队列把整组标成失败并把第一个原因返回
        return None

    job_id, download_url, archive_filename = register_batch(
        group.directory, entries, archive_stem=archive_stem
    )
    group.job_id = job_id
    group.download_url = download_url

    return BatchSummary(
        items=summaries,
        failures=sorted(failures, key=lambda failure: failure.index),
        original_total=original_total,
        result_total=result_total,
        saved_bytes=original_total - result_total,
        saved_percent=_percent(original_total - result_total, original_total),
        target_bytes=options.target_bytes,
        archived=archive_filename is not None,
        archive_filename=archive_filename,
    )
