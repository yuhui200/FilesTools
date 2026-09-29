"""资源池、超时组合、看门狗与自动重试（第八阶段 §八 ~ §二十一）。

这一层测试的是**架构行为**，不是「某个文件转得对不对」：

- 资源分类：一个任务项进哪个池，以及为什么；
- 超时组合：池兜底必须**不小于**池内既有操作的限额；
- 并发上限：每个池各管各的，一个池被拖住不影响别的池；
- 超时释放槽位：超时之后队列**不能**被永久堵住；
- 看门狗：连超时都不理的处理器会被回收，且槽位不会永久丢失；
- 自动重试：只重试服务器侧的偶发问题，且上限严格 ≤ 1；
- 优雅关机：在跑的项如实记为失败，绝不留下 processing。

全部直接构造 :class:`TaskQueue`，不经过 HTTP —— 状态机之外的这一层
值得被单独钉住。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from config import settings
from services import worker_pool as wp
from services.queue_service import (
    STATE_DONE,
    STATE_FAILED,
    STATE_PROCESSING,
    STATE_WAITING,
    TaskGroup,
    TaskItem,
    TaskQueue,
)
from services.worker_pool import (
    POOL_DEFAULT,
    POOL_IMAGE,
    POOL_OCR,
    POOL_OFFICE,
    POOL_PDF,
    WorkerPool,
    effective_timeout_seconds,
    pool_for,
    pool_sizes,
    source_timeouts,
    timeout_table,
)
from utils.errors import ErrorCode, TemporaryIoError

# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


async def _noop_finalizer(group: TaskGroup) -> None:
    return None


async def _ok_handler(item: TaskItem, group: TaskGroup) -> dict:
    return {"index": item.index}


def build(
    tmp: Path,
    group_id: str,
    count: int,
    handler,
    *,
    tool: str = "test",
    source_type: str = "",
    target_type: str = "",
    retry_enabled: bool = True,
) -> TaskGroup:
    """造一个批次：独占目录 + ``count`` 个项，**每一项的源文件真的写在磁盘上**。

    源文件必须真存在：自动重试的前置条件之一就是「原件还在」——
    原件没了的批次重来一遍也是白搭。用假路径会让重试测试测了个寂寞。
    """
    directory = tmp / group_id
    directory.mkdir(parents=True, exist_ok=True)
    items = []
    for index in range(count):
        source = directory / f"in{index}.bin"
        source.write_bytes(b"payload")
        items.append(
            TaskItem(
                index=index,
                filename=f"f{index}.bin",
                size=len(b"payload"),
                source=source,
                source_type=source_type,
                target_type=target_type,
            )
        )
    return TaskGroup(
        group_id=group_id,
        tool=tool,
        label="测试",
        directory=directory,
        items=items,
        handler=handler,
        finalizer=_noop_finalizer,
        retry_enabled=retry_enabled,
    )


def run(scenario) -> None:
    asyncio.run(scenario())


@asynccontextmanager
async def running_queue(*, grace: float = 0.0):
    """起一个**真的** TaskQueue，收尾时一定停掉（否则协程泄漏到下一个测试）。"""
    queue = TaskQueue()
    await queue.start()
    try:
        yield queue
    finally:
        await queue.stop(grace=grace)


async def wait_until(predicate, *, timeout: float = 15.0, interval: float = 0.01) -> bool:
    """轮询等待一个条件成立。返回是否在时限内成立。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def item_of(group: TaskGroup, index: int) -> TaskItem:
    return next(entry for entry in group.items if entry.index == index)


def pool_of(queue: TaskQueue, name: str) -> dict:
    return next(pool for pool in queue.pool_snapshot()["pools"] if pool["name"] == name)


