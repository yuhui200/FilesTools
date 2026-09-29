"""任务队列本身的测试（第四阶段 §3 / §4 / §7 / §14 / §19）。

这里关心的是「队列的行为」，不是某个工具的结果对不对：

- 每个文件独立的状态与失败原因（单个失败不影响整批）
- 进度数字与真实处理进度自洽，没有假进度
- 并发被限制住，不会一次性把所有文件塞给 CPU
- 任务过期与临时目录清理
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from config import settings
from services.job_store import Job, job_store
from services.worker_pool import POOL_DEFAULT, pool_sizes
from services.queue_service import (
    MAX_ITEM_RETRIES,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_LABELS,
    STATE_PROCESSING,
    STATE_WAITING,
    TERMINAL_STATES,
    TaskGroup,
    TaskItem,
    TaskQueue,
    task_queue,
)
from utils.errors import (
    ErrorCode,
    ProcessingError,
    TaskNotFoundError,
    TaskNotRetryableError,
)
from utils.files import remove_dir
from tests.conftest import (
    build_image_bytes,
    build_pdf_bytes,
    image_files,
    pdf_files,
    submit_task,
    wait_for_task,
)

CONVERT = "/api/image/convert"
COMPRESS = "/api/image/compress"
TO_IMAGES = "/api/pdf/to-images"


def temp_dirs() -> set[Path]:
    root = Path(settings.TEMP_ROOT) if settings.TEMP_ROOT else Path(tempfile.gettempdir())
    if not root.is_dir():
        return set()
    return {item for item in root.iterdir() if item.is_dir()}


async def _noop_handler(item: TaskItem, group: TaskGroup) -> dict:
    return {}


async def _noop_finalizer(group: TaskGroup) -> None:
    return None


def _make_group(
    group_id: str,
    items: list[TaskItem],
    handler,
    *,
    directory: Path,
    finalizer=_noop_finalizer,
    retry_enabled: bool = False,
) -> TaskGroup:
    return TaskGroup(
        group_id=group_id,
        tool="test",
        label="测试",
        directory=directory,
        items=items,
        handler=handler,
        finalizer=finalizer,
        retry_enabled=retry_enabled,
    )


# ----------------------------------------------------------------------
# 进度与状态（§3 / §4）
# ----------------------------------------------------------------------

def test_snapshot_reports_every_file_state(client: TestClient) -> None:
    """每个文件都要有自己的名称、大小、状态与结果。"""
    images = [
        ("one.jpg", build_image_bytes(200, 200, "JPEG")),
        ("two.jpg", build_image_bytes(200, 200, "JPEG")),
        ("three.jpg", build_image_bytes(200, 200, "JPEG")),
    ]
    response = submit_task(
        client, CONVERT, files=image_files(*images), data={"target_format": "png"}
    )
    assert response.status_code == 202

    snapshot = wait_for_task(client, response.json()["group_id"])
    assert snapshot["state"] == STATE_DONE
    assert snapshot["total"] == 3
    assert snapshot["completed"] == 3
    assert snapshot["failed"] == 0
    assert snapshot["waiting"] == 0
    assert snapshot["processing"] == 0
    assert snapshot["finished"] == 3
    assert snapshot["percent"] == 100.0

    assert [task["filename"] for task in snapshot["tasks"]] == [
        "one.jpg",
        "two.jpg",
        "three.jpg",
    ]
    for task in snapshot["tasks"]:
        assert task["state"] == STATE_DONE
        assert task["state_label"] == "已完成"
        assert task["size"] > 0
        assert task["result"]["result"]["format"] == "png"
        assert task["error_code"] is None


def test_progress_matches_the_numbers(client: TestClient) -> None:
    """进度百分比必须与「已完成 + 失败 / 总数」自洽。"""
    upload = client.post(
        "/api/pdf/upload", files=pdf_files(("doc.pdf", build_pdf_bytes(3)), field="file")
    )
    input_id = upload.json()["input_id"]

    submitted = client.post(TO_IMAGES, data={"input_ids": input_id, "pages": "all"})
    assert submitted.status_code == 202
    group_id = submitted.json()["group_id"]

    first = client.get(f"/api/tasks/{group_id}").json()
    assert first["state"] in (STATE_WAITING, STATE_PROCESSING, STATE_DONE)
    assert first["percent"] == pytest.approx(
        (first["completed"] + first["failed"]) / first["total"] * 100, abs=0.1
    )
    assert first["percent"] <= 100.0

    final = wait_for_task(client, group_id)
    assert final["percent"] == 100.0


def test_single_failure_does_not_break_the_batch(client: TestClient) -> None:
    """一张坏图只让它自己失败，同批其它文件照常出结果（§4）。"""
    images = [
        ("good1.jpg", build_image_bytes(200, 200, "JPEG")),
        ("broken.jpg", b"\xff\xd8\xff" + b"\x00" * 500),
        ("good2.jpg", build_image_bytes(200, 200, "JPEG")),
    ]
    response = submit_task(
        client, CONVERT, files=image_files(*images), data={"target_format": "png"}
    )
    snapshot = wait_for_task(client, response.json()["group_id"])

    assert snapshot["state"] == STATE_DONE
    assert snapshot["completed"] == 2
    assert snapshot["failed"] == 1

    states = {task["filename"]: task["state"] for task in snapshot["tasks"]}
    assert states["good1.jpg"] == STATE_DONE
    assert states["good2.jpg"] == STATE_DONE
    assert states["broken.jpg"] == STATE_FAILED

    broken = next(task for task in snapshot["tasks"] if task["filename"] == "broken.jpg")
    assert broken["error_code"] in ("CORRUPTED_FILE", "INVALID_FILE_TYPE")
    assert broken["error_message"], "失败的文件必须给出原因"
    assert "Traceback" not in broken["error_message"]

    # 成功的那两张仍然可以下载，且 ZIP 里只有它们
    download = client.get(snapshot["result"]["download_url"])
    assert download.status_code == 200
    assert snapshot["result"]["archived"] is True
    assert sorted(item["result"]["filename"] for item in snapshot["result"]["items"]) == [
        "good1.png",
        "good2.png",
    ]


def test_all_failed_batch_reports_first_reason(client: TestClient) -> None:
    """全部失败时整批标记失败，并原样给出第一个失败原因。"""
    images = [("bad1.jpg", b"nope" * 40), ("bad2.jpg", b"nope" * 40)]
    response = submit_task(
        client, CONVERT, files=image_files(*images), data={"target_format": "png"}
    )
    snapshot = wait_for_task(client, response.json()["group_id"])

    assert snapshot["state"] == STATE_FAILED
    assert snapshot["failed"] == 2
    assert snapshot["result"] is None
    assert snapshot["error"]["code"] in ("CORRUPTED_FILE", "INVALID_FILE_TYPE")
    assert snapshot["error"]["message"]


def test_snapshot_never_leaks_server_paths(client: TestClient) -> None:
    """快照里不能出现服务器上的真实路径（§18）。"""
    response = submit_task(
        client,
        COMPRESS,
        files=image_files(("photo.jpg", build_image_bytes(300, 200, "JPEG"))),
        data={"quality": "balanced"},
    )
    snapshot = wait_for_task(client, response.json()["group_id"])

    raw = str(snapshot)
    assert tempfile.gettempdir() not in raw
    assert "filetools_" not in raw
    assert ".upload" not in raw


# ----------------------------------------------------------------------
# 任务过期与清理（§14）
# ----------------------------------------------------------------------

def test_expired_task_is_gone(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """超过保留时长的任务查询不到。"""
    response = submit_task(
        client,
        CONVERT,
        files=image_files(("a.jpg", build_image_bytes(120, 120, "JPEG"))),
        data={"target_format": "png"},
    )
    group_id = response.json()["group_id"]
    wait_for_task(client, group_id)
    assert client.get(f"/api/tasks/{group_id}").status_code == 200

    # 把保留时长改成 0：下一次访问即视为过期
    monkeypatch.setattr(settings, "TASK_TTL_SECONDS", 0)

    assert client.get(f"/api/tasks/{group_id}").status_code == 404
    assert task_queue.get(group_id) is None


def test_purge_expired_keeps_live_records(client: TestClient) -> None:
    """清理时不能误删还在保留期内的任务。"""
    response = submit_task(
        client,
        CONVERT,
        files=image_files(("a.jpg", build_image_bytes(120, 120, "JPEG"))),
        data={"target_format": "png"},
    )
    group_id = response.json()["group_id"]
    wait_for_task(client, group_id)

    assert task_queue.purge_expired() == 0
    assert client.get(f"/api/tasks/{group_id}").status_code == 200


def test_cleanup_worker_sweeps_expired_tasks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§14 的定时清理必须覆盖任务记录，而不只是结果文件。"""
    from services.job_store import cleanup_worker

    response = submit_task(
        client,
        CONVERT,
        files=image_files(("a.jpg", build_image_bytes(120, 120, "JPEG"))),
        data={"target_format": "png"},
    )
    group_id = response.json()["group_id"]
    wait_for_task(client, group_id)

    monkeypatch.setattr(settings, "TASK_TTL_SECONDS", 0)
    assert cleanup_worker.sweep_once() >= 1
    assert client.get(f"/api/tasks/{group_id}").status_code == 404


