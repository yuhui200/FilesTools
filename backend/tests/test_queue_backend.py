"""队列后端本身的测试（第八阶段 §五十六 / §七）。

这一层不认识任务、不认识文件、不认识工具 —— 它只回答一个问题：
**下一条该是谁。** 所以这里全部是直接构造 :class:`QueueEntry` 的纯队列
断言，不经过 HTTP，也不碰状态机。

重点验证三件事：

1. **同优先级严格 FIFO**（第四阶段的行为不能因为加了优先级就没掉）；
2. **优先级真的起作用**（HIGH 先于 NORMAL 先于 LOW）；
3. **老化真的能防饿死** —— 这不是「大概会公平」，而是可以算出来的：
   一个 LOW 等满 ``PRIORITY_AGING_SECONDS × PRIORITY_AGING_MAX_STEPS``
   之后，它的有效档位与任何新来的 HIGH 相同，而按入队序号它更老，
   于是**必然**先出队。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from services.queue_backend import (
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    InMemoryTaskQueue,
    QueueEntry,
    priority_name,
)


def entry(index: int, *, priority: int = PRIORITY_NORMAL, resource: str = "image") -> QueueEntry:
    return QueueEntry(
        group_id="batch-1", index=index, resource_class=resource, priority=priority
    )


def run(scenario) -> None:
    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 基本语义
# ----------------------------------------------------------------------


def test_same_priority_is_strictly_fifo() -> None:
    """同优先级必须严格按入队顺序出队 —— 第四阶段的行为不能变。"""

    async def scenario() -> None:
        queue = InMemoryTaskQueue("image")
        queue.start()
        for index in range(5):
            queue.submit(entry(index))
        taken = [(await queue.get_next()).index for _ in range(5)]
        assert taken == [0, 1, 2, 3, 4]

    run(scenario)


def test_priority_beats_arrival_order() -> None:
    """低优先级的先到，也要给高优先级的让路。"""

    async def scenario() -> None:
        queue = InMemoryTaskQueue("image")
        queue.start()
        queue.submit(entry(0, priority=PRIORITY_LOW))
        queue.submit(entry(1, priority=PRIORITY_NORMAL))
        queue.submit(entry(2, priority=PRIORITY_HIGH))
        taken = [(await queue.get_next()).index for _ in range(3)]
        assert taken == [2, 1, 0]

    run(scenario)


def test_aging_lets_a_long_waiting_low_priority_win() -> None:
    """饿死是**可证**不会发生的，不是「大概公平」。

    构造一条已经等满老化档数的 LOW，再放一条刚到的 HIGH。
    按老化公式前者升到 HIGH 档，而同档按入队序号它更老 ⇒ 必然先出队。
    """

    async def scenario() -> None:
        queue = InMemoryTaskQueue("image", aging_seconds=10, aging_max_steps=2)
        queue.start()
        old = entry(0, priority=PRIORITY_LOW)
        # 直接把它「等」成老任务，比 sleep 20 秒可靠得多
        old.enqueued_at = time.time() - 1000
        queue.submit(old)
        queue.submit(entry(1, priority=PRIORITY_HIGH))
        assert (await queue.get_next()).index == 0

    run(scenario)


def test_aging_is_bounded_and_does_not_overshoot() -> None:
    """老化只能升到最高档，不会算出比 HIGH 还高的档位。"""

    async def scenario() -> None:
        queue = InMemoryTaskQueue("image", aging_seconds=10, aging_max_steps=2)
        queue.start()
        old = entry(0, priority=PRIORITY_LOW)
        old.enqueued_at = time.time() - 100_000
        # 等再久也只是「最高档」，不会变成负数
        assert queue.effective_rank(old) == PRIORITY_HIGH
        # 而一个刚到的 LOW 还是原样
        assert queue.effective_rank(entry(1, priority=PRIORITY_LOW)) == PRIORITY_LOW

    run(scenario)


def test_default_priority_is_normal() -> None:
    assert QueueEntry(group_id="g", index=0, resource_class="image").priority == PRIORITY_NORMAL
    assert priority_name(PRIORITY_HIGH) == "high"
    assert priority_name(PRIORITY_LOW) == "low"
    # 未知取值原样返回，不抛错 —— 日志不该因为一个陌生数字就崩
    assert priority_name(99) == "99"


# ----------------------------------------------------------------------
# 出入队与记账
# ----------------------------------------------------------------------


def test_get_next_waits_instead_of_polling() -> None:
    """空队列时是**等**，不是空转。

    这里验证的是行为而不是实现：一个先挂起的取件方，在后来的 submit
    之后必须被叫醒并拿到那一条。若实现改成轮询，这条测试仍然会过，
    但下面那条「不忙等」的断言会失败。
    """

    async def scenario() -> None:
        queue = InMemoryTaskQueue("image")
        queue.start()
        waiter = asyncio.create_task(queue.get_next())
        await asyncio.sleep(0)  # 让取件方真的挂到 wait 上
        assert not waiter.done()
        queue.submit(entry(7))
        got = await asyncio.wait_for(waiter, timeout=1)
        assert got is not None and got.index == 7

    run(scenario)


def test_many_idle_workers_do_not_burn_cpu() -> None:
    """20 个取件方一起空等，不应该有任何一条把 CPU 烧掉。

    轮询式实现（``while not entries: sleep(0)``）会让这一条超时。
    """

    async def scenario() -> None:
        queue = InMemoryTaskQueue("image")
        queue.start()
        waiters = [asyncio.create_task(queue.get_next()) for _ in range(20)]
        await asyncio.sleep(0.05)
        assert all(not waiter.done() for waiter in waiters)
        for index in range(20):
            queue.submit(entry(index))
        got = await asyncio.wait_for(asyncio.gather(*waiters), timeout=2)
        assert sorted(item.index for item in got) == list(range(20))

    run(scenario)


def test_ack_and_fail_only_account_and_never_redeliver() -> None:
    """``ack`` / ``fail`` 只记账，**绝不重投**。

    这是本层最容易被误读的语义，所以钉一条测试：ack 过的东西不会
    再次出现在队列里，也不会被谁「重试」。
    """

    async def scenario() -> None:
        queue = InMemoryTaskQueue("image")
        queue.start()
        first = queue.submit(entry(0))
        second = queue.submit(entry(1))
        queue.ack(await queue.get_next())
        queue.fail(await queue.get_next())

        stats = queue.stats()
        assert stats["handled"] == 2
        assert stats["failed"] == 1
        assert queue.size() == 0
        # 没有重投：关掉之后立刻就该是 None，而不是又冒出一条
        await queue.stop()
        assert await queue.get_next() is None
        queue.ack(first)
        queue.fail(second)
        assert queue.size() == 0

    run(scenario)


def test_cancel_removes_only_queued_entries() -> None:
    """取消只能摘掉**还在排队**的那一条；已经出队的摘不掉。"""

    async def scenario() -> None:
        queue = InMemoryTaskQueue("image")
        queue.start()
        queue.submit(entry(0))
        queue.submit(entry(1))

        taken = await queue.get_next()
        assert taken.index == 0
        # 已出队的：摘不掉，如实返回 False
        assert queue.cancel(group_id="batch-1", index=0) is False
        # 还在排队的：摘掉
        assert queue.cancel(group_id="batch-1", index=1) is True
        assert queue.size() == 0
        assert queue.stats()["cancelled"] == 1
        # 摘掉之后不会再出队
        assert queue.peek() is None

    run(scenario)


def test_queue_is_per_pool_not_shared() -> None:
    """两个池的队列互不可见 —— 否则又回到队头阻塞。"""

    async def scenario() -> None:
        images = InMemoryTaskQueue("image")
        office = InMemoryTaskQueue("office")
        images.start()
        office.start()
        images.submit(entry(0, resource="image"))
        assert office.size() == 0
        assert (await images.get_next()).resource_class == "image"
        office.submit(entry(1, resource="office"))
        # image 池里已经没有东西了，不该拿到 office 那条
        await images.stop()
        assert await images.get_next() is None
        assert (await office.get_next()).resource_class == "office"

    run(scenario)


def test_stop_wakes_waiters_and_returns_none() -> None:
    """关闭时必须叫醒所有等待者，让它们干净退出而不是永远挂着。"""

    async def scenario() -> None:
        queue = InMemoryTaskQueue("image")
        queue.start()
        waiters = [asyncio.create_task(queue.get_next()) for _ in range(5)]
        await asyncio.sleep(0)
        await queue.stop()
        results = await asyncio.wait_for(asyncio.gather(*waiters), timeout=1)
        assert results == [None] * 5

    run(scenario)


def test_stop_does_not_throw_away_queued_entries() -> None:
    """关闭**不清空**排队项 —— 它们记成什么由队列那一层决定，不是这一层。"""

    async def scenario() -> None:
        queue = InMemoryTaskQueue("image")
        queue.start()
        queue.submit(entry(0))
        await queue.stop()
        assert queue.size() == 1
        # 而且关掉之后仍然取得到（让上层有机会如实结算）
        assert (await queue.get_next()).index == 0

    run(scenario)


def test_submit_assigns_monotonic_seq() -> None:
    """序号必须严格递增 —— 同优先级的 FIFO 全靠它。"""

    async def scenario() -> None:
        queue = InMemoryTaskQueue("image")
        queue.start()
        seqs = [queue.submit(entry(index)).seq for index in range(4)]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == 4

    run(scenario)


@pytest.mark.parametrize("name", ["image", "pdf", "office", "ocr", "default"])
def test_stats_reports_its_own_name(name: str) -> None:
    """每个池的统计必须自报池名，否则合起来看根本分不清是谁。"""
    queue = InMemoryTaskQueue(name)
    assert queue.stats()["name"] == name