# ----------------------------------------------------------------------
# 资源分类（§九）
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "source_type", "target_type", "expected"),
    [
        # 既有四个批量工具：没有 source_type，靠工具名定
        ("image.compress", "", "", POOL_IMAGE),
        ("image.convert", "", "", POOL_IMAGE),
        ("image.resize", "", "", POOL_IMAGE),
        ("pdf.to-images", "", "", POOL_PDF),
        # 统一转换中心：靠「从什么变成什么」定
        ("conversion.unified", "jpg", "png", POOL_IMAGE),
        ("conversion.unified", "webp", "pdf", POOL_IMAGE),
        ("conversion.unified", "docx", "pdf", POOL_OFFICE),
        ("conversion.unified", "pptx", "pdf", POOL_OFFICE),
        # TXT 走 PyMuPDF 排版，全程不碰 LibreOffice —— 归 image 而不是 office，
        # 否则一批 50 个 txt 会把所有 Office 转换堵在 OFFICE_WORKERS=1 后面
        ("conversion.unified", "txt", "pdf", POOL_IMAGE),
        # PDF → Word 是逐页 OCR，归 ocr
        ("conversion.unified", "pdf", "docx", POOL_OCR),
        # 认不出来的一律进收容池，语义与第四阶段完全一致
        ("test", "", "", POOL_DEFAULT),
        ("", "", "", POOL_DEFAULT),
        ("未知工具", "", "", POOL_DEFAULT),
    ],
)
def test_pool_for_classification(tool: str, source_type: str, target_type: str, expected: str) -> None:
    assert pool_for(tool=tool, source_type=source_type, target_type=target_type) == expected


def test_pdf_to_images_is_not_mistaken_for_pdf_to_word() -> None:
    """``pdf.to-images`` 处理的是 ``.pdf`` 输入，**不能**按格式被误判进 OCR 池。

    这条是分类顺序的守门测试：工具名比源格式更具体，必须先看工具名。
    """
    assert pool_for(tool="pdf.to-images", source_type="pdf", target_type="docx") == POOL_PDF


def test_declared_pool_is_really_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    """条目声明的池子必须是**首选**，不能只是碰巧和需求映射算出同一个答案。

    做法是把那条退路整条抽掉：如果 ``pool_for`` 其实只走需求映射，
    下面这次调用就会掉进 ``default`` 池，断言当场失败。
    """
    from conversion import registry

    monkeypatch.setattr(wp, "_REQUIREMENT_POOLS", {})
    assert pool_for(tool="conversion.unified", source_type="docx", target_type="pdf") == POOL_OFFICE
    assert pool_for(tool="conversion.unified", source_type="pdf", target_type="docx") == POOL_OCR
    assert pool_for(tool="conversion.unified", source_type="txt", target_type="pdf") == POOL_IMAGE
    assert registry.POOL_BY_SOURCE, "注册表必须真的有声明，否则上面几条是假的"


def test_requirement_fallback_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """反过来，注册表不认识这个源时，退路必须接得住。

    那个状态是真会出现的：词汇表（``SOURCE_TYPES`` / ``SOURCE_REQUIREMENTS``）
    先于条目表存在 —— 比如一种格式先被写进词汇表，条目下个提交才补上。
    这段时间里 ``pool_for`` 不能直接掉进 ``default`` 池。
    """
    from conversion import registry

    monkeypatch.setattr(registry, "POOL_BY_SOURCE", {})
    assert registry.pool_for_source("docx") is None
    assert pool_for(tool="conversion.unified", source_type="docx", target_type="pdf") == POOL_OFFICE
    assert pool_for(tool="conversion.unified", source_type="txt", target_type="pdf") == POOL_IMAGE


def test_unknown_source_is_not_reported_as_the_default_pool() -> None:
    """``pool_for_source`` 用 ``None`` 表示「不知道」，不是一个具体的池名。

    混成一个值的话，上面那条退路就永远轮不到 —— 而且 ``default`` 本身
    是个合法的池名，调用方分不出「我知道，用收容池」和「我不知道」。
    """
    from conversion import registry

    assert registry.pool_for_source("exe") is None
    assert registry.pool_for_source("") is None
    assert registry.pool_for_source(None) is None
    assert POOL_DEFAULT not in registry.POOL_BY_SOURCE.values()


def test_requirement_pools_cover_every_declared_requirement() -> None:
    """``REQUIREMENTS`` 里声明过的每一种组件需求都要有对应的池。

    少一项的后果不是报错，而是**静默掉进收容池**：一批本来该排在
    ``image`` 池（并发 2）的活跑到 ``default`` 池上，与别的杂活抢额度。
    """
    from conversion import capability

    assert set(wp._REQUIREMENT_POOLS) == set(capability.REQUIREMENTS)
    assert set(wp._REQUIREMENT_POOLS.values()) <= set(wp.POOL_ORDER)


def test_default_pool_matches_queue_workers() -> None:
    """``default`` 池的大小 == ``QUEUE_WORKERS`` —— 第四阶段的并发语义没变。"""
    assert pool_sizes()[POOL_DEFAULT] == max(1, settings.QUEUE_WORKERS)


