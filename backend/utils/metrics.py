"""任务与 worker 的聚合指标（第八阶段 §二十六）。

只存**聚合值** —— 计数、总耗时、最大耗时。不保留任何逐项明细，
于是这个模块天然不可能泄露文件名或路径：它压根没有地方放。

这一点是刻意的，不是省事：一份「最近 50 个任务」的明细表看起来更好用，
但它会立刻变成第二个需要防泄露的地方，而第一个（``/api/system/workers``）
已经刻意做成无身份信息了。

计数与快照都在 ``threading.Lock`` 里做。今天所有写入都发生在事件循环
线程上，锁是廉价的保险 —— 加了不会错，不加则要依赖一个将来可能
不再成立的前提。

本模块**不 import services 层**：队列深度、活跃 worker 数这类实时状态
由调用方（路由）从池里取，与这里的累计值拼在一起。utils 不能反过来
依赖 services。
"""

from __future__ import annotations

import threading
import time

#: 终态。与 services/queue_service.py 的 STATE_* 一一对应，
#: 但这里用**对外的**说法（completed 而不是 done），指标是给运维看的。
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

#: 归不到任何已知资源类时的桶名。**不丢弃**这类样本 ——
#: 一个统计不到的类别，比一个叫「other」的类别难查得多。
RESOURCE_OTHER = "other"