def test_downloaded_result_marks_snapshot_expired(client: TestClient) -> None:
    """结果被下载（文件已删除）后，快照要如实标记，避免留下点不动的下载按钮。"""
    response = submit_task(
        client,
        CONVERT,
        files=image_files(("a.jpg", build_image_bytes(200, 200, "JPEG"))),
        data={"target_format": "png"},
    )
    group_id = response.json()["group_id"]
    snapshot = wait_for_task(client, group_id)

    assert client.get(snapshot["result"]["download_url"]).status_code == 200

    after = client.get(f"/api/tasks/{group_id}").json()
    assert after["result"]["download_url"] is None
    assert after["result"]["expired"] is True
    # 处理结果本身仍然可见（用户还能看到省了多少）
    assert after["result"]["items"]


def test_download_releases_temp_files(client: TestClient) -> None:
    """下载之后临时目录必须清空（§14 / §19）。"""
    response = submit_task(
        client,
        CONVERT,
        files=image_files(
            ("a.jpg", build_image_bytes(200, 200, "JPEG")),
            ("b.jpg", build_image_bytes(200, 200, "JPEG")),
        ),
        data={"target_format": "png"},
    )
    snapshot = wait_for_task(client, response.json()["group_id"])

    during = temp_dirs()
    assert client.get(snapshot["result"]["download_url"]).status_code == 200
    after = temp_dirs()

    assert len(after) < len(during), "下载后任务目录应当被删除"