def test_pool_sizes_sum_to_max_workers() -> None:
    """四个资源池之和 == ``MAX_WORKERS``（default 是额外的收容额度，不算进去）。"""
    sizes = pool_sizes()
    resources = sizes[POOL_IMAGE] + sizes[POOL_PDF] + sizes[POOL_OFFICE] + sizes[POOL_OCR]
    assert resources == settings.MAX_WORKERS


def test_every_pool_has_a_configured_size() -> None:
    assert set(pool_sizes()) == set(wp.POOL_ORDER)


# ----------------------------------------------------------------------
# 超时组合（§十六）
# ----------------------------------------------------------------------


def test_effective_timeout_is_max_of_pool_knob_and_existing_limits() -> None:
    """生效超时 = max(池兜底, 池内既有操作限额)。这张表是交付报告的一部分。"""
    assert effective_timeout_seconds(POOL_IMAGE) == 120.0
    assert effective_timeout_seconds(POOL_OFFICE) == 180.0
    assert effective_timeout_seconds(POOL_OCR) == 300.0
    assert effective_timeout_seconds(POOL_PDF) == 300.0
    assert effective_timeout_seconds(POOL_DEFAULT) == 300.0


def test_pool_timeout_never_undercuts_an_existing_operation() -> None:
    """池兜底**绝不能**比池内任何既有操作的限额更小。

    小了的后果不是「更安全」，而是把一次正常的 240 秒 PDF 拆分抢先判死，
    用户拿到笼统的「任务处理超时」，而不是诚实的「拆分超时，请减少页数」。
    """
    for row in timeout_table():
        for label, value in row["existing_timeouts"].items():
            assert row["effective_timeout_seconds"] >= value, (
                f"{row['pool']} 池的生效超时比 {label} 还小，会抢先误杀正常操作"
            )


def test_timeout_literals_match_their_real_constants() -> None:
    """只读副本必须与源常量一致。

    ``services/pdf_tools`` / ``tasks/pdf_tasks`` / ``routers/pdf`` 这三个模块
    没法在 import 期安全读进来（``tasks.pdf_tasks`` 会反过来 import 队列）。
    所以 ``worker_pool`` 里存的是副本，**由这条测试负责不让它漂移**。
    """
    from routers import pdf as pdf_router
    from services import pdf_tools
    from tasks import pdf_tasks

    modules = {
        "services.pdf_tools": pdf_tools,
        "tasks.pdf_tasks": pdf_tasks,
        "routers.pdf": pdf_router,
    }
    checked = 0
    for pool in wp.POOL_ORDER:
        for label, value in wp._SOURCE_TIMEOUT_LITERALS[pool]:
            module_name, attr = label.rsplit(".", 1)
            actual = getattr(modules[module_name], attr)
            assert actual == value, f"{label} 已经变成 {actual}，worker_pool 里的副本还是 {value}"
            checked += 1
    assert checked >= 7, "副本数量不对，说明有人删了条目而不是改了它"


def test_source_timeouts_names_are_readable() -> None:
    """审计表里每个来源名都要真的读得到 —— 写错一个字母就等于少算一项。"""
    for pool in wp.POOL_ORDER:
        for label, value in source_timeouts(pool):
            assert value > 0, f"{label} 不是正数"
            if label.startswith("settings."):
                assert hasattr(settings, label.split(".", 1)[1])
            else:
                # 形如 "services.pdf_tools.PDF_BUILD_TIMEOUT_SECONDS" ——
                # 至少要能拆出「模块」和「属性」两半
                module_name, _, attr = label.rpartition(".")
                assert module_name and attr


# ----------------------------------------------------------------------
# 池的基本行为
# ----------------------------------------------------------------------


def test_pool_starts_full_and_workers_are_named_globally() -> None:
    """池一创建就把 worker 拉满 —— 一个用 0 回答「并发上限是多少」的池是错的。"""

    async def scenario() -> None:
        async with running_queue() as queue:
            snapshot = queue.pool_snapshot()
            assert snapshot["accepting"] is True
            for pool in snapshot["pools"]:
                assert pool["started_workers"] == pool["configured_workers"]
            ids = [w["worker_id"] for pool in snapshot["pools"] for w in pool["workers"]]
            assert len(ids) == len(set(ids)), "worker 编号全局唯一，不能重号"
            assert all(wid.startswith("worker-") for wid in ids)
            assert all(w["state"] == "idle" for pool in snapshot["pools"] for w in pool["workers"])

    run(scenario)


def test_worker_count_semantics_unchanged() -> None:
    """``worker_count()`` 仍然是 ``QUEUE_WORKERS`` —— 既有测试的断言靠它。"""

    async def scenario() -> None:
        async with running_queue() as queue:
            assert queue.worker_count() == max(1, settings.QUEUE_WORKERS)

    run(scenario)


