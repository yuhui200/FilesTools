"""运行状态接口与指标登记表的测试（第八阶段 §十二 / §二十六 / §二十七）。

这一层验三件事，缺一不可：

1. **形状**：两个接口的 schema 稳定，`worker_id` 形如 ``worker-1``，
   池的配置数与 ``pool_sizes()`` 一致，合计等于各池之和。
2. **不泄露**：响应原文里不能出现路径分隔符、盘符、``soffice``、
   ``Traceback``、``.py``、文件名、令牌形状 —— 我把这条写成对**原文**的
   逐片段检查，而不是「我读了一遍觉得没有」。键名也要查：一个叫
   ``source_filename`` 的字段哪怕值是空的，这个接口也已经是个泄露面了。
3. **数对得上**：指标是给人做判断用的，算错比没有更糟。所以分两层验：
   登记表本身用**全新实例**做精确算术（5 成功 + 2 失败 + 1 取消 → 合计 8）；
   HTTP 那层则用**增量**验真实流量（跑真批次、真失败、真取消，看差值）。

第 3 点为什么用增量而不是绝对值：指标是**进程级**的，整个 pytest 会话共用
一个进程、一个 app。断言绝对值就等于断言「我是第一个跑的测试」，那是个
随执行顺序碎掉的假断言。增量既准确又不依赖顺序。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from config import settings
from services import office_converter
from services.worker_pool import POOL_ORDER, pool_sizes, timeout_table
from tests.conftest import (
    build_docx_bytes,
    build_image_bytes,
    conversion_task,
    image_files,
    office_files,
    requires_soffice,
    run_conversion,
    submit_conversion,
    wait_conversion,
)
from utils.errors import ErrorCode
from utils.metrics import (
    RESOURCE_OTHER,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    MetricsRegistry,
    metrics,
)

WORKERS = "/api/system/workers"
METRICS = "/api/system/metrics"
ENDPOINT = "/api/conversion/tasks"

BACKEND_DIR = Path(__file__).resolve().parent.parent

#: 取消测试用的批次大小。与 ``test_conversion_batch.py`` 同一个道理：
#: 只有**确实还在排队**的项才能立刻取消，所以批次必须比并发大。
CANCEL_BATCH_SIZE = settings.QUEUE_WORKERS + 1

#: ``group_id`` 与下载令牌都是 ``secrets.token_hex(16)`` ——
#: 一串 32 位小写十六进制。这个正则放宽到 16 位，
#: 任何长得像「随机标识」的东西都不许出现在运行状态接口里。
_TOKEN_SHAPE = re.compile(r"[0-9a-f]{16,}")

#: 运行状态接口原文里**一个都不许出现**的片段。
#:
#: 分三类，都是前七阶段一路守下来的东西：
#:   * 路径类：``/`` ``\`` 盘符 ``.py`` ``site-packages`` ``AppData``；
#:   * 实现类：``soffice`` ``libreoffice`` ``python`` ``Traceback``；
#:   * 用户数据类：``filename`` ``token``。
FORBIDDEN_FRAGMENTS = (
    "\\",
    "/",
    "soffice",
    "libreoffice",
    "Traceback",
    'File "',
    ".py",
    ".venv",
    "site-packages",
    "AppData",
    "C:",
    "D:",
    "python",
    "filename",
    "token",
)

#: 递归查键名时不许出现的词。值可以没有，**键本身**就已经是泄露面了。
FORBIDDEN_KEY_PARTS = ("filename", "path", "token", "group_id", "task_id", "dir")


def item_of(snapshot: dict, index: int) -> dict:
    return conversion_task(snapshot, index)


def get_json(client: TestClient, path: str) -> dict:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def all_keys(payload: Any) -> list[str]:
    """把一个 JSON 结构里**所有**层级的键名收集出来。"""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.append(key)
            found.extend(all_keys(value))
    elif isinstance(payload, list):
        for value in payload:
            found.extend(all_keys(value))
    return found


# ======================================================================
# 一、形状
# ======================================================================


def test_workers_schema_covers_every_pool(client: TestClient) -> None:
    """五个池一个不少、一个不多，顺序也必须是 POOL_ORDER。

    顺序不是小事：验收脚本和运维都按位置读这张表，
    哪天顺序变了，读出来的是错的池而不是一个报错。
    """
    body = get_json(client, WORKERS)

    assert [pool["name"] for pool in body["pools"]] == list(POOL_ORDER)
    assert body["accepting"] is True

    for pool in body["pools"]:
        assert set(pool) == {
            "name",
            "configured_workers",
            "started_workers",
            "active",
            "queue_size",
            "effective_timeout_seconds",
            "handled",
            "failed",
            "lost",
            "workers",
        }, pool["name"]
        assert pool["configured_workers"] >= 1
        assert pool["active"] <= pool["started_workers"]
        assert pool["queue_size"] >= 0


def test_workers_configured_matches_configuration(client: TestClient) -> None:
    """``configured_workers`` 必须就是配置里的那个数，不是另算的。"""
    body = get_json(client, WORKERS)
    sizes = pool_sizes()

    for pool in body["pools"]:
        assert pool["configured_workers"] == sizes[pool["name"]], pool["name"]


def test_workers_are_started_eagerly_and_totals_add_up(client: TestClient) -> None:
    """worker 是**开工即拉起**的（本阶段相对计划书的一处有意偏差）。

    为什么不做懒拉起：既有的 ``worker_count()`` 在**提交之前**就要返回
    配置数量（第四阶段的语义），懒拉起会让那两个调用点读到 0。
    代价是没活干的池也占着一个协程 —— 这是划算的，一个空转的协程
    不烧 CPU（它等在 ``asyncio.Event`` 上）。
    """
    body = get_json(client, WORKERS)

    for pool in body["pools"]:
        assert pool["started_workers"] == pool["configured_workers"], pool["name"]
        assert len(pool["workers"]) == pool["started_workers"], pool["name"]

    assert body["total_workers"] == sum(p["started_workers"] for p in body["pools"])
    assert body["total_active"] == sum(p["active"] for p in body["pools"])
    assert body["total_queue"] == sum(p["queue_size"] for p in body["pools"])
    assert body["total_workers"] == sum(pool_sizes().values())


def test_worker_rows_have_the_documented_shape(client: TestClient) -> None:
    body = get_json(client, WORKERS)

    seen: list[str] = []
    for pool in body["pools"]:
        for worker in pool["workers"]:
            assert set(worker) == {
                "worker_id",
                "pool",
                "state",
                "busy_seconds",
                "handled",
                "failed",
                "recycles",
                "generation",
            }
            # 编号是全局单调的（``worker-1`` …），池内不重号
            assert re.fullmatch(r"worker-\d+", worker["worker_id"]), worker
            assert worker["pool"] == pool["name"]
            assert worker["state"] in ("idle", "busy")
            assert worker["generation"] >= 1
            assert worker["recycles"] >= 0
            # 闲着的时候不该报一个耗时出来 —— 那是编的
            if worker["state"] == "idle":
                assert worker["busy_seconds"] is None
            else:
                assert worker["busy_seconds"] is not None
            seen.append(worker["worker_id"])

    assert len(seen) == len(set(seen)), "worker 编号在所有池之间也必须唯一"


def test_workers_timeout_section_matches_the_registry(client: TestClient) -> None:
    """超时表是**同一处代码**算出来的，不是在这里另抄一份。"""
    body = get_json(client, WORKERS)
    assert body["timeouts"] == timeout_table()

    row = next(item for item in body["timeouts"] if item["pool"] == "image")
    assert row["pool_timeout_seconds"] == settings.IMAGE_TIMEOUT_SECONDS
    # 生效值是「池兜底」与「池内既有操作」取 max，绝不能低于其中任何一个
    for item in body["timeouts"]:
        inner = [value for _, value in item["existing_timeouts"].items()]
        assert item["effective_timeout_seconds"] == max(
            [item["pool_timeout_seconds"], *inner]
        ), item["pool"]


@requires_soffice
def test_workers_reports_real_activity(client: TestClient) -> None:
    """接口报的是**真实现状**：提交完立刻看，office 池里 3 项一个不少。

    这是这个接口存在的全部意义 —— 它不能是估算、不能是平滑值。
    提交返回 202 时所有项**必然**已经入队（``submit`` 是 await 完才返回的），
    所以这里可以断言一个精确数字，而不是「大概能看到点什么」。

    顺带钉住两个接口的一致性：``/api/system/workers`` 的 ``total_queue``
    就是各池之和。此刻只有 office 池有活，所以它必须与 office 池对得上 ——
    两个接口对同一件事各说各话的话，运维该信哪一个？

    挂 ``requires_soffice``：这条要断言一个**精确数字**，前提是这批活真的
    还在跑。图片转换太快，抢不到那个窗口；而没有 LibreOffice 时
    ``CONVERTER_UNAVAILABLE`` 会让每项在 1~2 毫秒内结束（实测
    ``duration_ms=2``），查询到达时队列已经空了 —— 那时接口报的
    「0 项在跑」是**对的**，是这条用例的前提不成立。
    """
    files = tuple(
        (f"probe-{index}.docx", build_docx_bytes(f"PROBE{index}"))
        for index in range(CANCEL_BATCH_SIZE)
    )
    response = submit_conversion(
        client, files=office_files(*files, field="files"), target_type="pdf"
    )
    assert response.status_code == 202, response.text
    batch_id = response.json()["batch_id"]

    try:
        body = get_json(client, WORKERS)
        office = next(item for item in body["pools"] if item["name"] == "office")
        # 在跑的 + 还在排队的 = 全批。一项都没丢，也没凭空多出来。
        assert office["active"] + office["queue_size"] == CANCEL_BATCH_SIZE, office
        assert office["active"] >= 1, "至少有一项该开工了"
        # 别的池一个项都不该有：这批全是 docx
        for pool in body["pools"]:
            if pool["name"] != "office":
                assert pool["queue_size"] == 0, pool
                assert pool["active"] == 0, pool
        # 合计 = 各池之和
        assert body["total_queue"] == office["queue_size"]
        assert body["total_active"] == office["active"]
    finally:
        client.post(f"{ENDPOINT}/{batch_id}/cancel")
        # 别把 LibreOffice 的活留给下一个测试
        wait_conversion(client, batch_id, timeout=120.0)

    # 跑完之后要回到静止 —— 一个永远显示「有活在跑」的接口等于没有接口
    idle = get_json(client, WORKERS)
    assert idle["total_active"] == 0
    assert idle["total_queue"] == 0


def test_metrics_schema(client: TestClient) -> None:
    body = get_json(client, METRICS)

    assert set(body) == {
        "uptime_seconds",
        "tasks",
        "retries",
        "workers",
        "duration_ms",
        "by_type",
    }
    assert set(body["tasks"]) == {
        "total",
        "started",
        "completed",
        "failed",
        "cancelled",
        "timeout",
        "lost",
    }
    assert set(body["retries"]) == {"automatic", "manual"}
    assert set(body["workers"]) == {"recycled", "watchdog_kicks"}
    assert set(body["duration_ms"]) == {"count", "avg", "max", "min"}
    assert body["uptime_seconds"] >= 0
    assert body["tasks"]["total"] == (
        body["tasks"]["completed"] + body["tasks"]["failed"] + body["tasks"]["cancelled"]
    )


def test_system_endpoints_are_read_only(client: TestClient) -> None:
    """两个接口都只读。写方法必须被挡在门外。"""
    for path in (WORKERS, METRICS):
        assert client.post(path).status_code == 405, path
        assert client.delete(path).status_code == 405, path


# ======================================================================
# 二、不泄露
# ======================================================================


@pytest.mark.parametrize("path", [WORKERS, METRICS])
def test_system_endpoints_leak_nothing(client: TestClient, path: str) -> None:
    """查的是**响应原文**，不是「我读了一遍」。

    ``group_id`` 是 ``/api/tasks/{group_id}`` 的凭据，而那个接口会返回
    用户的文件名 —— 所以这两个接口刻意做成无身份信息的：不是「小心地
    过滤掉」，而是**没有地方放**。这条测试就是钉住这一点。
    """
    response = client.get(path)
    assert response.status_code == 200
    text = response.text

    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in text, f"{path} 的响应里出现了 {fragment!r}"

    assert not _TOKEN_SHAPE.search(text), f"{path} 的响应里出现了令牌形状的字符串"

    for key in all_keys(response.json()):
        lowered = key.lower()
        for part in FORBIDDEN_KEY_PARTS:
            assert part not in lowered, f"{path} 的响应里有 {key!r} 这个键"


def test_leak_check_would_actually_catch_a_leak() -> None:
    """反向对照：确认上面那套检查真的抓得住东西。

    一条永远通过的检查等于没有检查。这里拿 ``test_queue.py`` 里那个
    会返回文件名的接口做对照 —— 同一个字符串在它那里必须命中禁忌片段，
    否则说明我的禁忌表是空的、那三条断言全是摆设。
    """
    leaky = {
        "tasks": [
            {
                "source_filename": "report.pdf",
                "path": "C:\\Users\\Administrator\\AppData\\Local\\Temp\\filetools-abc",
                "group_id": "0123456789abcdef0123456789abcdef",
            }
        ]
    }
    text = json.dumps(leaky)
    assert any(fragment in text for fragment in FORBIDDEN_FRAGMENTS)
    assert _TOKEN_SHAPE.search(text)
    assert any(
        part in key.lower() for key in all_keys(leaky) for part in FORBIDDEN_KEY_PARTS
    )


# ======================================================================
# 三、指标：登记表本身的算术
# ======================================================================


def test_registry_counts_five_failed_two_and_one_cancelled() -> None:
    """5 成功 + 2 失败 + 1 取消 → 合计 **正好** 8。

    用全新实例，不碰进程级单例 —— 这是纯算术，必须能独立复现。
    """
    registry = MetricsRegistry()
    for _ in range(5):
        registry.record_started("image")
        registry.record_settled(status=STATUS_COMPLETED, resource_class="image", duration_ms=100)
    for _ in range(2):
        registry.record_started("office")
        registry.record_settled(
            status=STATUS_FAILED, resource_class="office", duration_ms=200, error_code="PROCESSING_FAILED"
        )
    registry.record_started("pdf")
    registry.record_settled(status=STATUS_CANCELLED, resource_class="pdf")

    snapshot = registry.snapshot()
    assert snapshot["tasks"] == {
        "total": 8,
        "started": 8,
        "completed": 5,
        "failed": 2,
        "cancelled": 1,
        "timeout": 0,
        "lost": 0,
    }
    # 总耗时只统计**有耗时**的那 7 条：取消的项根本没跑，给它编一个 0
    # 会把平均值往下拽
    assert snapshot["duration_ms"]["count"] == 7
    assert snapshot["duration_ms"]["max"] == 200
    assert snapshot["duration_ms"]["min"] == 100
    assert snapshot["duration_ms"]["avg"] == round((5 * 100 + 2 * 200) / 7, 1)


def test_registry_timeout_and_lost_are_subsets_of_failed() -> None:
    """``timeout`` / ``lost`` 是 ``failed`` 的**子集**，不能加进 ``total``。

    这两个数单独列出来只是为了可读性（「失败里有多少是超时」）。
    要是把它们也累加进 total，8 会变成 10，整个表就自相矛盾了。
    """
    registry = MetricsRegistry()
    registry.record_settled(
        status=STATUS_FAILED, resource_class="ocr", duration_ms=10, error_code=ErrorCode.TASK_TIMEOUT
    )
    registry.record_worker_lost()
    registry.record_settled(status=STATUS_FAILED, resource_class="ocr", duration_ms=10)

    snapshot = registry.snapshot()
    assert snapshot["tasks"]["total"] == 2
    assert snapshot["tasks"]["failed"] == 2
    assert snapshot["tasks"]["timeout"] == 1
    assert snapshot["tasks"]["lost"] == 1
    assert snapshot["by_type"]["ocr"]["timeout"] == 1


def test_registry_by_type_is_per_pool_and_keeps_unknown_buckets() -> None:
    registry = MetricsRegistry()
    registry.record_settled(status=STATUS_COMPLETED, resource_class="image", duration_ms=40)
    registry.record_settled(status=STATUS_COMPLETED, resource_class="image", duration_ms=60)
    registry.record_settled(status=STATUS_FAILED, resource_class=RESOURCE_OTHER, duration_ms=10)
    registry.record_settled(status=STATUS_CANCELLED, resource_class="image")

    snapshot = registry.snapshot()
    assert snapshot["by_type"]["image"] == {
        "count": 3,
        "failed": 0,
        "cancelled": 1,
        "timeout": 0,
        "avg_ms": 50.0,
        "max_ms": 60,
    }
    # 归不到已知资源类的样本**不丢弃**，落进 other ——
    # 一个统计不到的类别比一个叫 other 的类别难查得多
    assert snapshot["by_type"][RESOURCE_OTHER]["count"] == 1
    assert snapshot["by_type"][RESOURCE_OTHER]["failed"] == 1
    # 没有耗时的类别不该编出 avg/max
    assert snapshot["by_type"][RESOURCE_OTHER]["avg_ms"] == 10.0
    registry.record_settled(status=STATUS_COMPLETED, resource_class="newpool")
    assert registry.snapshot()["by_type"]["newpool"]["avg_ms"] is None


def test_registry_snapshot_is_a_copy_and_reset_keeps_the_lock() -> None:
    """快照是深拷贝；``reset`` 只清计数，**不换锁**。"""
    registry = MetricsRegistry()
    registry.record_settled(status=STATUS_COMPLETED, resource_class="image", duration_ms=5)

    snapshot = registry.snapshot()
    snapshot["tasks"]["completed"] = 999
    snapshot["by_type"]["image"]["count"] = 999
    again = registry.snapshot()
    assert again["tasks"]["completed"] == 1
    assert again["by_type"]["image"]["count"] == 1

    lock = registry._lock
    registry.reset()
    cleared = registry.snapshot()
    assert cleared["tasks"]["total"] == 0
    assert cleared["by_type"] == {}
    assert cleared["duration_ms"]["min"] is None
    # reset 换掉锁会让正持有旧锁的线程和新对象各说各话
    assert registry._lock is lock


def test_registry_counts_retries_and_worker_events() -> None:
    registry = MetricsRegistry()
    registry.record_retry(automatic=True)
    registry.record_retry(automatic=False)
    registry.record_retry(automatic=False)
    registry.record_worker_recycled()
    registry.record_watchdog_kick()
    registry.record_watchdog_kick()

    snapshot = registry.snapshot()
    assert snapshot["retries"] == {"automatic": 1, "manual": 2}
    assert snapshot["workers"] == {"recycled": 1, "watchdog_kicks": 2}


# ======================================================================
# 四、指标：真实流量（用增量，不看绝对值）
# ======================================================================


@requires_soffice
def test_metrics_track_real_traffic_end_to_end(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一批成功、一批真失败、一批真取消 —— 走完整条流水线，看差值。

    三种终态各用**真实**的产生方式，一个都不打桩：

    * 成功：5 张 JPEG 转 PNG，真的转出来；
    * 失败：把 ``OFFICE_LOCK_WAIT_SECONDS`` 调到 1 秒并自己握住转换锁，
      ``ConverterUnavailableError`` 由真实的锁等待超时抛出 ——
      这是**处理器内部**的失败，与「提交时就被拒的坏文件」不同，
      它是唯一能真正走到 worker 再落定的失败路径；
    * 取消：文档批次提交后立刻取消（沿用第七阶段那条已被验证的配方）。

    提交阶段就判死的项（坏文件、不支持的组合）**不进**这些计数 ——
    它们一个字节的活都没干，记进去就成了「处理失败」的假账。
    """
    before = get_json(client, METRICS)

    # -- 成功 5 项 --
    images = tuple(
        (f"metric-{index}.jpg", build_image_bytes(120 + index, 90, "JPEG"))
        for index in range(5)
    )
    ok = run_conversion(client, files=image_files(*images), target_type="png")
    assert ok["status"] == "completed", ok["error"]
    assert ok["completed"] == 5

    # -- 失败 2 项 --
    monkeypatch.setattr(settings, "OFFICE_LOCK_WAIT_SECONDS", 1)
    lock = office_converter._LAUNCHER_LOCK
    assert lock.acquire(timeout=5), "拿不到转换锁，说明有别的转换在跑"
    try:
        files = tuple(
            (f"busy-{index}.docx", build_docx_bytes(f"BUSY{index}")) for index in range(2)
        )
        bad = run_conversion(
            client, files=office_files(*files, field="files"), target_type="pdf"
        )
    finally:
        lock.release()

    assert bad["status"] == "failed", bad["error"]
    assert bad["failed"] == 2
    assert bad["completed"] == 0
    for index in range(2):
        item = item_of(bad, index)
        assert item["status"] == "failed"
        assert item["error_code"] == ErrorCode.PROCESSING_FAILED
        # 「组件忙」是可重试的服务器侧问题，但不属于那 3 个**自动**重试码
        assert item["auto_retry_count"] == 0

    # -- 取消 CANCEL_BATCH_SIZE 项 --
    files = tuple(
        (f"cancel-{index}.docx", build_docx_bytes(f"CANCEL{index}"))
        for index in range(CANCEL_BATCH_SIZE)
    )
    response = submit_conversion(
        client, files=office_files(*files, field="files"), target_type="pdf"
    )
    assert response.status_code == 202, response.text
    batch_id = response.json()["batch_id"]
    cancelled = client.post(f"{ENDPOINT}/{batch_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    stopped = wait_conversion(client, batch_id, timeout=120.0)
    assert stopped["status"] == "cancelled", stopped["error"]
    assert stopped["cancelled"] == CANCEL_BATCH_SIZE

    after = get_json(client, METRICS)
    delta = {
        key: after["tasks"][key] - before["tasks"][key]
        for key in ("total", "started", "completed", "failed", "cancelled")
    }

    assert delta["completed"] == 5, delta
    assert delta["failed"] == 2, delta
    assert delta["cancelled"] == CANCEL_BATCH_SIZE, delta
    assert delta["total"] == 5 + 2 + CANCEL_BATCH_SIZE, delta
    assert delta["total"] == delta["completed"] + delta["failed"] + delta["cancelled"]

    # ``started`` 是「真的开工了」，**不是**「进了队列」。
    #
    # 这两者混起来后果很具体：``started - completed`` 会被读成「有多少在跑」，
    # 而那个数该由池的 ``active`` 提供。这里 office 池只有 1 个 worker，
    # 所以取消那一批里最多 1 项真的开过工 —— 其余 CANCEL_BATCH_SIZE - 1 项
    # 在排队时就被取消了，它们的 started 必须是 0。
    #
    # 用区间而不是定值：到底取消请求赶到时已经有几项开工，取决于调度，
    # 那是个真实的竞态，不该被一条测试钉成一个假精确的数字。
    assert 5 + 2 <= delta["started"] <= 5 + 2 + settings.OFFICE_WORKERS, delta
    assert delta["started"] < 5 + 2 + CANCEL_BATCH_SIZE, "排队中被取消的项不该算「已开始」"

    # 分类统计也要跟着动，而且只动该动的池
    def bucket(snapshot: dict, name: str) -> dict:
        return snapshot["by_type"].get(name, {})

    for name, expected in (("image", 5), ("office", 2 + CANCEL_BATCH_SIZE)):
        assert (
            bucket(after, name).get("count", 0) - bucket(before, name).get("count", 0)
            == expected
        ), name
    assert (
        bucket(after, "office")["failed"] - bucket(before, "office")["failed"] == 2
    )
    assert (
        bucket(after, "office")["cancelled"] - bucket(before, "office")["cancelled"]
        == CANCEL_BATCH_SIZE
    )

    # 有耗时的样本数只能来自**真的跑过**的项。排队中被取消的项一个字节都没处理，
    # 给它们编一个 0 会把平均值往下拽 —— 所以下界是「5 成功 + 2 失败」，
    # 上界再算上取消那一批里真的开过工的那几项。
    duration_delta = after["duration_ms"]["count"] - before["duration_ms"]["count"]
    assert 5 + 2 <= duration_delta <= 5 + 2 + settings.OFFICE_WORKERS, duration_delta

    # 最长耗时只会涨不会跌
    previous_max = before["duration_ms"]["max"]
    if previous_max is not None:
        assert after["duration_ms"]["max"] >= previous_max


# ======================================================================
# 五、开关：FILETOOLS_SYSTEM_API=0 时接口**不存在**
# ======================================================================

#: 子进程里跑的一段探针：既看**路由注册表**，也真的发一次 HTTP 请求。
#:
#: 为什么非要用子进程：路由是在 ``import main`` 那一刻按 ``SYSTEM_API_ENABLED``
#: 决定注不注册的。当前进程里 main 早就 import 完了，改 settings 再断言
#: 就成了「测我自己刚改的那个变量」，什么也没验证。只有真的用另一个环境
#: 变量重新 import 一次，验的才是**启动路径**本身。
#:
#: 为什么要看 ``openapi()['paths']`` 而不是 ``app.routes``：这个 FastAPI 版本
#: 把 include 进来的路由包在 ``_IncludedRouter`` 里，``app.routes`` 上根本
#: 看不到嵌套路径（第一版探针就是这么被骗过去的 —— 它回报「一个都没有」，
#: 而接口其实是好的）。
_PROBE = """
import json
import main
from fastapi.testclient import TestClient

with TestClient(main.app) as client:
    http = {}
    for path in ("/api/system/workers", "/api/system/metrics"):
        response = client.get(path)
        http[path] = {
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
        }
print("PROBE=" + json.dumps({
    "paths": sorted(p for p in main.app.openapi()["paths"] if p.startswith("/api/system")),
    "http": http,
}))
"""


def probe_system_api(*, enabled: bool) -> dict:
    """在**另一个进程**里 import main，回报路由表与两个接口的真实响应。"""
    env = dict(os.environ)
    env["FILETOOLS_SYSTEM_API"] = "1" if enabled else "0"
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    # 子进程的 stdout 里除了这一行还有别的输出，所以是「找那一行」而不是
    # 「读第一行」；解码用 replace，免得一行非 ASCII 的意外输出把测试炸掉。
    stdout = result.stdout.decode("utf-8", "replace")
    for line in stdout.splitlines():
        if line.startswith("PROBE="):
            return json.loads(line[len("PROBE=") :])
    raise AssertionError(f"子进程没有回报结果，stdout={stdout!r}")


def test_system_api_is_on_by_default(client: TestClient) -> None:
    """默认开着（用户选定）：验收脚本跑在**已经起好**的实例上，改不了它的环境变量。"""
    assert settings.SYSTEM_API_ENABLED is True
    assert client.get(WORKERS).status_code == 200
    assert client.get(METRICS).status_code == 200


def test_system_api_can_be_switched_off_entirely() -> None:
    """``FILETOOLS_SYSTEM_API=0`` → 两个接口**整个不存在**。

    验的是「接口没了」，不是「接口拒绝你」：路由不注册，于是没有 JSON、
    没有 schema、没有数据 —— 连「这里本来有个运行状态接口」都不必让人知道。

    **一处与计划书不符的实情**（探针查出来的，不是猜的）：计划书写的是
    「404」，但生产形态下后端顺带托管着 ``frontend/dist``，那个 SPA 兜底
    会把**所有**未匹配的路径回落到 ``index.html``。所以关掉之后真实响应是
    ``200 text/html``（774 字节的静态外壳），而不是 404 —— 这与
    ``/api/随便什么`` 今天的行为完全一致，不是这个开关的特例。
    没挂前端（纯 API 部署）时才是真的 404。

    两种情况下要的东西是一样的：**API 面消失**。所以这里断言的是
    「响应不是我们的 JSON」，而不是一个会随部署形态变化的数字。
    """
    on = probe_system_api(enabled=True)
    assert on["paths"] == ["/api/system/metrics", "/api/system/workers"]
    for path, info in on["http"].items():
        assert info["status"] == 200, (path, info)
        assert "application/json" in info["content_type"], (path, info)

    off = probe_system_api(enabled=False)
    assert off["paths"] == []
    for path, info in off["http"].items():
        assert "application/json" not in info["content_type"], (path, info)
        assert info["status"] in (200, 404), (path, info)


def test_process_level_metrics_singleton_is_the_one_serving_http(client: TestClient) -> None:
    """接口报的就是那个进程级单例 —— 别哪天不小心接了个副本上去。"""
    body = get_json(client, METRICS)
    assert body["tasks"]["total"] == metrics.snapshot()["tasks"]["total"]
    assert body["workers"] == metrics.snapshot()["workers"]