def test_all_failed_batch_leaves_no_temp_dir(client: TestClient) -> None:
    """整批失败时也不能残留临时目录。"""
    before = temp_dirs()
    response = submit_task(
        client,
        CONVERT,
        files=image_files(("bad1.jpg", b"nope" * 40), ("bad2.jpg", b"nope" * 40)),
        data={"target_format": "png"},
    )
    wait_for_task(client, response.json()["group_id"])

    assert temp_dirs() == before


# ----------------------------------------------------------------------
# 队列内部行为（直接构造 TaskQueue，不经 HTTP）
# ----------------------------------------------------------------------

def test_queue_limits_concurrency() -> None:
    """worker 数量就是同时处理的文件数上限（§7）。"""

    async def scenario() -> tuple[int, int, Path]:
        work_dir = Path(tempfile.mkdtemp(prefix="filetools_test_"))
        queue = TaskQueue()
        await queue.start()
        try:
            peak = 0
            running = 0

            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                nonlocal peak, running
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.02)
                running -= 1
                return {}

            group = _make_group(
                "concurrency",
                [TaskItem(index=i, filename=f"{i}.jpg", size=1) for i in range(8)],
                handler,
                directory=work_dir,
            )
            await queue.submit(group)
            while not group.all_terminal():
                await asyncio.sleep(0.01)
            return peak, queue.worker_count(), work_dir
        finally:
            await queue.stop()

    peak, workers, work_dir = asyncio.run(scenario())
    assert workers == max(1, settings.QUEUE_WORKERS)
    assert peak <= workers, f"同时处理了 {peak} 个文件，超过 worker 数 {workers}"

    import shutil

    shutil.rmtree(work_dir, ignore_errors=True)


def test_unclassified_batch_runs_in_the_default_pool() -> None:
    """未分类批次（``tool="test"``、也没有源/目标类型）走 ``default`` 池。

    这是第八阶段留给第四阶段的**兼容闸门**。上面那条
    ``worker_count() == QUEUE_WORKERS`` 之所以一个字都没改还能过，
    靠的就是这里：``worker_count()`` 报的正是 ``default`` 池的大小，
    而未分类批次也正好落在那个池里 —— 语义与第四阶段完全一致。

    验的是**路由结果**而不是分类函数：批次真的提交进去，然后看
    ``pool_snapshot()`` 里活到底出现在哪个池。分类函数写对了、
    但派发时没按它走，这条测试会红，而只测分类函数的那条不会。
    """

    async def scenario() -> tuple[int, int, dict, Path]:
        work_dir = Path(tempfile.mkdtemp(prefix="filetools_test_"))
        queue = TaskQueue()
        await queue.start()
        release = asyncio.Event()
        count = settings.QUEUE_WORKERS + 2

        async def handler(item: TaskItem, group: TaskGroup) -> dict:
            # 卡住不让走，好让快照抓到一个「有活在跑也有活在等」的瞬间
            await release.wait()
            return {}

        try:
            group = _make_group(
                "unclassified",
                [TaskItem(index=i, filename=f"{i}.bin", size=1) for i in range(count)],
                handler,
                directory=work_dir,
            )
            await queue.submit(group)
            await asyncio.sleep(0.05)  # 让 worker 真的把件领走

            snapshot = queue.pool_snapshot()
            busy = {
                pool["name"]: pool["active"] + pool["queue_size"]
                for pool in snapshot["pools"]
                if pool["active"] or pool["queue_size"]
            }
            release.set()
            while not group.all_terminal():
                await asyncio.sleep(0.01)
            return queue.worker_count(), count, busy, work_dir
        finally:
            release.set()
            await queue.stop()

    workers, count, busy, work_dir = asyncio.run(scenario())

    # 活**只在** default 池里，一个都不许漏到资源池去
    assert list(busy) == [POOL_DEFAULT], busy
    assert busy[POOL_DEFAULT] == count, busy
    # 而且 default 池的大小就是 QUEUE_WORKERS —— 既有语义没变
    assert workers == max(1, settings.QUEUE_WORKERS)
    assert pool_sizes()[POOL_DEFAULT] == max(1, settings.QUEUE_WORKERS)

    import shutil

    shutil.rmtree(work_dir, ignore_errors=True)


def test_queue_survives_handler_crash() -> None:
    """处理器抛出非预期异常时，worker 不能死，该文件记成失败。"""

    async def scenario() -> dict:
        work_dir = Path(tempfile.mkdtemp(prefix="filetools_test_"))
        queue = TaskQueue()
        await queue.start()
        try:
            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                if item.index == 0:
                    raise RuntimeError("boom")
                return {"ok": True}

            group = _make_group(
                "crash",
                [TaskItem(index=i, filename=f"{i}.jpg", size=1) for i in range(3)],
                handler,
                directory=work_dir,
            )
            await queue.submit(group)
            while not group.all_terminal():
                await asyncio.sleep(0.01)
            return queue.snapshot(group)
        finally:
            await queue.stop()

    snapshot = asyncio.run(scenario())
    assert snapshot["failed"] == 1
    assert snapshot["completed"] == 2
    # 非预期异常不能把内部堆栈泄露给前端
    failed = next(task for task in snapshot["tasks"] if task["state"] == STATE_FAILED)
    assert failed["error_code"] == "PROCESSING_FAILED"
    assert "RuntimeError" not in (failed["error_message"] or "")