def test_unclassified_batch_runs_in_default_pool(tmp_path: Path) -> None:
    """``tool="test"`` 的批次走 default 池，并且真的跑完。"""

    async def scenario() -> None:
        async with running_queue() as queue:
            group = build(tmp_path, "g-default", 3, _ok_handler)
            await queue.submit(group)
            assert await wait_until(lambda: group.all_terminal())
            assert [item.state for item in group.items] == [STATE_DONE] * 3
            assert pool_of(queue, POOL_DEFAULT)["handled"] == 3

    run(scenario)


# ----------------------------------------------------------------------
# 并发上限与池间隔离（§十四）
# ----------------------------------------------------------------------


def test_image_pool_caps_concurrency_at_its_own_size(tmp_path: Path) -> None:
    """10 个图片任务，同时最多 ``IMAGE_WORKERS`` 个在跑 —— 而且要**真的**并发。"""

    async def scenario() -> None:
        peak = 0
        active = 0

        async def handler(item: TaskItem, group: TaskGroup) -> dict:
            nonlocal peak, active
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return {"index": item.index}

        async with running_queue() as queue:
            group = build(tmp_path, "g-image", 10, handler, tool="image.convert")
            await queue.submit(group)
            assert await wait_until(lambda: group.all_terminal(), timeout=20)

        assert peak == max(1, settings.IMAGE_WORKERS)
        assert peak >= 2, "IMAGE_WORKERS>1 时并发必须真的发生，否则分池毫无意义"

    run(scenario)


def test_office_pool_runs_one_at_a_time_but_still_finishes(tmp_path: Path) -> None:
    """Office 池固定 1 —— 这不是性能缺陷，是配置（LibreOffice 的闸门在更里面）。"""

    async def scenario() -> None:
        peak = 0
        active = 0

        async def handler(item: TaskItem, group: TaskGroup) -> dict:
            nonlocal peak, active
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return {"index": item.index}

        async with running_queue() as queue:
            group = build(tmp_path, "g-office", 4, handler, source_type="docx", target_type="pdf", tool="conversion.unified")
            await queue.submit(group)
            assert await wait_until(lambda: group.all_terminal(), timeout=20)

        assert peak == 1

    run(scenario)


def test_a_slow_pool_does_not_hold_up_a_fast_one(tmp_path: Path) -> None:
    """**这就是第八阶段存在的理由。**

    一批慢 Office 还在跑的时候，图片批次必须能照常跑完 ——
    在第四阶段的单队列 FIFO 里，它们只能排在 Office 后面干等。
    """

    async def scenario() -> None:
        office_started = asyncio.Event()

        async def slow_office(item: TaskItem, group: TaskGroup) -> dict:
            office_started.set()
            await asyncio.sleep(0.6)
            return {"index": item.index}

        async def fast_image(item: TaskItem, group: TaskGroup) -> dict:
            await asyncio.sleep(0.01)
            return {"index": item.index}

        async with running_queue() as queue:
            office = build(tmp_path, "g-slow", 3, slow_office, source_type="docx", target_type="pdf", tool="conversion.unified")
            images = build(tmp_path, "g-fast", 3, fast_image, tool="image.convert")
            # 先提交慢的：FIFO 下它一定会排在前面
            await queue.submit(office)
            await queue.submit(images)
            await asyncio.wait_for(office_started.wait(), timeout=5)

            # 慢批次还在跑的时候，快批次应该已经全部完成
            assert await wait_until(lambda: images.all_terminal(), timeout=5), (
                "图片批次被 Office 批次挡住了 —— 队头阻塞又回来了"
            )
            assert not office.all_terminal(), "Office 批次不该这么快就结束（睡眠时间不够）"

            assert await wait_until(lambda: office.all_terminal(), timeout=20)

        assert [item.state for item in images.items] == [STATE_DONE] * 3

    run(scenario)


