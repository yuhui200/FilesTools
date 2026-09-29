"""Worker 的身份与租约（第八阶段 §十 / §十九）。

这里**只有**一个数据类和一个编号器，没有执行逻辑 —— 谁在什么时候跑、
跑多久、跑完没有，是 :mod:`services.worker_pool` 的事；具体怎么处理一个
文件，是 :mod:`services.queue_service` 的事。三件事分开，才能各自被替换。

worker 编号是**全局**单调递增的（``worker-1``、``worker-2``…），
池名单独放一个字段。这样编号在任何池里都不重号，运维拿着一个
``worker-7`` 就能定位到唯一的那个槽位；把池名混进编号
（``image-worker-1``）会让「第几个 worker」这个信息消失。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行时不 import
    import asyncio

    from services.queue_backend import QueueEntry

#: worker 的状态词。只区分「空着」和「在忙」——
#: 「停止」「失联」都是池在回收过程中的瞬时判断，不是可以长期停留的状态。
WORKER_IDLE = "idle"
WORKER_BUSY = "busy"

_worker_seq = itertools.count(1)


def next_worker_id() -> str:
    """分配一个全局唯一的 worker 编号。"""
    return f"worker-{next(_worker_seq)}"


@dataclass(slots=True)
class Worker:
    """一个 worker 槽位。"""

    worker_id: str
    pool: str
    #: 承载这个 worker 的协程。``None`` 表示还没拉起（或已经被回收）。
    task: "asyncio.Task | None" = None
    #: 正在处理的项；空闲时为 None。
    current: "QueueEntry | None" = None
    #: 当前项的开工时间（由池在认领时打）。池超时从这里起算，
    #: **不是从入队起算** —— 排队等待不算处理时间。
    started_at: float | None = None
    #: 最近一次心跳。见 :meth:`beat` 的说明：它衡量的是「池还在照看
    #: 这个槽位」，不是「底层线程还活着」。
    last_beat: float = field(default_factory=time.time)
    #: 被回收/重启过几次。正常运行时永远是 0。
    recycles: int = 0
    #: 第几代协程。回收一次 +1，编号不变 —— 运维看到的还是同一个槽位。
    generation: int = 1
    #: 累计处理过多少项 / 其中多少项以非预期方式结束。
    handled: int = 0
    failed: int = 0

    def state(self) -> str:
        return WORKER_BUSY if self.current is not None else WORKER_IDLE

    # -- 认领与释放 ----------------------------------------------------

    def claim(self, entry: "QueueEntry") -> None:
        self.current = entry
        self.started_at = time.time()
        entry.started_at = self.started_at
        self.beat()

    def release(self, *, failed: bool = False) -> None:
        self.handled += 1
        if failed:
            self.failed += 1
        self.current = None
        self.started_at = None
        self.beat()

    def beat(self) -> None:
        """记一次心跳。

        **它证明不了底层线程还活着** —— LibreOffice / OCR 跑在同步线程里，
        那段时间协程是挂起的，根本没机会调这里。所以这个字段的诚实含义是
        「池最后一次照看这个槽位是什么时候」。真正判断「卡死了没有」靠的是
        租约：见 ``WorkerPool`` 的看门狗。
        """
        self.last_beat = time.time()

    # -- 对外 ----------------------------------------------------------

    def busy_seconds(self, now: float | None = None) -> float | None:
        """这一项已经跑了多久（秒）；空闲时 None。"""
        if self.started_at is None:
            return None
        return max(0.0, (time.time() if now is None else now) - self.started_at)

    def snapshot(self) -> dict:
        """给 ``/api/system/workers`` 用的一行。

        **刻意不含任务号、组号、文件名**：``group_id`` 是
        ``/api/tasks/{group_id}`` 的凭据，而那个接口会返回文件名 ——
        把这个接口做成无身份信息的，泄露就无从谈起。
        运维要的信息（哪个槽位、在不在忙、忙了多久、回收过几次）一个不少。
        """
        busy = self.busy_seconds()
        return {
            "worker_id": self.worker_id,
            "pool": self.pool,
            "state": self.state(),
            "busy_seconds": round(busy, 1) if busy is not None else None,
            "handled": self.handled,
            "failed": self.failed,
            "recycles": self.recycles,
            "generation": self.generation,
        }