class MetricsRegistry:
    """进程内的累计指标。重启即清零（不伪造持久化，见交付报告）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        # 计数
        self._started = 0
        self._completed = 0
        self._failed = 0
        self._cancelled = 0
        self._timeout = 0
        self._lost = 0
        self._retried_auto = 0
        self._retried_manual = 0
        self._recycled = 0
        self._watchdog_kicks = 0
        # 耗时（毫秒）。只累加，不落明细。
        self._duration_count = 0
        self._duration_sum = 0
        self._duration_max = 0
        self._duration_min: int | None = None
        # 分类统计
        self._by_type: dict[str, dict[str, int | None]] = {}

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def record_started(self, resource_class: str) -> None:
        """一个任务项真的开工了（**不是**「进了队列」）。

        排队等待不算开工 —— 否则「已开始」减去「已完成」会被读成
        「有多少在跑」，而那个数应该由池的 active 提供。
        """
        with self._lock:
            self._started += 1

    def record_settled(
        self,
        *,
        status: str,
        resource_class: str,
        duration_ms: int | None = None,
        error_code: str | None = None,
    ) -> None:
        """一个任务项落定（成功 / 失败 / 取消）。**只应该被调用一次**。"""
        with self._lock:
            if status == STATUS_COMPLETED:
                self._completed += 1
            elif status == STATUS_CANCELLED:
                self._cancelled += 1
            else:
                self._failed += 1
            if error_code == "TASK_TIMEOUT":
                self._timeout += 1

            bucket = self._bucket(resource_class)
            bucket["count"] = int(bucket["count"] or 0) + 1
            if status == STATUS_CANCELLED:
                bucket["cancelled"] = int(bucket["cancelled"] or 0) + 1
            elif status != STATUS_COMPLETED:
                bucket["failed"] = int(bucket["failed"] or 0) + 1
            if error_code == "TASK_TIMEOUT":
                bucket["timeout"] = int(bucket["timeout"] or 0) + 1

            if duration_ms is None:
                return
            self._duration_count += 1
            self._duration_sum += duration_ms
            self._duration_max = max(self._duration_max, duration_ms)
            if self._duration_min is None or duration_ms < self._duration_min:
                self._duration_min = duration_ms
            bucket["total_ms"] = int(bucket["total_ms"] or 0) + duration_ms
            bucket["count_ms"] = int(bucket["count_ms"] or 0) + 1
            bucket["max_ms"] = max(int(bucket["max_ms"] or 0), duration_ms)

    def record_retry(self, *, automatic: bool) -> None:
        """一次重试。``automatic=False`` 就是用户点了重试按钮。"""
        with self._lock:
            if automatic:
                self._retried_auto += 1
            else:
                self._retried_manual += 1

    def record_worker_lost(self) -> None:
        """一个 worker 被判为失联（协程死掉，或卡死到超过任何合理时限）。"""
        with self._lock:
            self._lost += 1

    def record_worker_recycled(self) -> None:
        """一个 worker 被重新拉起，槽位没有永久丢失。"""
        with self._lock:
            self._recycled += 1

    def record_watchdog_kick(self) -> None:
        """看门狗做了一次回收动作（与「回收成功」分开计，
        这样「一直在踢但踢不动」也能被看出来）。"""
        with self._lock:
            self._watchdog_kicks += 1

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def _bucket(self, resource_class: str) -> dict[str, int | None]:
        """取（必要时新建）某个资源类的桶。"""
        key = resource_class or RESOURCE_OTHER
        bucket = self._by_type.get(key)
        if bucket is None:
            bucket = {
                "count": 0,
                "failed": 0,
                "cancelled": 0,
                "timeout": 0,
                "total_ms": 0,
                "count_ms": 0,
                "max_ms": 0,
            }
            self._by_type[key] = bucket
        return bucket

    def snapshot(self) -> dict:
        """当前累计值的一份**深拷贝**，调用方改它不会影响登记表。"""
        with self._lock:
            total = self._completed + self._failed + self._cancelled
            by_type: dict[str, dict] = {}
            for name, bucket in self._by_type.items():
                count_ms = int(bucket["count_ms"] or 0)
                by_type[name] = {
                    "count": int(bucket["count"] or 0),
                    "failed": int(bucket["failed"] or 0),
                    "cancelled": int(bucket["cancelled"] or 0),
                    "timeout": int(bucket["timeout"] or 0),
                    "avg_ms": (
                        round(int(bucket["total_ms"] or 0) / count_ms, 1)
                        if count_ms
                        else None
                    ),
                    "max_ms": int(bucket["max_ms"] or 0) if count_ms else None,
                }
            return {
                "uptime_seconds": round(time.time() - self._started_at, 1),
                "tasks": {
                    "total": total,
                    "started": self._started,
                    "completed": self._completed,
                    "failed": self._failed,
                    "cancelled": self._cancelled,
                    # timeout / lost 是 failed 的子集，单独列出只为可读性。
                    # 它们**不能**再加进 total，否则计数就对不上了。
                    "timeout": self._timeout,
                    "lost": self._lost,
                },
                "retries": {
                    "automatic": self._retried_auto,
                    "manual": self._retried_manual,
                },
                "workers": {
                    "recycled": self._recycled,
                    "watchdog_kicks": self._watchdog_kicks,
                },
                "duration_ms": {
                    "count": self._duration_count,
                    "avg": (
                        round(self._duration_sum / self._duration_count, 1)
                        if self._duration_count
                        else None
                    ),
                    "max": self._duration_max if self._duration_count else None,
                    "min": self._duration_min,
                },
                "by_type": by_type,
            }

    def reset(self) -> None:
        """清零。只在测试里用 —— 指标是进程级的，测试之间要能各算各的。

        刻意只清计数、**不换锁**：``__init__`` 会把 ``_lock`` 整个换掉，
        正在别处等锁的线程就会拿着旧锁和新对象各说各话。
        """
        with self._lock:
            self._started = 0
            self._completed = 0
            self._failed = 0
            self._cancelled = 0
            self._timeout = 0
            self._lost = 0
            self._retried_auto = 0
            self._retried_manual = 0
            self._recycled = 0
            self._watchdog_kicks = 0
            self._duration_count = 0
            self._duration_sum = 0
            self._duration_max = 0
            self._duration_min = None
            self._by_type.clear()
            self._started_at = time.time()


#: 全局单例。与 ``task_queue`` / ``job_store`` 同一个模式。
metrics = MetricsRegistry()