def test_three_pools_run_at_the_same_time(tmp_path: Path) -> None:
    """三个池同时都有项在跑 —— 池之间不共享槽位。"""

    async def scenario() -> None:
        barrier = asyncio.Event()
        seen: set[str] = set()

        def make_handler(pool: str):
            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                seen.add(pool)
                if len(seen) == 3:
                    barrier.set()
                # 三个都在跑之后才一起放行
                await asyncio.wait_for(barrier.wait(), timeout=10)
                return {"index": item.index}

            return handler

        async with running_queue() as queue:
            groups = [
                build(tmp_path, "g-img", 1, make_handler(POOL_IMAGE), tool="image.compress"),
                build(
                    tmp_path,
                    "g-off",
                    1,
                    make_handler(POOL_OFFICE),
                    tool="conversion.unified",
                    source_type="docx",
                    target_type="pdf",
                ),
                build(
                    tmp_path,
                    "g-ocr",
                    1,
                    make_handler(POOL_OCR),
                    tool="conversion.unified",
                    source_type="pdf",
                    target_type="docx",
                ),
            ]
            for group in groups:
                await queue.submit(group)
            assert await wait_until(
                lambda: all(group.all_terminal() for group in groups), timeout=20
            ), "三个池没能同时在跑 —— 说明它们其实在共享槽位"

        assert seen == {POOL_IMAGE, POOL_OFFICE, POOL_OCR}

    run(scenario)


# ----------------------------------------------------------------------
# 超时与槽位释放（§十七）
# ----------------------------------------------------------------------