def test_submit_without_started_queue_is_rejected() -> None:
    """队列没启动时提交要明确报错，而不是悄悄丢掉任务。"""

    async def scenario() -> None:
        queue = TaskQueue()
        group = _make_group(
            "no-queue",
            [TaskItem(index=0, filename="a.jpg", size=1)],
            _noop_handler,
            directory=Path("."),
        )
        with pytest.raises(ProcessingError) as excinfo:
            await queue.submit(group)
        assert "队列" in str(excinfo.value)

    asyncio.run(scenario())


def test_handler_receives_one_file_at_a_time() -> None:
    """每个任务项只对应一个文件：处理时拿到的永远是单个 item。"""

    async def scenario() -> list[str]:
        work_dir = Path(tempfile.mkdtemp(prefix="filetools_test_"))
        queue = TaskQueue()
        await queue.start()
        try:
            seen: list[str] = []

            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                seen.append(item.filename)
                return {}

            group = _make_group(
                "one-at-a-time",
                [TaskItem(index=i, filename=f"{i}.jpg", size=1) for i in range(5)],
                handler,
                directory=work_dir,
            )
            await queue.submit(group)
            while not group.all_terminal():
                await asyncio.sleep(0.01)
            return seen
        finally:
            await queue.stop()

    seen = asyncio.run(scenario())
    assert sorted(seen) == ["0.jpg", "1.jpg", "2.jpg", "3.jpg", "4.jpg"]


def test_task_outcome_maps_error_code_to_status(client: TestClient) -> None:
    """异步任务的失败原因要能还原成同步接口的状态码（前端与测试都靠它对口径）。"""
    from utils.errors import status_for_code

    assert status_for_code("FILE_TOO_LARGE") == 413
    assert status_for_code("INVALID_FILE_TYPE") == 415
    assert status_for_code("PROCESSING_TIMEOUT") == 504
    assert status_for_code("TASK_NOT_FOUND") == 404
    assert status_for_code("NOT_A_REAL_CODE") == 500


def test_result_images_are_decodable(client: TestClient) -> None:
    """异步产出的结果文件必须真的是能解码的图片。"""
    response = submit_task(
        client,
        CONVERT,
        files=image_files(("photo.jpg", build_image_bytes(320, 240, "JPEG"))),
        data={"target_format": "png"},
    )
    snapshot = wait_for_task(client, response.json()["group_id"])

    download = client.get(snapshot["result"]["download_url"])
    with Image.open(io.BytesIO(download.content)) as image:
        assert image.format == "PNG"
        assert image.size == (320, 240)


# ----------------------------------------------------------------------
# 无人认领的临时目录（§14 兜底）
# ----------------------------------------------------------------------


def test_sweep_orphan_dirs_removes_stale_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """进程被强杀后留下的目录要能被清理掉。

    模拟方式：把临时根目录指到一个空目录，造一个「很久以前」的
    ``filetools_*`` 目录，再跑一次清理。
    """
    import os

    from utils.files import create_temp_dir, sweep_orphan_dirs

    monkeypatch.setattr(settings, "TEMP_ROOT", str(tmp_path))
    orphan = create_temp_dir()
    (orphan / "leftover.bin").write_bytes(b"x" * 16)

    # 把修改时间拨到两天前 —— 远超「最长保留时长 × 2」
    old = time.time() - 2 * 24 * 3600
    os.utime(orphan, (old, old))

    assert sweep_orphan_dirs() >= 1
    assert not orphan.exists()


def test_sweep_orphan_dirs_keeps_fresh_and_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """刚建的目录、以及还在登记表里的目录，一个都不能删。"""
    import os

    from utils.files import create_temp_dir, sweep_orphan_dirs

    monkeypatch.setattr(settings, "TEMP_ROOT", str(tmp_path))

    fresh = create_temp_dir()
    registered = create_temp_dir()
    old = time.time() - 2 * 24 * 3600
    os.utime(registered, (old, old))

    # 别人的目录（前缀不对）也不能碰
    stranger = tmp_path / "some-other-app"
    stranger.mkdir()
    os.utime(stranger, (old, old))

    removed = sweep_orphan_dirs({registered})

    assert removed == 0
    assert fresh.exists()
    assert registered.exists()
    assert stranger.exists()


