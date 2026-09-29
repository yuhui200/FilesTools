"""按资源分池的 worker 池（第八阶段 §八 ~ §十五 / §十七 ~ §十九）。

## 解决的是什么

第四阶段以来只有一个全局 FIFO 队列和两个 worker 协程。于是一批 10 个
Office 文档会把后面 3 张图片堵在身后 —— 尽管它们用的根本不是同一种资源
（一个等 LibreOffice，一个等 Pillow），互不相干。

这里按**资源**分成四个池（外加一个认不出资源类时的收容池），各自有
独立的并发上限与排队顺序：一种资源被拖垮时，其它种类照常工作。

## 池是逻辑并发，线程池仍然是全局的

**没有**给每个池各开一个 ThreadPoolExecutor。真正的 CPU 线程上限仍然是
``services/intake`` 里那个全局线程池的 ``MAX_WORKERS`` —— 池管的是
「谁先跑、同时跑几个」，不是「多开几条线程」。多开四个线程池会让
内存上限翻四倍，而图片解码恰恰是内存大户。

## 心跳的诚实定义

处理器里的重活是**同步阻塞**代码（LibreOffice / OCR / Pillow），跑在
线程里；那段时间 worker 协程是挂起的，根本没法上报心跳。所以这里
不做「worker 自己每 N 秒报一次」这种在真实负载下立刻失效的设计。
判断一个槽位是不是卡死了，靠两条**可检测**的事实：

1. 协程本身还在不在（``task.done()``）；
2. 当前项的租约有没有过期（``开工时间 + 生效超时 + 宽限``）。

两条都会导向同一个动作：回收这个槽位，**并把它手上的项如实结算掉**，
所以不会有任何一项永远停在 processing，池也不会永久少一个槽位。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from config import settings
from conversion import registry
from services.queue_backend import InMemoryTaskQueue, QueueEntry
from services.worker import Worker, next_worker_id
from utils import metrics as metrics_module
from utils.logging import (
    EVENT_WORKER_LOST,
    EVENT_WORKER_STARTED,
    EVENT_WORKER_STOPPED,
    log_event,
)

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 池名
# ----------------------------------------------------------------------

POOL_IMAGE = "image"
POOL_PDF = "pdf"
POOL_OFFICE = "office"
POOL_OCR = "ocr"
#: 认不出资源类型的批次走这里（既有四个批量工具之外的新工具、
#: 测试直接构造的 ``tool="test"`` 批次）。大小 = ``QUEUE_WORKERS``，
#: 于是第四阶段「队列同时最多处理 N 个文件」这句话仍然成立。
POOL_DEFAULT = "default"

POOL_ORDER: tuple[str, ...] = (
    POOL_IMAGE,
    POOL_PDF,
    POOL_OFFICE,
    POOL_OCR,
    POOL_DEFAULT,
)

# ----------------------------------------------------------------------
# 资源分类（§九 / §十）
# ----------------------------------------------------------------------

#: 工具名 -> 池。**先查这张表**：工具名是比格式更具体的信号。
#: 既有的四个批量工具都不填 ``source_type``，所以到这里也就定下来了。
_TOOL_POOLS: dict[str, str] = {
    "image.compress": POOL_IMAGE,
    "image.convert": POOL_IMAGE,
    "image.resize": POOL_IMAGE,
    "pdf.to-images": POOL_PDF,
}

#: 源格式的组件需求 -> 池。可以直接 import 注册表：它是**纯数据**，
#: 只 import 了 ``pathlib``（它自己的模块注释就是这么承诺的）。
#:
#: TXT（``builtin``）归 **image** 池而不是 office 池，值得说清楚：
#: 它走 PyMuPDF 排版，全程不碰 LibreOffice。放进 office 池的话，
#: 一批 50 个 txt 会在 ``OFFICE_WORKERS=1`` 上把所有 Office 转换堵在
#: 身后 —— 那正是本阶段要消灭的队头阻塞。它和图片一样属于「进程内
#: CPU 计算」，所以和图片共用一个池。
#:
#: 这张表是 :func:`pool_for` 的**第二条**路径（回退），不是首选：
#: 首选是条目自己声明的 :func:`conversion.registry.pool_for_source`。
#: 两条路径在注册表认识的源上必须给出同一个答案，由
#: ``test_declared_pool_agrees_with_the_requirement_mapping`` 钉住 ——
#: 所以这不是一份重复的真相，而是一条**可对账**的退路。
_REQUIREMENT_POOLS: dict[str, str] = {
    registry.REQUIREMENT_IMAGE: POOL_IMAGE,
    registry.REQUIREMENT_OFFICE: POOL_OFFICE,
    registry.REQUIREMENT_PDF_TO_WORD: POOL_OCR,
    registry.REQUIREMENT_BUILTIN: POOL_IMAGE,
    # Markdown / HTML 子集是自写的纯标准库代码，和 TXT 一样是进程内 CPU 计算。
    # 注册表里还没有 markup 源（第九阶段第 9 步才登记），但它已经是词汇表
    # 的一员，这张表要么完整、要么就得在别处特判一次。
    registry.REQUIREMENT_MARKUP: POOL_IMAGE,
    # TXT → Word 写的是 DOCX（python-docx），但**不进 OCR 池** ——
    # 它不跑 OCR，排在 OCR 后面等一个识别任务会让一批 TXT 干等。
    # 与 ``pdf_to_word`` 用同一个组件探测结果，池子却按「实际干什么活」分：
    # 那是组件需求，这才是资源需求。
    registry.REQUIREMENT_DOCX: POOL_IMAGE,
    # HEIC 的解码与编码（``pillow-heif``）都是进程内的 CPU 计算，与其它
    # 图片格式一样进 image 池 —— 编解码器装在库里，不像 LibreOffice 那样
    # 需要独占一个外部进程，没有理由为它单开一个池。
    #
    # 两个需求都在表里，虽然 ``REQUIREMENT_HEIF_ENCODE`` 从来不会作为
    # **源**的需求出现（``SOURCE_REQUIREMENTS[SOURCE_HEIC]`` 是解码器那一个，
    # 编码器只出现在 ``_REQUIRES_OVERRIDES`` 的逐格覆盖里）。仍然登记它，
    # 是因为这张表回答的问题是「这个需求该进哪个池」—— 答案与它从哪条
    # 路径问过来无关，缺一行只会让后来的人以为编码任务无处可去。
    registry.REQUIREMENT_HEIF_DECODE: POOL_IMAGE,
    registry.REQUIREMENT_HEIF_ENCODE: POOL_IMAGE,
}


def pool_for(*, tool: str, source_type: str, target_type: str) -> str:
    """这个任务项该进哪个池。纯函数、零 I/O、零副作用。

    三条路径，顺序是有讲究的：

    1. **工具名**最具体。「``pdf.to-images`` 处理的是 ``.pdf`` 输入」——
       按格式会被误判成 PDF → Word 那条 OCR 路径
       （``test_pdf_to_images_is_not_mistaken_for_pdf_to_word`` 钉住这一点）。
    2. **条目声明的池子**：统一转换中心的任务项带的正是
       ``(source_type, target_type)``，注册表为它登记了该用哪种资源。
       新增的格式（Markdown / HTML）只在这里有答案 ——
       ``_REQUIREMENT_POOLS`` 里没有它们的历史包袱。
    3. **组件需求映射**：注册表还不认识这个源（词汇表先于条目存在）
       时的退路，也是第八阶段以来的既有行为。
    """
    pool = _TOOL_POOLS.get(tool)
    if pool is not None:
        return pool
    declared = registry.pool_for_source(source_type)
    if declared is not None:
        return declared
    if source_type:
        requirement = registry.SOURCE_REQUIREMENTS.get(source_type)
        if requirement is not None:
            mapped = _REQUIREMENT_POOLS.get(requirement)
            if mapped is not None:
                return mapped
    return POOL_DEFAULT


# ----------------------------------------------------------------------
# 超时组合（§十六 / §十七）
# ----------------------------------------------------------------------

#: 每个池的兜底超时配置名。
_POOL_TIMEOUT_SETTING: dict[str, str] = {
    POOL_IMAGE: "IMAGE_TIMEOUT_SECONDS",
    POOL_PDF: "PDF_TIMEOUT_SECONDS",
    POOL_OFFICE: "OFFICE_TIMEOUT_SECONDS",
    POOL_OCR: "OCR_TIMEOUT_SECONDS",
    POOL_DEFAULT: "DEFAULT_ITEM_TIMEOUT_SECONDS",
}

#: 每个池里**既有操作**自己的超时，且它们都在 ``settings`` 里（可动态读，
#: 测试 monkeypatch 之后立刻生效）。
_SOURCE_TIMEOUT_SETTINGS: dict[str, tuple[str, ...]] = {
    POOL_IMAGE: ("PROCESS_TIMEOUT_SECONDS", "TXT_CONVERT_TIMEOUT_SECONDS"),
    POOL_PDF: (),
    POOL_OFFICE: ("OFFICE_CONVERT_TIMEOUT_SECONDS",),
    POOL_OCR: ("PDF_TO_WORD_TIMEOUT_SECONDS",),
    POOL_DEFAULT: ("PROCESS_TIMEOUT_SECONDS", "BATCH_TIMEOUT_SECONDS"),
}

#: 剩下几个既有超时是模块常量，没法在 import 期安全读进来
#: （``tasks/pdf_tasks`` 会反过来 import 队列，读它就成了循环依赖）。
#: 所以这里放**只读副本**，并在 tests/test_worker_pool.py 里逐项断言
#: 「这张表 == 源常量」。任何一边改了另一边没跟上，测试立刻红。
_SOURCE_TIMEOUT_LITERALS: dict[str, tuple[tuple[str, int], ...]] = {
    POOL_IMAGE: (),
    POOL_PDF: (
        ("services.pdf_tools.PDF_BUILD_TIMEOUT_SECONDS", 180),
        ("services.pdf_tools.PDF_MERGE_TIMEOUT_SECONDS", 180),
        ("services.pdf_tools.PDF_SPLIT_TIMEOUT_SECONDS", 240),
        ("services.pdf_tools.PDF_EDIT_TIMEOUT_SECONDS", 120),
        ("services.pdf_tools.PDF_COMPRESS_TIMEOUT_SECONDS", 300),
        ("tasks.pdf_tasks.PDF_RENDER_TIMEOUT_SECONDS", 300),
        ("routers.pdf.THUMBNAIL_TIMEOUT_SECONDS", 20),
    ),
    POOL_OFFICE: (),
    POOL_OCR: (),
    POOL_DEFAULT: (("tasks.pdf_tasks.PDF_RENDER_TIMEOUT_SECONDS", 300),),
}


def source_timeouts(resource_class: str) -> tuple[tuple[str, int], ...]:
    """某个池里所有既有操作的超时，``(来源, 秒数)``。给报告和测试审计用。"""
    pairs = [
        (f"settings.{name}", int(getattr(settings, name)))
        for name in _SOURCE_TIMEOUT_SETTINGS.get(resource_class, ())
    ]
    pairs.extend(_SOURCE_TIMEOUT_LITERALS.get(resource_class, ()))
    return tuple(pairs)


def pool_timeout_knob(resource_class: str) -> int:
    """池自己的那个兜底配置值。"""
    name = _POOL_TIMEOUT_SETTING.get(resource_class, "DEFAULT_ITEM_TIMEOUT_SECONDS")
    return int(getattr(settings, name))


def effective_timeout_seconds(resource_class: str) -> float:
    """这个池里一个项的生效超时（秒）= **max(池兜底, 池内既有操作超时)**。

    为什么是 max 而不是「池兜底说了算」：外层 ``asyncio.wait_for`` 只能
    取消协程、杀不掉线程。池超时若抢在一次正常操作自己的限额之前触发，
    那条线程会攥着 LibreOffice / OCR 锁继续跑完，排队的人只会越积越多，
    而用户拿到的还是一句笼统的「任务处理超时」而不是诚实的
    「拆分超时，请减少页数」。

    所以池兜底是**下限式的安全网**，不是可以往下拧紧的旋钮。
    """
    values = [pool_timeout_knob(resource_class)]
    values.extend(value for _, value in source_timeouts(resource_class))
    return float(max(values))


def timeout_table() -> list[dict]:
    """整张超时表，供交付报告与 ``/api/system/workers`` 自证。"""
    rows = []
    for name in POOL_ORDER:
        sources = source_timeouts(name)
        rows.append(
            {
                "pool": name,
                "pool_timeout_seconds": pool_timeout_knob(name),
                "existing_timeouts": {label: value for label, value in sources},
                "effective_timeout_seconds": effective_timeout_seconds(name),
            }
        )
    return rows


def pool_sizes() -> dict[str, int]:
    """每个池配了几个 worker。"""
    return {
        POOL_IMAGE: max(1, settings.IMAGE_WORKERS),
        POOL_PDF: max(1, settings.PDF_WORKERS),
        POOL_OFFICE: max(1, settings.OFFICE_WORKERS),
        POOL_OCR: max(1, settings.OCR_WORKERS),
        POOL_DEFAULT: max(1, settings.QUEUE_WORKERS),
    }


# ----------------------------------------------------------------------
# 池
# ----------------------------------------------------------------------

#: 回收槽位 / 关机时，等一个已经 ``cancel()`` 的协程退出的上限（秒）。
#:
#: **必须是有界的**：看门狗要回收的恰恰是「不肯退出」的那种协程，无界等待
#: 就等于看门狗自己卡死在第一个坏槽位上，从此再不照看别的槽位 ——
#: 那比不回收还糟。等不到就不再等：槽位此刻已经不属于它了，
#: 而它若日后带着结果回来，``execution_seq`` 会把那份结果作废掉。
_RECLAIM_GRACE_SECONDS = 1.0

#: 一个任务项的执行器：由 ``TaskQueue`` 提供，负责状态机与收尾。
#: 池**不碰**状态机，只负责「谁在什么时候跑、跑完没有」。
Runner = Callable[[QueueEntry], Awaitable[None]]
#: 项被判定失联时的结算回调，同样由 ``TaskQueue`` 提供。
OnLost = Callable[[QueueEntry, str], Awaitable[None]]


class WorkerPool:
    """一个资源池：N 个 worker 协程 + 一个优先级队列 + 一个看门狗。"""

    def __init__(
        self,
        name: str,
        *,
        size: int,
        runner: Runner,
        on_lost: OnLost,
        watchdog_interval: float | None = None,
    ) -> None:
        self.name = name
        self.size = max(1, int(size))
        self._runner = runner
        self._on_lost = on_lost
        self._backend = InMemoryTaskQueue(name)
        self._workers: list[Worker] = []
        self._watchdog: asyncio.Task | None = None
        self._stopping = False
        self._started = False
        self._lost = 0
        self._watchdog_interval = (
            float(settings.WORKER_WATCHDOG_INTERVAL_SECONDS)
            if watchdog_interval is None
            else float(watchdog_interval)
        )

    # -- 生命周期 ------------------------------------------------------

    def start(self) -> None:
        """拉起全部 worker。

        **不做懒拉起**：池的 worker 数在池创建时就定下来，因为它们要
        回答「这个池的并发上限是多少」—— 一个用 0 回答这个问题的池
        只会让所有依赖它的判断（含既有测试里的 ``worker_count()``）
        在第一批任务提交之前是错的。空转的协程挂在 Event 上，
        不占 CPU。
        """
        if self._started:
            return
        self._started = True
        self._stopping = False
        self._backend.start()
        for _ in range(self.size):
            self._spawn()
        self._watchdog = asyncio.create_task(
            self._watchdog_loop(), name=f"filetools-watchdog-{self.name}"
        )

    async def stop(self, *, grace: float = 0.0) -> None:
        """停止：不再接新项，给在跑的项一段宽限，到点就如实结算。

        ``grace`` 为 0 时**不等待**；空载时无论 grace 多大都是立刻返回。
        """
        if not self._started:
            return
        self._started = False
        self._stopping = True
        # 唤醒所有挂在队列上的 worker，让它们看到「已停止」后退出
        await self._backend.stop()

        if grace > 0 and self.active_count() > 0:
            deadline = time.monotonic() + grace
            while self.active_count() > 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.1)

        # 宽限到期还在跑的：如实结算，绝不留在 processing
        for worker in list(self._workers):
            entry = worker.current
            if entry is None:
                continue
            logger.warning(
                "池 %s 的 %s 在宽限期内没有结束，这一项如实记为被中断",
                self.name,
                worker.worker_id,
            )
            await self._settle_lost(worker, entry, reason="shutdown")

        tasks = [worker.task for worker in self._workers if worker.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            # 同样是有界等待：关机不能被一个不理会取消的协程拖住 ——
            # 关机超时的后果是进程被强杀，那时连日志都留不下来。
            _done, pending = await asyncio.wait(tasks, timeout=_RECLAIM_GRACE_SECONDS)
            for task in pending:
                logger.warning("池 %s 有一个 worker 在取消后仍未退出，不再等待", self.name)

        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog = None

        for worker in self._workers:
            log_event(
                EVENT_WORKER_STOPPED,
                worker_id=worker.worker_id,
                pool=self.name,
                reason="shutdown",
            )
        self._workers = []

    # -- 提交 ----------------------------------------------------------

    def submit(self, entry: QueueEntry) -> QueueEntry:
        """把一条放进本池的队列。"""
        if not self._started:
            from utils.errors import ProcessingError

            raise ProcessingError("任务队列尚未就绪，请稍后重试")
        return self._backend.submit(entry)

    def cancel(self, *, group_id: str, index: int) -> bool:
        """把还在本池排队的那一条摘掉。"""
        return self._backend.cancel(group_id=group_id, index=index)

    # -- 计数 ----------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    def worker_count(self) -> int:
        """本池已拉起的 worker 数。"""
        return len(self._workers)

    def active_count(self) -> int:
        """正在处理项的 worker 数。"""
        return sum(1 for worker in self._workers if worker.current is not None)

    def queue_size(self) -> int:
        return self._backend.size()

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "configured_workers": self.size,
            "started_workers": len(self._workers),
            "active": self.active_count(),
            "queue_size": self._backend.size(),
            "effective_timeout_seconds": effective_timeout_seconds(self.name),
            "handled": self._handled(),
            "failed": self._backend.stats()["failed"],
            "lost": self._lost,
            "workers": [worker.snapshot() for worker in self._workers],
        }

    def _handled(self) -> int:
        return self._backend.stats()["handled"]

    # -- worker 循环 ---------------------------------------------------

    def _spawn(self) -> Worker:
        worker = Worker(worker_id=next_worker_id(), pool=self.name)
        worker.task = asyncio.create_task(
            self._serve(worker), name=f"filetools-{worker.worker_id}"
        )
        self._workers.append(worker)
        log_event(
            EVENT_WORKER_STARTED,
            worker_id=worker.worker_id,
            pool=self.name,
            generation=worker.generation,
        )
        return worker

    async def _serve(self, worker: Worker) -> None:
        """一个 worker 的一生：取一条 → 跑完 → 再取一条。"""
        while not self._stopping:
            try:
                entry = await self._backend.get_next()
            except asyncio.CancelledError:
                if self._stopping:
                    raise
                # 一次**外来**的取消不该让这个槽位永久消失 ——
                # 第四阶段的实现在这里无条件 re-raise，一条 worker 就此
                # 少掉再也没人补，这是本阶段顺手修掉的一个真实缺陷。
                logger.warning("%s 被意外取消，继续取件", worker.worker_id)
                continue
            if entry is None:
                return  # 队列已关闭

            worker.claim(entry)
            failed = False
            try:
                await self._runner(entry)
                self._backend.ack(entry)
            except asyncio.CancelledError:
                failed = True
                self._backend.fail(entry)
                raise
            except Exception:
                failed = True
                self._backend.fail(entry)
                logger.exception(
                    "池 %s 的 %s 执行任务时抛出非预期异常", self.name, worker.worker_id
                )
            finally:
                # 只有当这一代协程还持有这一条时才释放 —— 回收之后
                # 池换的是**新的 Worker 对象**，所以这里不会踩到新一代。
                if worker.current is entry:
                    worker.release(failed=failed)

    # -- 看门狗 --------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self._watchdog_interval)
            except asyncio.CancelledError:
                raise
            if self._stopping:
                return
            for worker in list(self._workers):
                if worker.task is not None and worker.task.done():
                    await self._recycle(worker, reason="coroutine_died")
                elif self._is_overdue(worker):
                    await self._recycle(worker, reason="overdue")

    def _is_overdue(self, worker: Worker) -> bool:
        """当前项是不是已经超出租约：``开工时间 + 生效超时 + 宽限``。

        租约从**认领那一刻**起算，不是从入队 —— 排队等待不算处理时间。
        宽限留给「超时已经触发、线程还在收尾」：进程内的 LibreOffice /
        OCR 线程杀不掉，只能等它自己退出。
        """
        if worker.current is None or worker.started_at is None:
            return False
        lease = (
            float(worker.current.timeout_seconds)
            + float(settings.WORKER_LOST_GRACE_SECONDS)
        )
        return (time.time() - worker.started_at) > lease

    async def _recycle(self, worker: Worker, *, reason: str) -> None:
        """回收一个槽位，并把它手上的项如实结算掉。

        用**新的 Worker 对象**接手，编号不变、代次 +1。这样被取消的
        旧协程即使在 finally 里跑一下，动到的也是旧对象，踩不到新的一代。
        """
        entry = worker.current
        worker.recycles += 1
        self._lost += 1
        metrics_module.metrics.record_worker_lost()
        metrics_module.metrics.record_watchdog_kick()
        log_event(
            EVENT_WORKER_LOST,
            worker_id=worker.worker_id,
            pool=self.name,
            reason=reason,
            recycles=worker.recycles,
        )

        old_task = worker.task
        if old_task is not None and not old_task.done():
            old_task.cancel()
            # 有界等待，见 _RECLAIM_GRACE_SECONDS 的说明
            _done, pending = await asyncio.wait({old_task}, timeout=_RECLAIM_GRACE_SECONDS)
            if pending:
                logger.warning(
                    "池 %s 的 %s 不理会取消，不再等它退出；它若日后返回，结果会被丢弃",
                    self.name,
                    worker.worker_id,
                )
        if worker in self._workers:
            self._workers.remove(worker)

        if entry is not None:
            self._backend.fail(entry)
            await self._settle_lost(worker, entry, reason=reason)

        if self._stopping:
            return
        fresh = Worker(
            worker_id=worker.worker_id,
            pool=self.name,
            recycles=worker.recycles,
            generation=worker.generation + 1,
            handled=worker.handled,
            failed=worker.failed,
        )
        fresh.task = asyncio.create_task(
            self._serve(fresh), name=f"filetools-{fresh.worker_id}"
        )
        self._workers.append(fresh)
        metrics_module.metrics.record_worker_recycled()
        log_event(
            EVENT_WORKER_STARTED,
            worker_id=fresh.worker_id,
            pool=self.name,
            generation=fresh.generation,
        )

    async def _settle_lost(self, worker: Worker, entry: QueueEntry, *, reason: str) -> None:
        """把一条失联的项交回队列那一层结算（失败 → 自动重试 → 收尾）。

        池不知道这一项该记成什么状态，也不该知道 —— 那是队列的事。
        """
        worker.current = None
        worker.started_at = None
        try:
            await self._on_lost(entry, reason)
        except Exception:  # pragma: no cover - 兜底，结算失败也不能让看门狗死
            logger.exception("结算失联任务失败：%s", entry.task_id)


# ----------------------------------------------------------------------
# 管理器
# ----------------------------------------------------------------------


class WorkerPoolManager:
    """五个池的集合，以及「这个项该进哪个池」的唯一入口。"""

    def __init__(self, *, runner: Runner, on_lost: OnLost) -> None:
        sizes = pool_sizes()
        self._pools: dict[str, WorkerPool] = {
            name: WorkerPool(
                name,
                size=sizes[name],
                runner=runner,
                on_lost=on_lost,
            )
            for name in POOL_ORDER
        }

    # -- 生命周期 ------------------------------------------------------

    def start(self) -> None:
        for pool in self._pools.values():
            pool.start()

    async def stop(self, *, grace: float = 0.0) -> None:
        for pool in self._pools.values():
            await pool.stop(grace=grace)

    # -- 派发 ----------------------------------------------------------

    def pool_for(self, *, tool: str, source_type: str, target_type: str) -> str:
        return pool_for(tool=tool, source_type=source_type, target_type=target_type)

    def pool(self, name: str) -> WorkerPool:
        return self._pools[name]

    def submit(self, entry: QueueEntry) -> QueueEntry:
        return self._pools[entry.resource_class].submit(entry)

    def cancel(self, *, group_id: str, index: int) -> bool:
        """把还在某个池里排队的项摘掉。返回是否真的摘到了一条。"""
        for pool in self._pools.values():
            if pool.cancel(group_id=group_id, index=index):
                return True
        return False

    def effective_timeout(self, resource_class: str) -> float:
        return effective_timeout_seconds(resource_class)

    # -- 计数 ----------------------------------------------------------

    def worker_count(self, name: str = POOL_DEFAULT) -> int:
        return self._pools[name].worker_count()

    def active_count(self) -> int:
        return sum(pool.active_count() for pool in self._pools.values())

    def queue_size(self) -> int:
        return sum(pool.queue_size() for pool in self._pools.values())

    def snapshot(self) -> dict:
        pools = [self._pools[name].snapshot() for name in POOL_ORDER]
        return {
            "accepting": all(pool.started for pool in self._pools.values()),
            "total_workers": sum(pool["started_workers"] for pool in pools),
            "total_active": sum(pool["active"] for pool in pools),
            "total_queue": sum(pool["queue_size"] for pool in pools),
            "pools": pools,
        }


__all__ = [
    "POOL_DEFAULT",
    "POOL_IMAGE",
    "POOL_OCR",
    "POOL_OFFICE",
    "POOL_ORDER",
    "POOL_PDF",
    "WorkerPool",
    "WorkerPoolManager",
    "effective_timeout_seconds",
    "pool_for",
    "pool_sizes",
    "source_timeouts",
    "timeout_table",
]
