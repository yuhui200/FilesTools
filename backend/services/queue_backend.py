"""任务队列后端：接口 + 进程内实现（第八阶段 §五十六）。

这一层只负责**排序与记账**，不认识 ``TaskGroup`` / ``TaskItem``，
也不知道「工具」是什么。它拿到的是一条条 :class:`QueueEntry`。

## 为什么要有一个接口

用户规格要求「将来可以换成 Redis / Celery 而不重写转换器」。
真正决定能不能换的，是**语义**有没有被写清楚，而不是有没有一个 Protocol：

* ``ack`` / ``fail`` / ``cancel`` **都不重投**。当前实现没有 redelivery，
  一条消息出队就是出队。将来换成 Redis 时，这三个方法该做的事会变多
  （至少要 ``XACK``），但调用方的语义不变 —— 处理器**不需要**知道
  自己有没有被重投过，也不需要处理「同一条消息跑两次」。
* ``get_next`` 在队列空时**等待**，不轮询。轮询在内存里只是浪费 CPU，
  换成 Redis 时会变成实打实的 RTT 开销。

## 优先级与老化（§七）

排序键是 ``(effective_rank, seq)``：

* ``seq`` 是提交时分配的全局单调递增序号 ⇒ **同优先级严格 FIFO**；
* ``effective_rank`` 由老化算出，见 :meth:`InMemoryTaskQueue.effective_rank`。

老化是**在出队那一刻现算的**，不是靠一个后台协程定期改权重。这样
不需要「改了权重之后再重排堆」这种容易出错的动作，也就不存在
「两个 worker 同时把同一条取走」的窗口。代价是每次出队要重算一遍
所有排队项的权重 —— 但排队长度受 ``MAX_BATCH_FILES``（50）和任务
TTL 双重约束，n 很小。

**不饿死是可证的**，不是「大概」：一个最低优先级的项等满
``PRIORITY_AGING_SECONDS × PRIORITY_AGING_MAX_STEPS`` 之后，
``effective_rank`` 必然到达最高档；此后它与任何新来的最高优先级项
同档，而按 ``seq`` 它更老 ⇒ 必然先出队。
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from config import settings

# ----------------------------------------------------------------------
# 优先级
# ----------------------------------------------------------------------

#: 数值越小越先出队。默认 NORMAL，且**没有 HTTP 入口** ——
#: 用户选定优先级只作为内部调度手段，接口参数与响应一个字都不改。
PRIORITY_HIGH = 0
PRIORITY_NORMAL = 1
PRIORITY_LOW = 2

PRIORITY_BY_NAME: dict[str, int] = {
    "high": PRIORITY_HIGH,
    "normal": PRIORITY_NORMAL,
    "low": PRIORITY_LOW,
}

PRIORITY_NAMES: dict[int, str] = {rank: name for name, rank in PRIORITY_BY_NAME.items()}


def priority_name(rank: int) -> str:
    """优先级数值对应的名字，未知取值原样返回字符串（不抛错）。"""
    return PRIORITY_NAMES.get(rank, str(rank))


# ----------------------------------------------------------------------
# 队列里的一条
# ----------------------------------------------------------------------


@dataclass(slots=True)
class QueueEntry:
    """队列里的一条待办。**只描述「哪个任务项」，不持有它本身。**

    持有 ``TaskItem`` 会让这一层反向依赖 ``queue_service``，
    而它恰恰是要被换掉的那一层。需要状态时由 runner 拿 ``group_id`` +
    ``index`` 回登记簿查。
    """

    group_id: str
    index: int
    resource_class: str
    seq: int = 0
    priority: int = PRIORITY_NORMAL
    #: 这个池给这一项算出的生效超时（秒）。池超时只从**认领那一刻**起算。
    timeout_seconds: float = 0.0
    #: 第几次派发这一项。每次重新派发（首次入队、手动重试、自动重试）
    #: 都会 +1。用途只有一个：**作废上一轮的迟到结果**。
    #:
    #: 一个被判失联的 worker，它那条线程可能还在跑（线程杀不掉），
    #: 几十秒后才带着结果回来。那时这一项可能已经被重新派发过，甚至
    #: 已经跑完新一轮了 —— 旧结果写回去就是覆盖真相。执行器在动手前
    #: 与收工时各比一次这个号，对不上就整条丢弃。
    execution_seq: int = 0
    enqueued_at: float = field(default_factory=time.time)
    #: 池认领时打的开工时间。排队期间为 None —— 排队不算处理时间。
    started_at: float | None = None

    @property
    def task_id(self) -> str:
        """日志里用的任务标识。**不进任何 HTTP 响应**：``group_id``
        是 ``/api/tasks/{group_id}`` 的凭据，而那个接口会返回文件名。"""
        return f"{self.group_id}:{self.index}"


# ----------------------------------------------------------------------
# 接口
# ----------------------------------------------------------------------


@runtime_checkable
class TaskQueueBackend(Protocol):
    """队列后端。当前只有 :class:`InMemoryTaskQueue` 一个实现。"""

    def start(self) -> None:
        """让队列可以开始收发。"""

    async def stop(self) -> None:
        """关闭：唤醒所有等待中的取件方，让它们干净退出。"""

    def submit(self, entry: QueueEntry) -> QueueEntry:
        """入队并分配序号。返回同一条（序号已填好）。"""

    async def get_next(self) -> QueueEntry | None:
        """取下一条可执行的；队列关闭且已排空时返回 ``None``。"""

    def ack(self, entry: QueueEntry) -> None:
        """这一条正常跑完了（成功或**预期的**失败都算）。"""

    def fail(self, entry: QueueEntry) -> None:
        """这一条以非预期的方式结束了（worker 没了、被回收了）。"""

    def cancel(self, *, group_id: str, index: int) -> bool:
        """把还在排队的那一条摘掉。已经出队的摘不掉，返回 ``False``。"""

    def size(self) -> int:
        """还在排队的条数。"""


# ----------------------------------------------------------------------
# 进程内实现
# ----------------------------------------------------------------------


class InMemoryTaskQueue:
    """纯内存的优先级队列。**每个池一个** —— 池之间不共享排队顺序。

    共享一个队列就没法让图片和 Office 各排各的，也就回到了第八阶段
    要解决的队头阻塞。
    """

    def __init__(
        self,
        name: str,
        *,
        aging_seconds: int | None = None,
        aging_max_steps: int | None = None,
    ) -> None:
        self.name = name
        self._entries: list[QueueEntry] = []
        self._seq = itertools.count(1)
        # 用 Event 而不是 Condition：取件方只需要「有东西了叫我一声」，
        # 不需要互斥 —— 所有出入队都在事件循环线程上完成，中间没有
        # await，天然是原子的。
        self._wakeup = asyncio.Event()
        self._closed = False
        self._aging_seconds = max(
            1, settings.PRIORITY_AGING_SECONDS if aging_seconds is None else aging_seconds
        )
        self._aging_max_steps = max(
            0,
            settings.PRIORITY_AGING_MAX_STEPS
            if aging_max_steps is None
            else aging_max_steps,
        )
        # 记账（§五十六：ack/fail 只做记账与指标，不重投）
        self._handled = 0
        self._failed = 0
        self._cancelled = 0

    # -- 生命周期 ------------------------------------------------------

    def start(self) -> None:
        self._closed = False

    async def stop(self) -> None:
        """关闭并唤醒所有等待者。**不清空排队项** —— 由队列那一层
        决定这些项最终记成什么（见 ``TaskQueue.stop``）。"""
        self._closed = True
        self._wakeup.set()

    # -- 排序 ----------------------------------------------------------

    def effective_rank(self, entry: QueueEntry, now: float | None = None) -> int:
        """算上老化之后这一条实际的优先级数值（越小越先出队）。"""
        moment = time.time() if now is None else now
        waited = max(0.0, moment - entry.enqueued_at)
        steps = min(int(waited // self._aging_seconds), self._aging_max_steps)
        return max(PRIORITY_HIGH, entry.priority - steps)

    # -- 出入队 --------------------------------------------------------

    def submit(self, entry: QueueEntry) -> QueueEntry:
        entry.seq = next(self._seq)
        self._entries.append(entry)
        self._wakeup.set()
        return entry

    async def get_next(self) -> QueueEntry | None:
        while True:
            if self._entries:
                now = time.time()
                entry = min(
                    self._entries,
                    key=lambda item: (self.effective_rank(item, now), item.seq),
                )
                self._entries.remove(entry)
                return entry
            if self._closed:
                return None
            # clear 与 wait 之间没有 await，所以不可能漏掉一次 submit：
            # 事件循环只能在挂起点切换协程，而这里没有挂起点。
            self._wakeup.clear()
            if self._entries or self._closed:
                continue
            await self._wakeup.wait()

    def peek(self) -> QueueEntry | None:
        """看一眼下一条是谁，但**不取走**。只给测试与调试用。"""
        if not self._entries:
            return None
        now = time.time()
        return min(
            self._entries, key=lambda item: (self.effective_rank(item, now), item.seq)
        )

    # -- 记账 ----------------------------------------------------------

    def ack(self, entry: QueueEntry) -> None:
        self._handled += 1

    def fail(self, entry: QueueEntry) -> None:
        self._handled += 1
        self._failed += 1

    def cancel(self, *, group_id: str, index: int) -> bool:
        for entry in self._entries:
            if entry.group_id == group_id and entry.index == index:
                self._entries.remove(entry)
                self._cancelled += 1
                return True
        return False

    def size(self) -> int:
        return len(self._entries)

    def stats(self) -> dict:
        return {
            "name": self.name,
            "queue_size": len(self._entries),
            "handled": self._handled,
            "failed": self._failed,
            "cancelled": self._cancelled,
        }