def test_cleanup_worker_sweeps_orphan_dirs(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清理线程启动时就注册了「临时目录」这一项，不用额外配置。"""
    import os

    from services.job_store import cleanup_worker
    from utils.files import create_temp_dir

    monkeypatch.setattr(settings, "TEMP_ROOT", str(tmp_path))
    orphan = create_temp_dir()
    old = time.time() - 2 * 24 * 3600
    os.utime(orphan, (old, old))

    assert cleanup_worker.sweep_once() >= 1
    assert not orphan.exists()


# ----------------------------------------------------------------------
# 取消与重试（第七阶段 §十五 / §十六）
#
# 这一段用可控的假处理器而不是真转换。原因不是图快，而是**可观察性**：
# 「排队项立刻停、在跑的项如实显示正在取消」全是时序问题，靠真文件去抢
# 那个时间点会变成抽奖 —— 转一个 4 秒的 docx 时它确实还在跑，转一个
# 已经预热好的可能已经结束了，测试就会时红时绿。
# ----------------------------------------------------------------------


async def _settled(group: TaskGroup) -> None:
    """等到整批真的收尾。

    不能只等 ``group.finalized``：那个标志在 ``_maybe_finalize`` 里**先**置位、
    之后才跑收尾器，所以它单独为真时整组状态还没定下来。
    """
    for _ in range(2000):
        if group.finalized and group.state in TERMINAL_STATES:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务没有收尾：{group.state} / {group.counts()}")


def _source_files(work_dir: Path, count: int) -> list[Path]:
    """造几个真实的源文件 —— 重试的前提是它还在磁盘上。"""
    paths: list[Path] = []
    for index in range(count):
        path = work_dir / f"source-{index}.bin"
        path.write_bytes(b"payload" * 4)
        paths.append(path)
    return paths


def _work_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="filetools_test_"))


def test_cancelled_state_fits_the_existing_state_machine() -> None:
    """新增的取消状态必须接进既有状态机，而不是另起一套词汇。"""
    # 前四个状态的**字面量**一个都没动：四个既有测试模块与前端页面
    # 都是按字面量判断的，改名会让它们静默失配。
    assert (STATE_WAITING, STATE_PROCESSING, STATE_DONE, STATE_FAILED) == (
        "waiting",
        "processing",
        "done",
        "failed",
    )
    assert TERMINAL_STATES == frozenset({STATE_DONE, STATE_FAILED, STATE_CANCELLED})
    assert STATE_CANCELLED in TERMINAL_STATES
    assert STATE_LABELS[STATE_CANCELLED] == "已取消"


def test_counts_always_add_up_to_the_total() -> None:
    """加了 cancelled 之后，五项之和仍然必须等于总数。"""
    group = _make_group(
        "counts",
        [
            TaskItem(index=0, filename="a.bin", size=1, state=STATE_DONE),
            TaskItem(index=1, filename="b.bin", size=1, state=STATE_FAILED),
            TaskItem(index=2, filename="c.bin", size=1, state=STATE_CANCELLED),
            TaskItem(index=3, filename="d.bin", size=1, state=STATE_PROCESSING),
            TaskItem(index=4, filename="e.bin", size=1),
        ],
        _noop_handler,
        directory=Path("."),
    )
    counts = group.counts()

    assert counts == {
        "total": 5,
        "completed": 1,
        "processing": 1,
        "waiting": 1,
        "failed": 1,
        "cancelled": 1,
    }
    assert sum(counts[key] for key in counts if key != "total") == counts["total"]


def test_cancel_stops_queued_items_and_is_honest_about_running_ones() -> None:
    """取消的诚实语义：排队的立刻停，在跑的**如实**报成「正在取消」。

    这是 §十五 的核心要求。正在跑的项在共享线程池里（LibreOffice / OCR），
    asyncio 取消不了线程，所以它只能等自己跑完；界面必须说「正在取消」，
    不能假装已经停了。
    """

    async def scenario() -> tuple[int, dict, dict, TaskGroup, list[int]]:
        work_dir = _work_dir()
        queue = TaskQueue()
        await queue.start()
        try:
            workers = queue.worker_count()
            total = workers + 3
            running = 0
            enough_running = asyncio.Event()
            release = asyncio.Event()
            seen: list[int] = []

            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                nonlocal running
                seen.append(item.index)
                running += 1
                if running >= workers:
                    enough_running.set()
                await release.wait()
                return {"ok": True}

            group = _make_group(
                "cancel-honest",
                [TaskItem(index=i, filename=f"{i}.bin", size=8) for i in range(total)],
                handler,
                directory=work_dir,
            )
            await queue.submit(group)
            await asyncio.wait_for(enough_running.wait(), timeout=5)

            during = queue.snapshot(group)
            await queue.cancel("cancel-honest")
            after = queue.snapshot(group)

            release.set()
            await _settled(group)
            return total, during, after, group, seen
        finally:
            await queue.stop()
            remove_dir(work_dir)

    total, during, after, group, seen = asyncio.run(scenario())
    queued = total - len(seen)

    assert queued >= 1, "构造的批次必须留下排队的项，否则这个测试什么也没测到"
    assert during["processing"] == len(seen)
    assert during["waiting"] == queued
    assert during["cancelling"] is False
    assert during["cancelled"] == 0

    # 取消之后：排队的全停了，在跑的一个都没停
    assert after["cancelling"] is True
    assert after["cancelled"] == queued
    assert after["waiting"] == 0
    assert after["processing"] == len(seen), "线程里的活停不下来，状态就不能假装停了"
    assert after["completed"] == 0
    # 进度停在「已定下来」的比例上，不假装跑完了
    assert after["percent"] == pytest.approx(queued / total * 100, abs=0.1)

    # 跑完之后：结果被丢弃、状态改判为已取消，整批是 cancelled 而不是 failed
    assert group.state == STATE_CANCELLED
    assert group.counts()["cancelled"] == total
    assert group.counts()["completed"] == 0
    assert group.error_code is None, "用户自己按的取消，不该报成服务器出错"
    assert group.error_message is None
    for item in group.items:
        assert item.state == STATE_CANCELLED
        assert item.result is None, "取消掉的项结果必须丢掉"
        assert item.payload is None
    # 取消那一刻还在排队的那几项，从头到尾就没进过处理器 ——
    # 队列是先进先出，开工的必然是开头连续的那几个序号。
    assert sorted(seen) == list(range(len(seen))), "排队中被取消的项也开工了"


def test_cancel_keeps_real_failures_visible() -> None:
    """取消不能把「这个文件真的坏了」这件事一起抹掉。

    全是取消 → 整批记为已取消（用户按的键，不是服务器坏了）；
    只要有一个是真失败 → 仍按失败报，并把真实原因原样带出来。
    """

    async def scenario() -> tuple[TaskGroup, int]:
        work_dir = _work_dir()
        queue = TaskQueue()
        await queue.start()
        try:
            workers = queue.worker_count()
            total = workers + 3
            started = 0
            enough_started = asyncio.Event()
            gate = asyncio.Event()
            sources = _source_files(work_dir, total)

            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                nonlocal started
                started += 1
                if started >= workers:
                    enough_started.set()
                await gate.wait()
                if item.index == 0:
                    raise ProcessingError("这个文件读不了")
                return {}

            group = _make_group(
                "cancel-mixed",
                [
                    TaskItem(index=i, filename=f"{i}.bin", size=8, source=sources[i])
                    for i in range(total)
                ],
                handler,
                directory=work_dir,
            )
            await queue.submit(group)
            # 等到所有 worker 都被占住，再取消：此时还没开工的项是确定的
            await asyncio.wait_for(enough_started.wait(), timeout=5)
            await queue.cancel("cancel-mixed")
            gate.set()
            await _settled(group)
            return group, total
        finally:
            await queue.stop()
            remove_dir(work_dir)

    group, total = asyncio.run(scenario())
    counts = group.counts()

    assert group.state == STATE_FAILED, "有真失败就不能报成「用户取消了」"
    assert group.error_code == ErrorCode.PROCESSING_FAILED
    assert group.error_message == "这个文件读不了"
    assert counts["failed"] == 1
    assert counts["cancelled"] == total - 1, "跑完才被取消的项要改判成已取消"
    assert counts["completed"] == 0
    assert group.items[0].state == STATE_FAILED
    assert group.items[0].error_message == "这个文件读不了"


def test_cancel_after_finalize_changes_nothing() -> None:
    """已经收尾的批次没什么可取消的，原样返回、不改状态。"""

    async def scenario() -> tuple[TaskGroup, TaskGroup]:
        work_dir = _work_dir()
        queue = TaskQueue()
        await queue.start()
        try:
            group = _make_group(
                "cancel-done",
                [TaskItem(index=0, filename="a.bin", size=1)],
                _noop_handler,
                directory=work_dir,
            )
            await queue.submit(group)
            await _settled(group)

            returned = await queue.cancel("cancel-done")
            assert returned is not None
            return group, returned
        finally:
            await queue.stop()
            remove_dir(work_dir)

    group, returned = asyncio.run(scenario())
    assert returned is group
    assert group.state == STATE_DONE
    assert group.cancel_requested is False


def test_cancel_unknown_group_returns_none() -> None:
    """不存在的批次：与查询接口一致，返回 None 而不是抛错。"""

    async def scenario() -> None:
        queue = TaskQueue()
        assert await queue.cancel("nothing-here") is None

    asyncio.run(scenario())


def test_retry_requires_a_failed_item_with_its_source() -> None:
    """重试的三道门槛：失败过、次数没用完、源文件还在（§十六）。"""

    async def scenario() -> TaskGroup:
        work_dir = _work_dir()
        queue = TaskQueue()
        await queue.start()
        try:
            sources = _source_files(work_dir, 3)

            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                if item.index == 1:
                    raise ProcessingError("偶发失败")
                return {}

            group = _make_group(
                "retry-gates",
                [
                    TaskItem(index=i, filename=f"{i}.bin", size=8, source=sources[i])
                    for i in range(3)
                ],
                handler,
                directory=work_dir,
                retry_enabled=True,
            )
            await queue.submit(group)
            await _settled(group)
            assert group.state == STATE_DONE
            assert group.retryable(group.items[1]) is True
            assert group.retryable(group.items[0]) is False, "成功的项不能重试"

            # 成功的项：拒
            with pytest.raises(TaskNotRetryableError) as succeeded:
                await queue.retry("retry-gates", 0)
            assert "失败" in str(succeeded.value)
            # 不在这一批里的序号：拒绝，且要说清是「不在该任务里」
            with pytest.raises(TaskNotFoundError):
                await queue.retry("retry-gates", 9)
            # 不存在的批次：拒
            with pytest.raises(TaskNotFoundError):
                await queue.retry("no-such-group", 0)

            # 源文件被清理掉之后也不能重试（重试要用 in/ 里那份原件）
            sources[1].unlink()
            with pytest.raises(TaskNotRetryableError) as gone:
                await queue.retry("retry-gates", 1)
            assert "重新上传" in str(gone.value)
            return group
        finally:
            await queue.stop()
            remove_dir(work_dir)

    group = asyncio.run(scenario())
    assert group.items[1].retry_count == 0, "被拒的重试不能算用掉次数"
    assert group.items[1].state == STATE_FAILED


def test_retry_reruns_the_item_and_finalizes_again() -> None:
    """重试真的会重跑，并把整批重新收尾一次（重建 ZIP 靠的就是这一步）。"""

    async def scenario() -> tuple[TaskGroup, int, list[int]]:
        work_dir = _work_dir()
        queue = TaskQueue()
        await queue.start()
        try:
            sources = _source_files(work_dir, 3)
            attempts: dict[int, int] = {}
            finalize_calls: list[int] = []

            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                attempts[item.index] = attempts.get(item.index, 0) + 1
                if item.index == 1 and attempts[item.index] == 1:
                    raise ProcessingError("第一次失败")
                return {"ok": True}

            async def finalizer(group: TaskGroup) -> None:
                finalize_calls.append(len(group.successful()))
                group.summary = {"completed": len(group.successful())}

            group = _make_group(
                "retry-rerun",
                [
                    TaskItem(index=i, filename=f"{i}.bin", size=8, source=sources[i])
                    for i in range(3)
                ],
                handler,
                directory=work_dir,
                finalizer=finalizer,
                retry_enabled=True,
            )
            await queue.submit(group)
            await _settled(group)
            assert group.items[1].state == STATE_FAILED, "重试的前提是这一项真的失败过"
            assert group.state == STATE_DONE, "还有别的项成功了，整批就是「有结果」"

            await queue.retry("retry-rerun", 1)
            await _settled(group)
            return group, attempts.get(1, 0), finalize_calls
        finally:
            await queue.stop()
            remove_dir(work_dir)

    group, attempts, finalize_calls = asyncio.run(scenario())

    assert attempts == 2, "重试必须真的重跑一次"
    assert finalize_calls == [2, 3], "整批要重新收尾，收尾器因此跑了两次"
    assert group.state == STATE_DONE
    assert group.counts()["completed"] == 3
    assert group.items[1].retry_count == 1
    assert group.items[1].error_code is None
    assert group.summary == {"completed": 3}


def test_retry_is_allowed_only_once() -> None:
    """次数上限由服务端强制 —— 狂点重试的客户端同样能把 LibreOffice 打爆。"""

    async def scenario() -> TaskGroup:
        work_dir = _work_dir()
        queue = TaskQueue()
        await queue.start()
        try:
            sources = _source_files(work_dir, 1)

            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                raise ProcessingError("永远失败")

            group = _make_group(
                "retry-limit",
                [TaskItem(index=0, filename="a.bin", size=8, source=sources[0])],
                handler,
                directory=work_dir,
                retry_enabled=True,
            )
            await queue.submit(group)
            await _settled(group)

            await queue.retry("retry-limit", 0)
            await _settled(group)
            assert group.items[0].retry_count == MAX_ITEM_RETRIES
            assert group.items[0].state == STATE_FAILED
            assert group.retryable(group.items[0]) is False

            with pytest.raises(TaskNotRetryableError) as limited:
                await queue.retry("retry-limit", 0)
            assert "重试过" in str(limited.value)
            return group
        finally:
            await queue.stop()
            remove_dir(work_dir)

    group = asyncio.run(scenario())
    assert group.state == STATE_FAILED


def test_batches_without_retry_are_refused_and_cleaned_up_at_once() -> None:
    """不支持重试的批次：目录当场删掉，重试请求也直接拒绝。

    既有的四个批量工具从来没有重试按钮，所以它们失败时不该为了一个
    没人会用的能力多占半小时磁盘 —— 这也是第三阶段就有的行为，
    第七阶段不能把它改掉。
    """

    async def scenario() -> tuple[TaskGroup, bool]:
        work_dir = _work_dir()
        queue = TaskQueue()
        await queue.start()
        try:
            sources = _source_files(work_dir, 1)

            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                raise ProcessingError("处理不了")

            group = _make_group(
                "no-retry",
                [TaskItem(index=0, filename="a.bin", size=8, source=sources[0])],
                handler,
                directory=work_dir,
            )
            await queue.submit(group)
            await _settled(group)

            assert group.items[0].can_retry() is True, "项本身的失败状态是可重试的"
            assert group.retryable(group.items[0]) is False, "但这一批不支持重试"
            assert queue.snapshot(group)["tasks"][0]["can_retry"] is False

            with pytest.raises(TaskNotRetryableError) as refused:
                await queue.retry("no-retry", 0)
            assert "不支持重试" in str(refused.value)
            return group, work_dir.exists()
        finally:
            await queue.stop()

    group, survived = asyncio.run(scenario())
    assert group.state == STATE_FAILED
    assert survived is False, "不支持重试的批次失败后要当场清掉临时目录"


def test_retry_discards_the_archive_but_not_the_single_results() -> None:
    """重试只作废聚合 ZIP，别人那份单文件结果一个都不能碰（§六 的令牌归属）。"""

    async def scenario() -> tuple[TaskGroup, list[str], str, str]:
        work_dir = _work_dir()
        queue = TaskQueue()
        await queue.start()
        try:
            sources = _source_files(work_dir, 3)
            attempts: dict[int, int] = {}

            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                attempts[item.index] = attempts.get(item.index, 0) + 1
                if item.index == 1 and attempts[item.index] == 1:
                    raise ProcessingError("第一次失败")
                return {"ok": True}

            async def finalizer(group: TaskGroup) -> None:
                # 每个成功项一个令牌，各自独占 out/<n>/
                for item in group.successful():
                    out_dir = group.directory / "out" / str(item.index)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    target = out_dir / f"{item.index}.bin"
                    target.write_bytes(b"result")
                    job = job_store.add(
                        Job(
                            job_id=f"job-{group.group_id}-{item.index}-{item.retry_count}",
                            directory=out_dir,
                            path=target,
                            filename=target.name,
                            media_type="application/octet-stream",
                            size=target.stat().st_size,
                        )
                    )
                    item.payload = {"job_id": job.job_id}

                if len(group.successful()) < 2:
                    group.summary = {"archived": False}
                    return
                # 成功 ≥2 个 → 聚合成一个 ZIP 令牌，独占 zip/
                zip_dir = group.directory / "zip"
                zip_dir.mkdir(parents=True, exist_ok=True)
                archive = zip_dir / "bundle.zip"
                archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
                job = job_store.add(
                    Job(
                        job_id=f"zip-{group.group_id}-{len(group.successful())}",
                        directory=zip_dir,
                        path=archive,
                        filename=archive.name,
                        media_type="application/zip",
                        size=archive.stat().st_size,
                    )
                )
                group.archive_job_id = job.job_id
                group.job_id = job.job_id
                group.summary = {"archived": True}

            group = _make_group(
                "retry-tokens",
                [
                    TaskItem(index=i, filename=f"{i}.bin", size=8, source=sources[i])
                    for i in range(3)
                ],
                handler,
                directory=work_dir,
                finalizer=finalizer,
                retry_enabled=True,
            )
            await queue.submit(group)
            await _settled(group)

            assert group.counts()["completed"] == 2
            first_jobs = [
                item.payload["job_id"] for item in group.items if item.payload
            ]
            old_archive = group.archive_job_id
            assert old_archive is not None
            assert sorted(first_jobs) == [
                "job-retry-tokens-0-0",
                "job-retry-tokens-2-0",
            ]

            await queue.retry("retry-tokens", 1)
            assert job_store.get(old_archive) is None, "重试时就该作废旧 ZIP 令牌"
            await _settled(group)
            return group, first_jobs, old_archive, group.archive_job_id
        finally:
            await queue.stop()
            remove_dir(work_dir)

    group, first_jobs, old_archive, new_archive = asyncio.run(scenario())

    assert group.state == STATE_DONE
    assert group.counts()["completed"] == 3
    # 新的 ZIP 覆盖了三个文件，旧的那个少一个，不能继续挂着
    assert new_archive != old_archive
    assert new_archive is not None and job_store.get(new_archive) is not None
    # 两个单文件结果一个都没被连累
    for job_id in first_jobs:
        assert job_store.get(job_id) is not None, f"{job_id} 被误删了"


def test_expired_task_keeps_its_dir_while_a_download_token_lives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """任务记录过期时删工作目录，但**还挂着下载令牌的不能删**。

    每个令牌独占一个子目录，快照里可能还挂着它的下载链接；
    目录一删，用户点下载就指向空气。
    """

    async def scenario() -> tuple[bool, bool]:
        keep_dir = tmp_path / "keep"
        drop_dir = tmp_path / "drop"
        keep_dir.mkdir()
        drop_dir.mkdir()

        queue = TaskQueue()
        await queue.start()
        try:
            keep = _make_group(
                "expire-keep",
                [TaskItem(index=0, filename="a.bin", size=1)],
                _noop_handler,
                directory=keep_dir,
            )
            drop = _make_group(
                "expire-drop",
                [TaskItem(index=0, filename="b.bin", size=1)],
                _noop_handler,
                directory=drop_dir,
            )
            await queue.submit(keep)
            await queue.submit(drop)
            await _settled(keep)
            await _settled(drop)

            out_dir = keep_dir / "out" / "0"
            out_dir.mkdir(parents=True)
            target = out_dir / "a.bin"
            target.write_bytes(b"x")
            job_store.add(
                Job(
                    job_id="phase7-expire-keep",
                    directory=out_dir,
                    path=target,
                    filename="a.bin",
                    media_type="application/octet-stream",
                    size=1,
                )
            )

            monkeypatch.setattr(settings, "TASK_TTL_SECONDS", 0)
            assert queue.purge_expired() == 2
            return keep_dir.exists(), drop_dir.exists()
        finally:
            await queue.stop()
            job_store.discard("phase7-expire-keep")

    kept, dropped = asyncio.run(scenario())
    assert kept is True, "还有令牌指向它时不能删"
    assert dropped is False, "没人指向它了就该删，不能等两天后的孤儿清理兜底"


def test_expired_failed_task_drops_its_dir_once_the_record_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败批次为了重试会留着目录；记录一过期就必须删掉，否则白占磁盘。"""

    async def scenario() -> tuple[bool, bool]:
        work_dir = tmp_path / "failed"
        work_dir.mkdir()
        queue = TaskQueue()
        await queue.start()
        try:
            sources = _source_files(work_dir, 1)

            async def handler(item: TaskItem, group: TaskGroup) -> dict:
                raise ProcessingError("处理不了")

            group = _make_group(
                "expire-failed",
                [TaskItem(index=0, filename="a.bin", size=8, source=sources[0])],
                handler,
                directory=work_dir,
                retry_enabled=True,
            )
            await queue.submit(group)
            await _settled(group)

            # 还在保留期内：不能删，重试要用 in/ 里那份原件
            assert group.retryable(group.items[0]) is True
            kept_while_alive = work_dir.exists()

            monkeypatch.setattr(settings, "TASK_TTL_SECONDS", 0)
            assert queue.purge_expired() == 1
            # 记录没了，重试也就无从谈起（查询接口已经是 404），目录该删了
            assert queue.get("expire-failed") is None
            return kept_while_alive, work_dir.exists()
        finally:
            await queue.stop()

    kept_while_alive, after_expiry = asyncio.run(scenario())
    assert kept_while_alive is True, "保留期内不能删 —— 重试还要用里面的原件"
    assert after_expiry is False