def test_timeout_releases_the_slot_and_keeps_the_queue_moving(monkeypatch, tmp_path: Path) -> None:
    """超时之后槽位必须**立刻**释放，队列不能被永久堵住。

    把 image 池压到 1 个槽位、超时压到 1 秒：如果超时不释放槽位，
    排在后面的那一项永远轮不到，这条测试会超时失败。
    """
    monkeypatch.setattr(settings, "IMAGE_WORKERS", 1)
    monkeypatch.setattr(settings, "IMAGE_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "PROCESS_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "TXT_CONVERT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "TASK_TIMEOUT_AUTO_RETRY", False)

    async def scenario() -> None:
        async def hang(item: TaskItem, group: TaskGroup) -> dict:
            await asyncio.sleep(3600)
            return {}

        async def quick(item: TaskItem, group: TaskGroup) -> dict:
            return {"index": item.index}

        async with running_queue() as queue:
            # image 池被压到 1 个槽位 —— 这一条测试的整个前提
            assert pool_of(queue, POOL_IMAGE)["started_workers"] == 1
            stuck = build(tmp_path, "g-stuck", 1, hang, tool="image.convert")
            after = build(tmp_path, "g-after", 1, quick, tool="image.convert")
            await queue.submit(stuck)
            await queue.submit(after)

            assert await wait_until(lambda: stuck.all_terminal(), timeout=10)
            assert await wait_until(lambda: after.all_terminal(), timeout=10), (
                "超时没有释放槽位，后面的任务被永久堵住了"
            )

        timed_out = stuck.items[0]
        assert timed_out.state == STATE_FAILED
        assert timed_out.error_code == ErrorCode.TASK_TIMEOUT
        # 诚实：超时只请求了放弃，底层线程停不下来
        assert timed_out.timeout_requested is True
        assert after.items[0].state == STATE_DONE

    run(scenario)


def test_timeout_is_not_auto_retried_by_default(monkeypatch, tmp_path: Path) -> None:
    """``TASK_TIMEOUT`` 在可重试类里，但默认被 ``TASK_TIMEOUT_AUTO_RETRY`` 挡住。

    一个已经跑满 300 秒还没完的文件，再排一次队多半还是 300 秒。
    """
    monkeypatch.setattr(settings, "IMAGE_WORKERS", 1)
    monkeypatch.setattr(settings, "IMAGE_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "PROCESS_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "TXT_CONVERT_TIMEOUT_SECONDS", 1)

    async def scenario() -> None:
        async def hang(item: TaskItem, group: TaskGroup) -> dict:
            await asyncio.sleep(3600)
            return {}

        async with running_queue() as queue:
            group = build(tmp_path, "g-t", 1, hang, tool="image.convert")
            await queue.submit(group)
            assert await wait_until(lambda: group.all_terminal(), timeout=10)

        assert group.items[0].auto_retry_count == 0

    run(scenario)


# ----------------------------------------------------------------------
# 看门狗（§十九）
# ----------------------------------------------------------------------


def test_watchdog_recycles_a_worker_that_ignores_cancellation(monkeypatch, tmp_path: Path) -> None:
    """连超时都不理的处理器 → worker 被回收，项记 WORKER_LOST，池之后仍可用。

    这是「没有任何项会永远停在 processing」与「槽位不会永久丢失」的
    唯一硬证据：处理器故意吞掉 ``CancelledError``，模拟杀不掉的线程。
    """
    monkeypatch.setattr(settings, "IMAGE_WORKERS", 1)
    monkeypatch.setattr(settings, "IMAGE_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "PROCESS_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "TXT_CONVERT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "WORKER_LOST_GRACE_SECONDS", 0)
    monkeypatch.setattr(settings, "WORKER_WATCHDOG_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(settings, "TASK_MAX_AUTO_RETRIES", 0)

    async def scenario() -> None:
        release = asyncio.Event()

        async def unkillable(item: TaskItem, group: TaskGroup) -> dict:
            # 吞掉取消：模拟跑在同步线程里、杀不掉的 LibreOffice / OCR
            while not release.is_set():
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    continue
            return {}

        async def quick(item: TaskItem, group: TaskGroup) -> dict:
            return {"index": item.index}

        async with running_queue() as queue:
            try:
                assert pool_of(queue, POOL_IMAGE)["started_workers"] == 1
                stuck = build(tmp_path, "g-unkillable", 1, unkillable, tool="image.convert")
                await queue.submit(stuck)

                # 项必须落定，绝不留在 processing
                assert await wait_until(lambda: stuck.all_terminal(), timeout=15), (
                    "卡死的任务一直没有落定 —— 它会永远停在 processing"
                )
                pool = pool_of(queue, POOL_IMAGE)
                assert await wait_until(
                    lambda: pool_of(queue, POOL_IMAGE)["workers"][0]["recycles"] >= 1,
                    timeout=10,
                ), "看门狗没有回收卡死的 worker"

                # 槽位没有永久丢失：池还能继续干活
                after = build(tmp_path, "g-after-recycle", 1, quick, tool="image.convert")
                await queue.submit(after)
                assert await wait_until(lambda: after.all_terminal(), timeout=10), (
                    "回收之后槽位没有补回来，池永久少了一个 worker"
                )
            finally:
                # 无论断言是否通过都要放行：这个处理器故意不理会取消，
                # 不放行的话收尾时它会一直挂在事件循环上。
                release.set()

        lost = stuck.items[0]
        assert lost.state == STATE_FAILED
        assert lost.error_code == ErrorCode.WORKER_LOST
        assert pool["workers"][0]["generation"] >= 2, "回收后编号不变、代次应该 +1"

    run(scenario)


def test_a_cancelled_worker_coroutine_is_not_lost_forever(monkeypatch, tmp_path: Path) -> None:
    """一次**外来**的取消不该让槽位永久消失（第四阶段那个真实缺陷）。

    第四阶段的实现在这里无条件 re-raise ``CancelledError``，一条 worker 就此
    少掉、再也没人补。现在它继续取件，池的并发上限不缩水。
    """

    async def scenario() -> None:
        async with running_queue() as queue:
            before = queue.worker_count()
            pool = queue._pools.pool(POOL_DEFAULT)
            victim = pool._workers[0]
            # 先让协程真的跑起来、挂到取件上再取消它 —— 对一个**还没开始执行**
            # 的任务调 cancel()，任务会直接死掉（协程一步都没进去），
            # 那是另一回事，测不到「取件中途被取消」这条路径。
            await asyncio.sleep(0.05)
            assert not victim.task.done(), "worker 应该已经挂到取件上了"

            victim.task.cancel()
            await asyncio.sleep(0.1)

            assert queue.worker_count() == before, "worker 数量缩水了"
            assert victim.task is not None and not victim.task.done(), (
                "被取消的 worker 应该继续取件，而不是死掉"
            )

            group = build(tmp_path, "g-after-cancel", 4, _ok_handler)
            await queue.submit(group)
            assert await wait_until(lambda: group.all_terminal(), timeout=10)

    run(scenario)


def test_watchdog_respawns_a_dead_coroutine(monkeypatch) -> None:
    """协程真的死了（``task.done()``）→ 看门狗把它重新拉起来，编号不变。"""
    monkeypatch.setattr(settings, "WORKER_WATCHDOG_INTERVAL_SECONDS", 0.1)

    async def scenario() -> None:
        seen: list[str] = []

        async def runner(entry) -> None:
            seen.append(entry.group_id)

        async def on_lost(entry, reason: str) -> None:
            return None

        pool = WorkerPool(
            POOL_IMAGE, size=2, runner=runner, on_lost=on_lost, watchdog_interval=0.1
        )
        pool.start()
        try:
            victim = pool._workers[0]
            victim_id = victim.worker_id
            # 把它的协程换成一个立刻结束的任务，模拟「协程死了」
            victim.task.cancel()
            await asyncio.sleep(0)
            for _ in range(50):
                if victim.task.done():
                    break
                await asyncio.sleep(0.02)

            assert await wait_until(
                lambda: any(
                    w.worker_id == victim_id and w.generation >= 2 for w in pool._workers
                ),
                timeout=5,
            ), "死掉的协程没有被重新拉起"
            assert pool.worker_count() == 2, "回收后槽位数必须补回配置值"
        finally:
            await pool.stop()

    run(scenario)


# ----------------------------------------------------------------------
# 自动重试（§二十 / §二十一）
# ----------------------------------------------------------------------


def test_retryable_failure_is_retried_once_and_then_succeeds(monkeypatch, tmp_path: Path) -> None:
    """一次服务器侧的偶发失败 → 服务器自己重排一次 → 第二次成功。"""
    monkeypatch.setattr(settings, "TASK_MAX_AUTO_RETRIES", 1)

    async def scenario() -> None:
        attempts = 0

        async def flaky(item: TaskItem, group: TaskGroup) -> dict:
            nonlocal attempts
            attempts += 1
            if item.auto_retry_count == 0:
                raise TemporaryIoError("临时目录一时写不进去")
            return {"index": item.index}

        async with running_queue() as queue:
            group = build(tmp_path, "g-flaky", 1, flaky)
            await queue.submit(group)
            assert await wait_until(lambda: group.all_terminal(), timeout=10)

        item = group.items[0]
        assert attempts == 2
        assert item.state == STATE_DONE
        assert item.auto_retry_count == 1
        # **手动重试计数一个字都没变** —— 前端「已重试过」的文案靠它
        assert item.retry_count == 0
        assert group.state == STATE_DONE

    run(scenario)


def test_auto_retry_never_becomes_an_infinite_loop(monkeypatch, tmp_path: Path) -> None:
    """一直失败的可重试错误，最终也必须停下来（上限严格 ≤ 1）。"""
    monkeypatch.setattr(settings, "TASK_MAX_AUTO_RETRIES", 1)

    async def scenario() -> None:
        attempts = 0

        async def always_fail(item: TaskItem, group: TaskGroup) -> dict:
            nonlocal attempts
            attempts += 1
            raise TemporaryIoError("临时目录一时写不进去")

        async with running_queue() as queue:
            group = build(tmp_path, "g-forever", 1, always_fail)
            await queue.submit(group)
            assert await wait_until(lambda: group.all_terminal(), timeout=10)

        item = group.items[0]
        assert attempts == 2, "自动重试只允许一次，加上首次执行一共两次"
        assert item.auto_retry_count == 1
        assert item.state == STATE_FAILED
        assert item.error_code == ErrorCode.TEMPORARY_IO_ERROR
        assert group.state == STATE_FAILED

    run(scenario)


def test_non_retryable_failure_is_not_retried(monkeypatch, tmp_path: Path) -> None:
    """文件本身有问题的失败，重试一百次结果都一样，一次都不该重排。"""
    monkeypatch.setattr(settings, "TASK_MAX_AUTO_RETRIES", 3)

    async def scenario() -> None:
        attempts = 0

        async def bad_file(item: TaskItem, group: TaskGroup) -> dict:
            nonlocal attempts
            attempts += 1
            from utils.errors import CorruptedFileError

            raise CorruptedFileError("文件已损坏")

        async with running_queue() as queue:
            group = build(tmp_path, "g-bad", 1, bad_file)
            await queue.submit(group)
            assert await wait_until(lambda: group.all_terminal(), timeout=10)

        assert attempts == 1
        assert group.items[0].auto_retry_count == 0
        assert group.items[0].error_code == ErrorCode.CORRUPTED_FILE

    run(scenario)


def test_auto_retry_is_refused_after_the_user_cancels(monkeypatch, tmp_path: Path) -> None:
    """用户已经不要这一批了，服务器不该自作主张再排一次。"""
    monkeypatch.setattr(settings, "TASK_MAX_AUTO_RETRIES", 1)

    async def scenario() -> None:
        async def fail(item: TaskItem, group: TaskGroup) -> dict:
            raise TemporaryIoError("临时目录一时写不进去")

        async with running_queue() as queue:
            group = build(tmp_path, "g-cancelled", 1, fail)
            group.cancel_requested = True
            await queue.submit(group)
            assert await wait_until(lambda: group.all_terminal(), timeout=10)

        assert group.items[0].auto_retry_count == 0

    run(scenario)


def test_watchdog_abandoned_item_is_auto_retried(monkeypatch, tmp_path: Path) -> None:
    """``WORKER_LOST`` 是可重试类：被判失联的项会由服务器重排一次。"""
    monkeypatch.setattr(settings, "IMAGE_WORKERS", 1)
    monkeypatch.setattr(settings, "IMAGE_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "PROCESS_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "TXT_CONVERT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "WORKER_LOST_GRACE_SECONDS", 0)
    monkeypatch.setattr(settings, "WORKER_WATCHDOG_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(settings, "TASK_MAX_AUTO_RETRIES", 1)

    async def scenario() -> None:
        release = asyncio.Event()

        async def unkillable(item: TaskItem, group: TaskGroup) -> dict:
            while not release.is_set():
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    continue
            return {"index": item.index}

        async with running_queue() as queue:
            try:
                group = build(tmp_path, "g-lost-retry", 1, unkillable, tool="image.convert")
                await queue.submit(group)
                # 第一次被判失联之后应该被重排（auto_retry_count 到 1），
                # 第二次仍然卡死，但上限已到，于是落在 failed
                assert await wait_until(
                    lambda: group.items[0].auto_retry_count >= 1, timeout=15
                ), "WORKER_LOST 没有被自动重排"
                assert await wait_until(lambda: group.all_terminal(), timeout=15)
            finally:
                release.set()

        item = group.items[0]
        assert item.state == STATE_FAILED
        assert item.error_code == ErrorCode.WORKER_LOST

    run(scenario)


# ----------------------------------------------------------------------
# 优雅关机（§二十九 / §三十）
# ----------------------------------------------------------------------


def test_shutdown_settles_running_items_instead_of_leaving_them_processing(tmp_path: Path) -> None:
    """宽限期到了还在跑的项，必须**如实记成失败**，绝不留在 processing。"""

    async def scenario() -> None:
        async def hang(item: TaskItem, group: TaskGroup) -> dict:
            await asyncio.sleep(3600)
            return {}

        queue = TaskQueue()
        await queue.start()
        group = build(tmp_path, "g-shutdown", 2, hang)
        await queue.submit(group)
        assert await wait_until(
            lambda: any(item.state == STATE_PROCESSING for item in group.items), timeout=5
        )

        await queue.stop(grace=0.2)

        assert group.items[0].state == STATE_FAILED
        assert group.items[0].error_code == ErrorCode.WORKER_LOST
        assert "重启" in (group.items[0].error_message or "")
        assert not any(item.state == STATE_PROCESSING for item in group.items)

    run(scenario)


def test_shutdown_on_an_idle_queue_does_not_wait() -> None:
    """空载时**零等待** —— 否则每个测试会话收尾都要白等 10 秒。"""

    async def scenario() -> None:
        queue = TaskQueue()
        await queue.start()
        started = time.monotonic()
        await queue.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"空载关个机等了 {elapsed:.1f} 秒"

    run(scenario)


def test_shutdown_lets_a_short_item_finish(tmp_path: Path) -> None:
    """宽限期内跑得完的项，应该正常收尾，而不是被记成失败。"""

    async def scenario() -> None:
        async def brief(item: TaskItem, group: TaskGroup) -> dict:
            await asyncio.sleep(0.2)
            return {"index": item.index}

        queue = TaskQueue()
        await queue.start()
        group = build(tmp_path, "g-grace", 1, brief)
        await queue.submit(group)
        assert await wait_until(
            lambda: group.items[0].state == STATE_PROCESSING, timeout=5
        )

        await queue.stop(grace=5)

        assert group.items[0].state == STATE_DONE

    run(scenario)


def test_submit_after_stop_is_refused(tmp_path: Path) -> None:
    """停掉之后不再接新活 —— 否则会有项排进一个永远不会被消费的队列。"""

    async def scenario() -> None:
        from utils.errors import ProcessingError

        queue = TaskQueue()
        await queue.start()
        await queue.stop()
        group = build(tmp_path, "g-late", 1, _ok_handler)
        with pytest.raises(ProcessingError):
            await queue.submit(group)

    run(scenario)


def test_double_start_and_double_stop_are_harmless() -> None:
    """重复 start / stop 不该把池搞乱（lifespan 与测试都可能碰它）。"""

    async def scenario() -> None:
        queue = TaskQueue()
        await queue.start()
        await queue.start()
        assert queue.worker_count() == max(1, settings.QUEUE_WORKERS)
        await queue.stop()
        await queue.stop()
        assert queue.worker_count() == 0

    run(scenario)
