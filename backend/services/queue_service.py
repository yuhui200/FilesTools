"""后台任务队列（第四阶段 §5 / §6 / §7）。

一次批量处理的生命周期：

    用户上传 → 建立任务组（等待中）
             → 任务进队列 → worker 认领（处理中）
             → 处理完成 / 失败（逐文件独立）
             → 整批收尾：打包 ZIP、登记一次性下载令牌
             → 用户下载 → 结果文件自动删除

## 为什么是进程内队列，而不是 Redis + Celery

- 处理全部是 CPU 密集型的本地计算，没有跨机器分发的需求；
- 单机部署时 Redis 只是多了一个必须常驻、必须运维的进程，
  Windows 上还要额外解决 Redis 与 Celery 线程池的兼容问题；
- 真正需要防的是「同时跑太多大文件把内存打满」，这一点由
  ``services/intake`` 里**全局唯一**的线程池兜住，队列只负责排队与状态。

因此这里用 asyncio 队列 + N 个 worker 协程实现。所有实现细节都关在本模块里，
将来要换成 Celery，只需重写本模块并把 ``tasks/`` 里的处理器改成 Celery task，
路由与前端拿到的状态结构都不用动。

## 并发是怎么被限制住的（§7）

每个 worker 一次只处理一个文件；worker 内部的 CPU 计算通过
:func:`services.intake.run_in_pool` 提交给共享线程池，线程池大小是
``MAX_WORKERS``。于是**全局**同时在跑的 CPU 任务不会超过 ``MAX_WORKERS`` 个，
不管有多少个批量任务同时在排队。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from config import settings
from services.job_store import job_store
from services.queue_backend import PRIORITY_NORMAL, QueueEntry
from services.worker_pool import POOL_DEFAULT, WorkerPoolManager, pool_for
from utils import metrics as metrics_module
from utils.errors import (
    ErrorCode,
    FileToolsError,
    ProcessingError,
    TaskNotFoundError,
    TaskNotRetryableError,
    is_retryable_code,
)
from utils.files import remove_dir
from utils.logging import (
    EVENT_TASK_ABANDONED_FINISHED,
    EVENT_TASK_CANCELLED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_RETRIED,
    EVENT_TASK_STARTED,
    EVENT_TASK_TIMEOUT,
    duration_ms,
    log_event,
)

logger = logging.getLogger(__name__)

# 单个文件的状态（§4）
#
# 前四个是第四阶段就有的，**名字与取值一个都没动** —— 既有四个测试模块
# 与前端的批量页面都按字面量判断它们。第七阶段只追加 ``cancelled``。
STATE_WAITING = "waiting"
STATE_PROCESSING = "processing"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({STATE_DONE, STATE_FAILED, STATE_CANCELLED})

STATE_LABELS: dict[str, str] = {
    STATE_WAITING: "等待中",
    STATE_PROCESSING: "处理中",
    STATE_DONE: "已完成",
    STATE_FAILED: "失败",
    STATE_CANCELLED: "已取消",
}

#: 同一个文件**手动重试**的次数上限（§十六）。
#:
#: 刻意写成常量而不是配置项：这不是一个可以「调大一点」的性能参数，
#: 而是一道保护 LibreOffice / OCR 的闸门 —— 每个文件多跑一次，
#: 服务器就多一次几秒钟的重活。真需要重试第三次，正确做法是让用户
#: 重新上传（那时他会知道自己在重来，而不是狂点按钮）。
MAX_ITEM_RETRIES = 1

# 处理器：处理一个文件，返回给前端展示的结果摘要
Handler = Callable[["TaskItem", "TaskGroup"], Awaitable[dict]]
# 收尾器：整批处理完后打包、登记下载令牌
Finalizer = Callable[["TaskGroup"], Awaitable[None]]


@dataclass(slots=True)
class TaskItem:
    """队列里的一个文件。"""

    index: int              # 上传顺序，从 0 开始
    filename: str
    size: int
    source: Path | None = None   # 落盘后的原始文件；上传阶段就被拒的为 None
    state: str = STATE_WAITING
    error_code: str | None = None
    error_message: str | None = None
    result: dict | None = None   # 单文件结果摘要，直接进快照
    # 处理器自己用的内部数据（结果文件路径、原始的 ItemSummary 等）。
    # **不会**进快照：里面有服务器临时目录的真实路径，不能泄露给前端。
    payload: dict | None = None
    started_at: float | None = None
    finished_at: float | None = None
    # 统一转换中心（第七阶段）：这一项「从什么变成什么」。
    # 交给处理器读，用来决定调哪个转换函数；空串表示这个批次不区分格式
    # （既有四个批量工具都不填，行为完全不变）。
    source_type: str = ""
    target_type: str = ""
    # 统一转换中心 2.0（第九阶段 §二十三）：两项**展示用**的元数据。
    #
    # ``conversion_id`` 是这一项命中的那条能力（``image.jpg-to-png``）。
    # 它**不**参与执行 —— 执行只认 ``source_type`` / ``target_type``，
    # 也就是说 id 拼错了也不会转错东西；它的用处是让前端把一份结果
    # 精确地映射回它当初选的那条能力（结果卡上的「JPG → PNG」）。
    #
    # ``source_format`` / ``target_format`` 这两个字段**故意不加**：
    # 它们已经由 ``source_type`` / ``target_type`` 表达了，加一遍就是
    # 第二份真相，两份迟早会不一致。
    conversion_id: str = ""
    # 用户为这一项提交的参数（扁平点号键，已通过校验）。
    # ``None`` 表示「这个批次没有记录参数」—— 既有四个批量工具永远是 None，
    # 它们的参数在各自的 handler 闭包里，没有任何理由在这里复制一份。
    options: dict | None = None
    # 已经手动重试过几次。重试要重置这个 item 的其它字段，
    # 但**不能重置它自己**，否则上限就形同虚设（§十六）。
    retry_count: int = 0
    # 第八阶段：服务器**自己**重排过几次队（用户没点任何按钮）。
    # 与 ``retry_count`` 分开记是刻意的 —— 那个是手动重试计数，两个接口
    # 都在暴露它，前端「已重试过」的文案也靠它，语义一个字都不能变。
    auto_retry_count: int = 0
    # 第八阶段：这一项被超时放弃过。**如实暴露**，不假装它被中断了：
    # 超时只释放了队列槽位，那条线程还在跑完（线程杀不掉，见 intake）。
    timeout_requested: bool = False
    # 第八阶段：第几次派发这一项。见 ``QueueEntry.execution_seq``。
    execution_seq: int = 0

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def can_retry(self) -> bool:
        """现在能不能重试这一项。前端据此决定按钮是否可点。

        与 :meth:`TaskQueue.retry` 的准入条件保持一致：文件还在磁盘上、
        这一项确实失败过、且重试次数没用完。源文件是否还在由队列判断。
        """
        return (
            self.state == STATE_FAILED
            and self.retry_count < MAX_ITEM_RETRIES
            and self.source is not None
        )


@dataclass(slots=True)
class TaskGroup:
    """一次批量处理。"""

    group_id: str
    tool: str                 # 内部标识，例如 image.convert
    label: str                # 中文名，用于最近处理记录
    directory: Path           # 本批独占的临时目录
    items: list[TaskItem]
    handler: Handler
    finalizer: Finalizer
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    state: str = STATE_WAITING
    # 收尾后由 finalizer 填充
    job_id: str | None = None
    download_url: str | None = None
    # 聚合 ZIP 的那个下载令牌（成功 ≥2 个时才有，见第七阶段 §6）。
    # 与 ``job_id`` 分开记是必需的：重试时要作废并重建 ZIP，
    # 但**不能**连累单文件结果 —— 只有这个字段指向的才是那个可丢弃的打包令牌。
    archive_job_id: str | None = None
    summary: dict | None = None
    # 整批失败时的原因（全部文件都失败，或收尾本身失败）
    error_code: str | None = None
    error_message: str | None = None
    finalized: bool = False
    # 用户请求过取消（§十五）。**只表示「请求过」**，不代表活已经停了 ——
    # 已经在跑的 LibreOffice / OCR 取消不了，界面上要如实显示「正在取消」。
    cancel_requested: bool = False
    # 这一批允不允许重试（§十六）。默认 False：既有的四个批量工具（图片转换、
    # 图片压缩、PDF 转图片…）界面上从来没有重试按钮，谁也不会来调 retry，
    # 为它们留着失败批次的原始文件只是白占磁盘。统一转换中心才把它打开。
    retry_enabled: bool = False

    def counts(self) -> dict[str, int]:
        total = len(self.items)
        done = sum(1 for item in self.items if item.state == STATE_DONE)
        failed = sum(1 for item in self.items if item.state == STATE_FAILED)
        processing = sum(1 for item in self.items if item.state == STATE_PROCESSING)
        cancelled = sum(1 for item in self.items if item.state == STATE_CANCELLED)
        waiting = total - done - failed - processing - cancelled
        return {
            "total": total,
            "completed": done,
            "processing": processing,
            "waiting": waiting,
            "failed": failed,
            "cancelled": cancelled,
        }

    def all_terminal(self) -> bool:
        return all(item.is_terminal() for item in self.items)

    def successful(self) -> list[TaskItem]:
        return [item for item in self.items if item.state == STATE_DONE]

    def retryable(self, item: TaskItem) -> bool:
        """这一项现在能不能重试 —— 对外唯一的答案。

        **整批不支持重试时，任何一项都不能**：只看着 ``TaskItem`` 的
        状态与文件在不在，会得出「这个批次可以重试」的结论，而那个批次
        的目录早就按老规矩删掉了，真去重试只会报「原始文件已被清理」。
        """
        return self.retry_enabled and item.can_retry()

    def first_failure(self) -> TaskItem | None:
        for item in self.items:
            if item.state == STATE_FAILED:
                return item
        return None

    def is_expired(self, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return (moment - self.created_at) > settings.TASK_TTL_SECONDS


class TaskQueue:
    """进程内任务队列 + 任务组登记簿。"""

    def __init__(self) -> None:
        self._groups: dict[str, TaskGroup] = {}
        # 登记簿会被请求协程和清理线程同时访问，用一个锁保护增删；
        # 组内字段只在事件循环里改，所以不需要更细的锁。
        self._lock = threading.Lock()
        self._started = False
        # 第八阶段：一个 asyncio.Queue 换成五个按资源分开的池。
        # **状态机没有搬走** —— 池拿到的 runner 就是下面的 :meth:`_run_item`，
        # 组/项的状态、错误码、取消改判、收尾全留在本模块。
        self._pools = WorkerPoolManager(runner=self._run_item, on_lost=self._abandon_item)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._pools.start()
        stats = self._pools.snapshot()
        logger.info(
            "任务队列已启动（%d 个 worker，分 %d 个资源池）",
            stats["total_workers"],
            len(stats["pools"]),
        )

    async def stop(self, *, grace: float | None = None) -> None:
        """停队列并清掉还没处理完的临时文件。

        ``grace``（默认 ``SHUTDOWN_GRACE_SECONDS``）是留给在跑的项的宽限：
        空载时**零等待**，有项在跑时最多等这么久，到点还没结束的由池
        如实记为失败 —— 不会留下任何一个停在 ``processing`` 的项。
        """
        if not self._started:
            return
        self._started = False
        wait = settings.SHUTDOWN_GRACE_SECONDS if grace is None else grace
        await self._pools.stop(grace=float(wait))

        with self._lock:
            groups = list(self._groups.values())
            self._groups.clear()

        for group in groups:
            # 已收尾的组，目录归结果任务管；没来得及收尾的由这里兜底删除。
            # **关机时不跑收尾器**：那会去打 ZIP、登记下载令牌，而进程马上就没了，
            # 代价是关机时间不可预测，收益是零 —— 那些令牌活不过这一秒。
            if not group.finalized:
                remove_dir(group.directory)

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------

    async def submit(self, group: TaskGroup) -> TaskGroup:
        """登记任务组并把待处理的文件放进**各自资源池**的队列。"""
        if not self._started:
            raise ProcessingError("任务队列尚未就绪，请稍后重试")

        with self._lock:
            self._groups[group.group_id] = group

        pending = [item for item in group.items if not item.is_terminal()]
        for item in pending:
            self._pools.submit(self._entry_for(group, item))

        if group.items and not pending:
            # 全部文件在上传阶段就被拒了（例如扩展名不对），没有可排队的任务，
            # 直接收尾，让前端立刻拿到失败原因。
            await self._finalize(group)

        return group

    # ------------------------------------------------------------------
    # 取消与重试（第七阶段）
    # ------------------------------------------------------------------

    async def cancel(self, group_id: str) -> TaskGroup | None:
        """请求取消整批。**协作式**，不是立刻停。

        为什么不能真停：处理器里的重活跑在共享线程池里
        （``run_in_pool`` → LibreOffice / OCR），``asyncio`` 取消不了线程；
        OCR 还是在**进程内**跑的，连个子进程都没有，没有可杀的对象。
        硬要「取消」只能丢弃结果、让那条线程继续烧 CPU 到自然结束 ——
        那既没有省下资源，又向用户撒了谎（§十五 明令禁止）。

        所以这里只做三件**确实做得到**的事：

        1. 还在排队的（``waiting``）立刻记为 ``cancelled`` —— 这些是真取消，
           它们一个字节的活都还没干；
        2. 已经在跑的（``processing``）**不动它的状态**，等它自己跑完，
           由 :meth:`_run_item` 把结果丢掉并改判为 ``cancelled``；
        3. 打上 ``cancel_requested``，让快照把在跑的项如实报成「正在取消」。

        返回 ``None`` 表示任务不存在或已过期（与 ``get`` 一致）。
        已经收尾的批次原样返回、不做任何改动 —— 没什么可取消的了。
        """
        group = self.get(group_id)
        if group is None:
            return None
        if group.finalized:
            return group

        group.cancel_requested = True
        now = time.time()
        for item in group.items:
            if item.state == STATE_WAITING:
                item.state = STATE_CANCELLED
                item.finished_at = now
                # 排队中被取消的项没有失败原因，也不该凭空造一个
                item.error_code = None
                item.error_message = None
                item.result = None
                item.payload = None
                # 顺手把它从池的队列里摘掉。摘不掉也不影响正确性
                # （执行器开工前会看到它已是 cancelled），只是少排一会儿队。
                self._pools.cancel(group_id=group.group_id, index=item.index)
                metrics_module.metrics.record_settled(
                    status=metrics_module.STATUS_CANCELLED,
                    resource_class=self._resource_class(group, item),
                )
                log_event(
                    EVENT_TASK_CANCELLED,
                    task_id=f"{group.group_id}:{item.index}",
                    group_id=group.group_id,
                    resource_class=self._resource_class(group, item),
                    reason="queued",
                )

        # 全都在排队时，这一下就把整批取消了；还有在跑的项则让
        # 那一项的 _run_item 稍后收尾。两种情况下状态都不会卡在 waiting。
        await self._maybe_finalize(group)
        return group

    async def retry(self, group_id: str, index: int) -> TaskItem:
        """把某一项失败的文件重新排进队列（§十六）。

        只允许失败项，且每项最多 :data:`MAX_ITEM_RETRIES` 次 —— 上限在
        **服务端**强制，不靠前端自觉：一个狂点重试的客户端同样能把
        LibreOffice 打爆，而它恰恰是最需要被拦住的那个。

        另外只对 ``retry_enabled`` 的批次开放（见 :class:`TaskGroup`）：
        其它批次失败时目录已经当场删了，放它进来只会得到一个
        「原始文件已被清理」，不如一开始就说清楚。

        重试会重置该项、重新排队，并作废上一轮的聚合 ZIP 令牌
        （见下），但**不碰其它项的结果文件**。
        """
        group = self.get(group_id)
        if group is None:
            raise TaskNotFoundError("任务不存在或已过期，请重新提交")
        if not group.retry_enabled:
            raise TaskNotRetryableError("这个任务不支持重试，请重新上传文件")

        item = next((entry for entry in group.items if entry.index == index), None)
        if item is None:
            raise TaskNotFoundError("这一项不在该任务里")

        if item.state != STATE_FAILED:
            raise TaskNotRetryableError("只有失败的文件才能重试")
        if item.retry_count >= MAX_ITEM_RETRIES:
            raise TaskNotRetryableError("这一项已经重试过，请重新上传文件")
        if item.source is None or not item.source.exists():
            raise TaskNotRetryableError("原始文件已被清理，无法重试，请重新上传")

        if not self._started:
            raise ProcessingError("任务队列尚未就绪，请稍后重试")

        # 作废上一轮的聚合 ZIP。重试会改变成功集合，让用户拿着一个
        # 「少一个文件」的旧压缩包，比直接告诉他重新下载更糟。
        #
        # **只作废 ZIP**：单文件结果是各自独立的令牌、各自独占 out/<n>/，
        # 重试这一项跟别人的结果是两回事。（成功只有一个时 job_id 就是
        # 那个单文件令牌，此时 archive_job_id 为 None，不会被误删。）
        if group.archive_job_id is not None:
            job_store.discard(group.archive_job_id)
            group.archive_job_id = None
        group.job_id = None
        group.download_url = None
        group.summary = None

        item.retry_count += 1
        item.state = STATE_WAITING
        item.error_code = None
        item.error_message = None
        item.result = None
        # 上一轮的中间产物（结果文件路径、令牌）绝不能带进新一轮
        item.payload = None
        item.started_at = None
        item.finished_at = None
        # 「这一轮被超时放弃过」是**本轮**的事实，重新排队就翻篇了。
        # 刻意**不**重置 auto_retry_count：它记的是「服务器一共自作主张
        # 重排过几次」，那个上限必须跨手动重试也严格成立，否则
        # 「失败 → 自动重试 → 失败 → 手动重试 → 又自动重试」就成了一个
        # 用户点几下就能无限转下去的圈。
        item.timeout_requested = False

        # 让整批重新变成「进行中」：收尾器要再跑一次来重建 ZIP
        group.finalized = False
        group.finished_at = None
        group.state = STATE_WAITING
        group.error_code = None
        group.error_message = None
        # 用户主动重试，说明他还想要这批东西；上一轮的取消不再适用
        group.cancel_requested = False

        metrics_module.metrics.record_retry(automatic=False)
        log_event(
            EVENT_TASK_RETRIED,
            task_id=f"{group.group_id}:{item.index}",
            group_id=group.group_id,
            resource_class=self._resource_class(group, item),
            automatic=False,
            attempt=item.retry_count,
        )
        self._pools.submit(self._entry_for(group, item))
        return item

    def get(self, group_id: str) -> TaskGroup | None:
        with self._lock:
            group = self._groups.get(group_id)
        if group is None:
            return None
        if group.is_expired():
            self._discard(group_id)
            return None
        return group

    def _discard(self, group_id: str) -> None:
        with self._lock:
            group = self._groups.pop(group_id, None)
        if group is None:
            return
        if not group.finalized:
            remove_dir(group.directory)
            return
        # 已经收尾的批次，目录里可能还压着**别人要下载的结果**：
        # 每个下载令牌独占一个子目录（``out/<n>/``、``zip/``），令牌还活着
        # 就不能删目录，否则快照里挂着的下载链接会指向空气。
        #
        # 反过来，令牌都没了就该删 —— 第七阶段的失败批次会刻意留着目录
        # （重试要用 ``in/`` 里那份原件），不留这条出口的话它只能等
        # 两天后的孤儿清理兜底，白白占着磁盘。
        live = job_store.live_dirs()
        if any(live_dir.is_relative_to(group.directory) for live_dir in live):
            return
        remove_dir(group.directory)

    def purge_expired(self) -> int:
        """清理过期的任务记录，返回清理数量。"""
        now = time.time()
        with self._lock:
            expired = [
                gid for gid, group in self._groups.items() if group.is_expired(now)
            ]
        for group_id in expired:
            self._discard(group_id)
        return len(expired)

    def count(self) -> int:
        with self._lock:
            return len(self._groups)

    def live_dirs(self) -> set[Path]:
        """还没收尾的任务组目录，供孤儿目录清理跳过（§14）。"""
        with self._lock:
            return {group.directory for group in self._groups.values()}

    def worker_count(self) -> int:
        """当前 worker 数量。

        **语义与第四阶段完全一致**：未分类的批次（既有四个批量工具、
        测试构造的批次）全部走 ``default`` 池，而那个池的大小就是
        ``QUEUE_WORKERS``。所以这个数仍然是「一个普通批次同时最多
        处理几个文件」的诚实答案。
        """
        return self._pools.worker_count(POOL_DEFAULT)

    def pool_snapshot(self) -> dict:
        """各资源池的实时状态。供 ``/api/system/workers`` 使用。"""
        return self._pools.snapshot()

    # ------------------------------------------------------------------
    # 快照（前端轮询拿到的就是这个）
    # ------------------------------------------------------------------

    def snapshot(self, group: TaskGroup) -> dict:
        counts = group.counts()
        # 「已定下来」的项：成功、失败、取消。取消的也算定下来了 ——
        # 用户已经明确不要它了，进度条停在那里不动才是对的。
        finished = counts["completed"] + counts["failed"] + counts["cancelled"]
        total = counts["total"]
        percent = round(finished / total * 100, 1) if total else 0.0

        result = None
        if group.summary is not None:
            result = dict(group.summary)
            # 结果文件是「一次性」的：下载过、或超过保留时长后令牌就没了。
            # 这里如实反映，避免前端留着一个点了会报错的下载按钮。
            if group.job_id is not None:
                alive = job_store.get(group.job_id) is not None
                result["download_url"] = group.download_url if alive else None
                result["expired"] = not alive

        error = None
        if group.error_code:
            error = {"code": group.error_code, "message": group.error_message}

        return {
            "group_id": group.group_id,
            "tool": group.tool,
            "label": group.label,
            "state": group.state,
            "total": total,
            "completed": counts["completed"],
            "processing": counts["processing"],
            "waiting": counts["waiting"],
            "failed": counts["failed"],
            "cancelled": counts["cancelled"],
            "finished": finished,
            "percent": percent,
            # 「用户请求过取消」与「活真的停了」是两件事，分开暴露（§十五）。
            # 前端拿它把「处理中」显示成「正在取消」，而不是假装已经停了。
            "cancelling": group.cancel_requested,
            "tasks": [
                {
                    "index": item.index,
                    "filename": item.filename,
                    "size": item.size,
                    "state": item.state,
                    "state_label": STATE_LABELS.get(item.state, item.state),
                    "error_code": item.error_code,
                    "error_message": item.error_message,
                    "result": item.result,
                    "source_type": item.source_type,
                    "target_type": item.target_type,
                    # 第九阶段追加的两个展示字段（**纯追加**）：这一项命中
                    # 的那条能力，以及用户提交的参数。既有消费者读的是
                    # source_type / target_type，多两个键不会影响它们。
                    "conversion_id": item.conversion_id,
                    "options": item.options,
                    "retry_count": item.retry_count,
                    "can_retry": group.retryable(item),
                    # 第八阶段追加的两个字段（**纯追加**，既有消费者不受影响）：
                    # 服务器自己重排过几次队；这一轮有没有被超时放弃过。
                    "auto_retry_count": item.auto_retry_count,
                    "timeout_requested": item.timeout_requested,
                }
                for item in group.items
            ],
            "result": result,
            "error": error,
        }

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------

    # -- 派发 ----------------------------------------------------------

    def _resource_class(self, group: TaskGroup, item: TaskItem) -> str:
        """这一项归哪个资源池。"""
        return pool_for(
            tool=group.tool,
            source_type=item.source_type,
            target_type=item.target_type,
        )

    def _entry_for(self, group: TaskGroup, item: TaskItem) -> QueueEntry:
        """给一项造一条队列记录，并**推进它的派发号**（作废上一轮的迟到结果）。"""
        item.execution_seq += 1
        resource_class = self._resource_class(group, item)
        return QueueEntry(
            group_id=group.group_id,
            index=item.index,
            resource_class=resource_class,
            priority=PRIORITY_NORMAL,
            timeout_seconds=self._pools.effective_timeout(resource_class),
            execution_seq=item.execution_seq,
        )

    def _resolve(self, entry: QueueEntry) -> tuple[TaskGroup, TaskItem] | None:
        """把一条队列记录还原成 (组, 项)。已经不该跑的返回 None。"""
        group = self._groups.get(entry.group_id)
        if group is None or group.finalized:
            return None
        item = next((entry_item for entry_item in group.items if entry_item.index == entry.index), None)
        if item is None:
            return None
        if item.state == STATE_CANCELLED:
            # 排队期间被取消的项，别再开工了（§十五）
            return None
        if item.execution_seq != entry.execution_seq:
            # 这一条属于**上一轮**派发：这一项后来被手动/自动重试重新排过队，
            # 队里那条新的才是要跑的。旧的那条直接丢掉，否则同一个文件会跑两遍。
            return None
        return group, item

    async def _run_item(self, entry: QueueEntry) -> None:
        """池交给执行器的一条。**状态机全在这里，池不碰它。**

        签名与第四阶段的 ``_run_item(group, item)`` 不同是有意的：
        池只认队列记录，不认 ``TaskGroup`` / ``TaskItem`` —— 那一层将来
        要能整个换成 Redis。
        """
        resolved = self._resolve(entry)
        if resolved is None:
            return
        group, item = resolved
        seq = item.execution_seq

        if group.state == STATE_WAITING:
            # 整组的状态跟着第一个开始处理的文件走，前端顶部那行
            # 「处理中」才是真实发生的，而不是靠倒计时猜的
            group.state = STATE_PROCESSING
        item.state = STATE_PROCESSING
        item.started_at = time.time()
        metrics_module.metrics.record_started(entry.resource_class)
        log_event(
            EVENT_TASK_STARTED,
            task_id=entry.task_id,
            group_id=group.group_id,
            resource_class=entry.resource_class,
            source_type=item.source_type,
            target_type=item.target_type,
            attempt=item.retry_count + item.auto_retry_count,
            timeout_ms=int(entry.timeout_seconds * 1000),
        )

        outcome: dict | None = None
        error_code: str | None = None
        error_message: str | None = None
        try:
            # 超时是**兜底**：池的生效超时 = max(池配置, 池内既有操作超时)，
            # 所以正常路径上先触发的一定是操作自己的限额（那时用户拿到的是
            # 「拆分超时，请减少页数」这种诚实的说法），这一层只兜住
            # 「连自己的限额都没能收住」的情况。
            outcome = await asyncio.wait_for(
                group.handler(item, group), timeout=float(entry.timeout_seconds)
            )
        except asyncio.TimeoutError:
            # 注意顺序：3.11 起 asyncio.TimeoutError 就是内置 TimeoutError，
            # 而它是 OSError 的子类 —— 放到 OSError 后面就永远轮不到它。
            item.timeout_requested = True
            error_code = ErrorCode.TASK_TIMEOUT
            error_message = "任务处理超时，请稍后重试"
            logger.warning(
                "任务处理超时：%s（%s，%.0f 秒）",
                entry.task_id,
                entry.resource_class,
                entry.timeout_seconds,
            )
            log_event(
                EVENT_TASK_TIMEOUT,
                task_id=entry.task_id,
                group_id=group.group_id,
                resource_class=entry.resource_class,
                timeout_ms=int(entry.timeout_seconds * 1000),
                # 诚实：跑的是线程池里的同步代码，超时**停不下它**。
                # 槽位在这里释放了，CPU 并没有。
                interruptible=False,
            )
        except FileToolsError as exc:
            # 可预期的失败：原因原样告诉用户（§13）
            error_code = exc.code
            error_message = exc.message
        except OSError:
            # 磁盘满、临时文件被占用、目录突然不可写…这类**服务器侧的偶发问题**。
            # 以前它们统统落到笼统的 PROCESSING_FAILED（用户看到的是「处理失败」，
            # 除了重试没有别的可做，而我们还告诉他别重试）；现在给一个有名字的
            # 类别，用户拿到的是「稍后重试通常就能成功」，服务器也自动重排一次。
            logger.warning("任务遇到可恢复的 I/O 错误：%s", entry.task_id, exc_info=True)
            error_code = ErrorCode.TEMPORARY_IO_ERROR
            error_message = "服务器临时出错，请稍后重试"
        except Exception:  # pragma: no cover - 兜底，不把堆栈抛给前端
            logger.exception("任务失败：%s / %s", group.group_id, item.filename)
            error_code = ErrorCode.PROCESSING_FAILED
            error_message = "处理失败，请重试"

        finished_at = time.time()

        if item.execution_seq != seq:
            # 这一项在跑的过程中已经被判失联、并重新派发过了（看门狗回收了
            # 承载它的 worker）。旧协程现在才回来，结果属于上一轮 ——
            # 写回去就是拿一个**可能已被取消或重试**的旧结论覆盖真相。
            log_event(
                EVENT_TASK_ABANDONED_FINISHED,
                task_id=entry.task_id,
                group_id=group.group_id,
                resource_class=entry.resource_class,
                duration_ms=duration_ms(item.started_at, finished_at),
                interruptible=False,
            )
            return

        item.finished_at = finished_at
        if error_code is None:
            item.result = outcome
            item.state = STATE_DONE
            item.error_code = None
            item.error_message = None
        else:
            item.state = STATE_FAILED
            item.error_code = error_code
            item.error_message = error_message

        if group.cancel_requested and item.state == STATE_DONE:
            # 这一项是在「已经请求取消」之后才跑完的：线程停不下来，
            # 结果也已经没有意义了，如实记为已取消并把结果丢掉。
            #
            # 只改判成功项，**不动失败项** —— 失败是真实发生过的信息，
            # 把它改写成「已取消」等于替用户抹掉了「这个文件有问题」这件事。
            item.state = STATE_CANCELLED
            item.result = None
            item.payload = None

        elapsed = duration_ms(item.started_at, item.finished_at)
        if item.state == STATE_CANCELLED:
            settled = metrics_module.STATUS_CANCELLED
        elif item.state == STATE_DONE:
            settled = metrics_module.STATUS_COMPLETED
        else:
            settled = metrics_module.STATUS_FAILED
        metrics_module.metrics.record_settled(
            status=settled,
            resource_class=entry.resource_class,
            duration_ms=elapsed,
            error_code=item.error_code,
        )
        if item.state == STATE_DONE:
            event = EVENT_TASK_COMPLETED
        elif item.state == STATE_CANCELLED:
            event = EVENT_TASK_CANCELLED
        else:
            event = EVENT_TASK_FAILED
        log_event(
            event,
            task_id=entry.task_id,
            group_id=group.group_id,
            resource_class=entry.resource_class,
            status=item.state,
            error_code=item.error_code,
            duration_ms=elapsed,
        )

        if item.state == STATE_FAILED and self._should_auto_retry(group, item, item.error_code):
            await self._auto_retry(group, item, entry)
            return

        await self._maybe_finalize(group)

    # -- 自动重试（§二十 / §二十一）-------------------------------------

    def _should_auto_retry(
        self, group: TaskGroup, item: TaskItem, error_code: str | None
    ) -> bool:
        """这一项现在该不该由**服务器**自己重排一次队。

        只有三类错误可重试（见 ``utils.errors.RETRYABLE_CODES``），而且
        都是「服务器侧一时的问题」—— 文件本身有问题、格式不支持、
        组件没装，重试一百次结果都一样，只会白烧 CPU 并让用户多等。

        超时**默认不自动重试**（``TASK_TIMEOUT_AUTO_RETRY``）：一个已经
        跑了 300 秒还没完的文件，再排一次队多半还是 300 秒，而这段时间里
        正确的做法是把槽位让给别人。
        """
        if error_code is None or not is_retryable_code(error_code):
            return False
        if error_code == ErrorCode.TASK_TIMEOUT and not settings.TASK_TIMEOUT_AUTO_RETRY:
            return False
        if item.auto_retry_count >= max(0, settings.TASK_MAX_AUTO_RETRIES):
            return False
        # 用户已经不要这一批了，别再自作主张排一次
        if group.cancel_requested:
            return False
        # 源文件还在才谈得上重来一遍
        if item.source is None or not item.source.exists():
            return False
        return True

    async def _auto_retry(self, group: TaskGroup, item: TaskItem, entry: QueueEntry) -> None:
        """把这一项重置后重新排进它自己的池。"""
        item.auto_retry_count += 1
        metrics_module.metrics.record_retry(automatic=True)
        log_event(
            EVENT_TASK_RETRIED,
            task_id=entry.task_id,
            group_id=group.group_id,
            resource_class=entry.resource_class,
            automatic=True,
            attempt=item.auto_retry_count,
            error_code=item.error_code,
        )

        # 与手动重试同一套重置：上一轮的中间产物与时间戳绝不能带进新一轮。
        # 这一项刚才还在 processing，所以这个组**不可能**已经收尾，
        # 也就没有需要作废的下载令牌 —— 那两个字段此时本来就是空的。
        item.state = STATE_WAITING
        item.error_code = None
        item.error_message = None
        item.result = None
        item.payload = None
        item.started_at = None
        item.finished_at = None
        group.finalized = False
        group.finished_at = None
        group.state = STATE_WAITING
        group.error_code = None
        group.error_message = None

        self._pools.submit(self._entry_for(group, item))

    # -- 失联结算（池在看门狗回收 / 关机时回调）--------------------------

    async def _abandon_item(self, entry: QueueEntry, reason: str) -> None:
        """一条被判失联的项，由**这一层**决定它记成什么。

        池不知道状态机，也不该知道；它只说「这一条我不管了，原因是 X」。
        """
        group = self._groups.get(entry.group_id)
        if group is None:
            return
        item = next((entry_item for entry_item in group.items if entry_item.index == entry.index), None)
        if item is None or item.state != STATE_PROCESSING:
            # 已经落定了（成功、失败、被取消）—— 看门狗晚了一步，什么都没欠着
            return

        # 推进派发号：那条还在跑的旧协程若日后回来，结果一律作废。
        item.execution_seq += 1
        item.state = STATE_FAILED
        item.error_code = ErrorCode.WORKER_LOST
        item.error_message = (
            "服务正在重启，任务被中断，请重新提交"
            if reason == "shutdown"
            else "任务被中断，请稍后重试"
        )
        item.finished_at = time.time()
        metrics_module.metrics.record_settled(
            status=metrics_module.STATUS_FAILED,
            resource_class=entry.resource_class,
            duration_ms=duration_ms(item.started_at, item.finished_at),
            error_code=ErrorCode.WORKER_LOST,
        )
        log_event(
            EVENT_TASK_FAILED,
            task_id=entry.task_id,
            group_id=group.group_id,
            resource_class=entry.resource_class,
            status=item.state,
            error_code=ErrorCode.WORKER_LOST,
            reason=reason,
        )

        if reason == "shutdown":
            # 关机路径：不重排队（池正在停，submit 会直接被拒），
            # 也不跑收尾器（进程马上就没了，打 ZIP 只会拖长关机时间）。
            return
        if self._should_auto_retry(group, item, ErrorCode.WORKER_LOST):
            await self._auto_retry(group, item, entry)
            return
        await self._maybe_finalize(group)

    async def _maybe_finalize(self, group: TaskGroup) -> None:
        if group.finalized or not group.all_terminal():
            return
        # 结束态先落定，避免收尾过程中被别的 worker 重复触发
        group.finalized = True
        await self._finalize(group)

    async def _finalize(self, group: TaskGroup) -> None:
        """整批收尾：打包、登记下载令牌、确定整组状态。"""
        group.finalized = True
        group.finished_at = time.time()

        try:
            await group.finalizer(group)
        except FileToolsError as exc:
            group.state = STATE_FAILED
            group.error_code = exc.code
            group.error_message = exc.message
            remove_dir(group.directory)
            return
        except Exception:  # pragma: no cover - 兜底
            logger.exception("任务收尾失败：%s", group.group_id)
            group.state = STATE_FAILED
            group.error_code = ErrorCode.SERVER_ERROR
            group.error_message = "服务器处理失败，请稍后重试"
            remove_dir(group.directory)
            return

        if group.successful():
            group.state = STATE_DONE
            return

        # 一个都没成功。要分清「用户自己不要了」和「真的出错了」：
        # 全是取消 → cancelled（用户按的取消键，不是服务器坏了）；
        # 只要有一个是真失败 → 仍按失败报，把第一个失败原因原样返回，
        # 与第三阶段「全部失败时整体报错」的行为保持一致。
        counts = group.counts()
        if counts["cancelled"] and not counts["failed"]:
            group.state = STATE_CANCELLED
            group.error_code = None
            group.error_message = None
            remove_dir(group.directory)
            return

        first = group.first_failure()
        group.state = STATE_FAILED
        group.error_code = first.error_code if first else ErrorCode.PROCESSING_FAILED
        group.error_message = first.error_message if first else "处理失败，请重试"
        self._drop_work_dir(group)

    @staticmethod
    def _drop_work_dir(group: TaskGroup) -> None:
        """整批没成功时删掉临时目录 —— **除非还有项可以被重试**。

        重试（§十六）要用 ``in/`` 里留着的原件，删了就只能报「原始文件已被
        清理」。所以只要还有一项 :meth:`TaskGroup.retryable`，目录就得留着。

        反过来，不支持重试的批次（既有四个批量工具）照旧当场删 —— 它们以前
        就是这个行为，以后也得是，不能因为第七阶段加了重试，就让老工具的
        失败批次白白多占半小时磁盘。

        留着不等于漏掉：任务记录还在 ``_groups`` 里时它的目录被
        :meth:`live_dirs` 保着，记录一过期就会被 :meth:`_discard` 删掉，
        与「进程被强杀」走的孤儿清理是同一条兜底路径。
        """
        if any(group.retryable(item) for item in group.items):
            return
        remove_dir(group.directory)


# 全局单例
task_queue = TaskQueue()
