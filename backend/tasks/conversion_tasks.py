"""统一转换中心的批量任务（第七阶段 §6）。

一个批次 = 一组同源同目标的文件（前端按「源格式 → 目标格式」自动分组，
每组提交一个批次）。批次内部与既有三个批量工具完全同构：
逐个排队、逐个转换、单个失败不影响其它、整批收尾时打包。

## 目录布局（关键：每个下载令牌只拥有一个子目录）

::

    <group.directory>/                      批次根目录，受 TASK_TTL 与孤儿清理保护
        in/<token>.<ext>                    上传的源文件，**保留给重试**
        out/<n>/<转换后的文件>               每个成功项一个令牌，独占这个子目录
        zip/filetools-converted.zip         成功 ≥2 个时才有，一个令牌

之所以让每个结果独占一个子目录：下载是「一次性」的，``/api/download``
在发完文件后会 ``remove_dir(job.directory)``。若所有结果共用一个目录，
下载任意一个就会把别人的结果一起删掉。

## 为什么 in/ 里的原件必须留着

重试（§十六）要用它。这也是 ``convert_document`` / ``convert_pdf_to_docx``
只能拿到**副本**的原因 —— 那两个函数会删掉传给它们的文件。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import UploadFile

from config import settings
from conversion import registry
from services.batch_service import (
    ResultEntry,
    receive_uploads,
    register_batch,
    unique_name,
)
from services.conversion_service import (
    ConversionOptions,
    ConversionOutput,
    ConversionRequest,
    convert_file,
    detect_source,
)
from services.intake import run_in_pool
from services.job_store import job_store
from services.progress import valid_progress_id
from services.queue_service import (
    STATE_DONE,
    STATE_FAILED,
    TaskGroup,
    TaskItem,
    task_queue,
)
from utils.errors import (
    ErrorCode,
    FileToolsError,
    UnsupportedConversionError,
    ValidationError,
)
from utils.files import create_temp_dir, new_token, remove_dir

__all__ = ["CONVERSION_TOOL_LABEL", "submit_conversion_batch"]

CONVERSION_TOOL_LABEL = "统一转换中心"

#: 整批打包后的文件名（不含扩展名）
ARCHIVE_STEM = "filetools-converted"

#: 上传阶段的提示语。转换中心的白名单是**所有源格式的并集**，
#: 所以提示语要列全，不能像单格式工具那样只说一种。
UNSUPPORTED_UPLOAD_MESSAGE = (
    "暂不支持这种文件。可转换的格式："
    "图片（JPG / PNG / WEBP）、PDF、Word、Excel、PowerPoint、TXT。"
)


def _all_extensions() -> set[str]:
    """注册表里全部源扩展名的并集，供上传阶段做第一道粗筛。"""
    return {
        ext
        for extensions in registry.EXTENSIONS_BY_SOURCE.values()
        for ext in extensions
    }


async def submit_conversion_batch(
    *,
    uploads: list[UploadFile],
    target_type: str,
    options: ConversionOptions,
    options_payload: dict | None = None,
    progress_ids: list[str | None] | None = None,
) -> TaskGroup:
    """收下一批文件并提交转换。

    ``target_type`` 是这一批统一的目标格式（前端已按源格式分好组，
    同组同目标）。每个文件的源格式**不采信客户端的说法**，一律由
    :func:`detect_source` 按真实内容判定 —— 客户端的分组只用来排版。

    ``options_payload`` 是收敛后的 ``options`` JSON（§二十三），
    **只用于回显**：它写进 ``TaskItem.options`` 让结果页能说明这次用的是什么参数，
    执行路径读的仍然是 ``options`` 这个 DTO。为空时行为与第七阶段完全一致。
    """
    if not uploads:
        raise ValidationError("请至少上传一个文件")
    if len(uploads) > settings.MAX_BATCH_FILES:
        raise ValidationError(f"一次最多处理 {settings.MAX_BATCH_FILES} 个文件")

    # 目标格式先验一遍：整批共用一个目标，早失败比逐个失败省事得多
    if target_type not in registry.TARGET_TYPES:
        raise UnsupportedConversionError("不支持这个目标格式，请重新选择。")

    work_dir = create_temp_dir()
    in_dir = work_dir / "in"
    in_dir.mkdir(parents=True, exist_ok=True)

    try:
        items = await receive_uploads(
            uploads,
            in_dir,
            allowed_extensions=_all_extensions(),
            invalid_type_message=UNSUPPORTED_UPLOAD_MESSAGE,
            total_limit_message=(
                f"这一批文件加起来超过了 "
                f"{settings.MAX_BATCH_TOTAL_BYTES // (1024 * 1024)} MB 的上限，"
                "请分批上传。"
            ),
        )
        _classify(
            items,
            target_type=target_type,
            options_payload=options_payload,
            progress_ids=progress_ids,
        )
    except Exception:
        remove_dir(work_dir)
        raise

    group = TaskGroup(
        group_id=new_token(),
        tool="conversion.unified",
        label=CONVERSION_TOOL_LABEL,
        directory=work_dir,
        items=items,
        handler=_handler(options),
        finalizer=_finalize,
        # 只有统一转换中心在界面上提供重试（§十六），所以只有它的批次
        # 需要把失败项的原始文件留到保留期结束。
        retry_enabled=True,
    )
    return await task_queue.submit(group)


def _classify(
    items: list[TaskItem],
    *,
    target_type: str,
    options_payload: dict | None = None,
    progress_ids: list[str | None] | None,
) -> None:
    """给每一项定性：这是什么格式、要转成什么、能不能转。

    定性在这里一次做完（提交时），而不是等到 worker 里：
    用户上传完立刻就能看到「这 3 个能转、那 1 个不支持」，
    不用等排到队才被告知。

    判定不通过的项**直接记为失败并清掉 source**（见下），
    它们不会进队列、不会占用 LibreOffice / OCR，也不可重试 ——
    「这个文件本来就转不了」，重试一百次也是同样的结果（§十六）。
    """
    for item in items:
        if item.source is None:
            # 上传阶段就被拒的项（类型不在白名单 / 超过大小上限）：
            # 保持它原来的失败原因，不要覆盖成别的
            continue

        item.target_type = target_type
        try:
            detected = detect_source(item.source, item.filename)
        except FileToolsError as exc:
            _reject(item, exc.code, exc.message)
            continue

        item.source_type = detected.source_type

        entry = registry.capability_for(detected.source_type, target_type)
        if entry is None:
            source_label = registry.SOURCE_LABELS.get(
                detected.source_type, detected.source_type
            )
            target_label = registry.TARGET_LABELS.get(target_type, target_type)
            _reject(
                item,
                ErrorCode.UNSUPPORTED_CONVERSION,
                f"暂不支持把「{source_label}」转换为「{target_label}」，"
                "请为这个文件换一个目标格式。",
            )
            continue

        # 命中能力的 ID（§二十三）。纯展示：执行路径仍然只认
        # source_type / target_type，所以这里就算写错也不会转错东西。
        item.conversion_id = entry.id
        # 这次用的参数，同样只用于回显（结果卡上说明「按什么参数转的」）。
        # 判定不通过的项拿不到它 —— 它们根本没走到这里。
        if options_payload:
            item.options = dict(options_payload)

        # 进度 id 与上传顺序对齐。形状不合法的一律当作没传 ——
        # 与第六阶段一致：不为了让位一个非法 id 就报参数错误。
        if progress_ids and item.index < len(progress_ids):
            candidate = progress_ids[item.index]
            if valid_progress_id(candidate):
                item.payload = {"progress_id": candidate}


def _reject(item: TaskItem, key: str, message: str) -> None:
    """把一项标成「这个文件转不了」。

    清掉 ``source`` 有两个作用：一是这一项确实不需要再读盘了，
    二是 ``TaskItem.can_retry()`` 要求 source 非空，于是它自然不可重试 ——
    正是我们想要的（§十六：只重试服务器侧的偶发问题，不重试「文件本身不行」）。
    """
    item.state = STATE_FAILED
    item.error_code = key
    item.error_message = message
    item.source = None
    item.payload = None


def _handler(options: ConversionOptions):
    """生成处理器：一项 = 一次转换 + 一个独占的输出目录。"""

    async def handler(item: TaskItem, group: TaskGroup) -> dict:
        if item.source is None:  # pragma: no cover - 队列里一定有源文件
            raise ValidationError("文件已被清理，请重新上传")

        out_dir = group.directory / "out" / str(item.index)
        # 先把这个独占目录清空再开工。重试时尤其重要：上一轮可能
        # 已经登记过一个指向这里的令牌（转换成功、但之后的某一步出错），
        # 不清掉就会出现「两个令牌共用同一个目录」——
        # 下载其中任意一个都会把另一个的文件删掉。
        # 清掉之后，旧令牌会在下载时报「结果已过期」，这是诚实的结局。
        remove_dir(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        request = ConversionRequest(
            source=item.source,
            filename=item.filename,
            source_type=item.source_type,
            target_type=item.target_type,
            out_dir=out_dir,
            options=options,
            progress_id=(item.payload or {}).get("progress_id"),
        )

        output = await convert_file(request)

        if group.cancel_requested:
            # 用户已经请求取消。转换本身停不下来（线程池里的活，
            # asyncio 取消不了），但**结果可以不要**：令牌是刚刚才登记的，
            # 作废它就不会留下一个没人要的孤儿下载链接。
            # 随后队列会把这一项改判成 cancelled。
            job_store.discard(output.job_id)
            return {"filename": output.filename, "cancelled": True}

        item.payload = {**(item.payload or {}), "output": output}
        return _item_summary(output, item.target_type)

    return handler


def _item_summary(output: ConversionOutput, target_type: str) -> dict:
    """单项结果摘要。**只放展示与下载需要的东西**，不含服务器路径（§18）。

    ``target_type`` 只用来判断能不能预览。由调用方传进来而不是塞进
    ``ConversionOutput``：目标格式是**任务**的属性（``TaskItem.target_type``
    一直都有），DTO 里再存一份就是第二份真相。
    """
    return {
        "filename": output.filename,
        # ``size`` 是第九阶段已经出网的键，结果卡在用；``output_size``
        # 是 §三十一 点名的那个名字。**同一个数，同一个来源**（都取
        # ``output.size``），在这里一次赋值给两个键 —— 不是两份真相，
        # 是一份数据配一个兼容别名。改一个不改另一个的情况不可能发生。
        "size": output.size,
        "output_size": output.size,
        "media_type": output.media_type,
        "download_url": output.download_url,
        # ---- 预览（§三十九–§四十一）----
        # 能内联看的结果才给地址，给不出来就是 ``None``。前端因此不需要
        # 自己判断「这个格式能不能显示」—— 那会变成第二份格式知识。
        "preview_url": registry.preview_url(output.job_id, target_type),
        "page_count": output.page_count,
        "width": output.width,
        "height": output.height,
        # ---- 压缩的如实报告（§三十一/§三十二）----
        # 取不到就是不适用，一律 ``None``，绝不填 0 或 True 冒充一个结论。
        "original_size": output.original_size,
        "target_size": output.target_size,
        "quality_used": output.quality_used,
        "target_reached": output.target_reached,
        "notes": list(output.notes),
    }


async def _finalize(group: TaskGroup) -> None:
    """整批收尾：登记整批的下载入口。

    沿用前几个阶段定下的规则：**成功 1 个就直接给那个文件，≥2 个才打包**。
    单文件套一层 ZIP 只会让人多解压一次。
    """
    succeeded = [
        item for item in group.items if item.state == STATE_DONE and item.payload
    ]
    if not succeeded:
        # 全失败或全取消：交给队列去定整组的状态与原因
        return

    if len(succeeded) == 1:
        # 只有一项成功时，它的令牌就是整批的下载入口 ——
        # **不再为同一个文件登记第二个令牌**：两个令牌会共用同一个目录，
        # 下载任意一个都会把另一个指向的文件删掉。
        output: ConversionOutput = succeeded[0].payload["output"]
        group.job_id = output.job_id
        group.download_url = output.download_url
        group.summary = _summary(group, archived=False, archive_filename=None)
        return

    entries = _dedup_entries(succeeded)
    zip_dir = group.directory / "zip"
    zip_dir.mkdir(parents=True, exist_ok=True)

    job_id, download_url, archive_filename = await run_in_pool(
        _build_archive,
        zip_dir,
        entries,
        timeout=settings.BATCH_TIMEOUT_SECONDS,
        timeout_message=(
            f"打包结果超时（超过 {settings.BATCH_TIMEOUT_SECONDS} 秒），"
            "请减少文件数量后重试"
        ),
    )

    group.archive_job_id = job_id
    group.job_id = job_id
    group.download_url = download_url
    group.summary = _summary(group, archived=True, archive_filename=archive_filename)


def _build_archive(
    zip_dir: Path, entries: list[ResultEntry]
) -> tuple[str, str, str | None]:
    """在线程池里跑的那一段：把结果打成 ZIP 并登记令牌。"""
    return register_batch(zip_dir, entries, archive_stem=ARCHIVE_STEM)


def _dedup_entries(items: list[TaskItem]) -> list[ResultEntry]:
    """按上传顺序生成 ZIP 成员，同名的加 ``-2``、``-3`` 后缀。

    同名是真会发生的：两个不同目录下的 ``报告.docx`` 转出来都叫 ``报告.pdf``。
    不处理的话 ZIP 里只会剩下最后一个 —— 用户拿到一个「少了文件」的压缩包，
    而且不会有任何提示。
    """
    used: set[str] = set()
    entries: list[ResultEntry] = []
    for item in items:
        output: ConversionOutput = item.payload["output"]
        entries.append(
            ResultEntry(
                index=item.index,
                filename=unique_name(output.filename, used),
                path=output.path,
                media_type=output.media_type,
            )
        )
    return entries


def _summary(
    group: TaskGroup, *, archived: bool, archive_filename: str | None
) -> dict:
    """整批结果摘要。逐项摘要原样带上，前端据此渲染统一结果列表。"""
    counts = group.counts()
    results = [
        _item_summary(item.payload["output"], item.target_type)
        for item in group.items
        if item.state == STATE_DONE and item.payload
    ]
    notes: list[str] = []
    for result in results:
        for note in result["notes"]:
            if note not in notes:
                notes.append(note)

    return {
        "total": counts["total"],
        "completed": counts["completed"],
        "failed": counts["failed"],
        "cancelled": counts["cancelled"],
        "archived": archived,
        "archive_filename": archive_filename,
        "items": results,
        "notes": notes,
    }
