"""结果文件的登记簿（内存实现）。

第一阶段不引入数据库：处理完成后把结果文件放在临时目录，
用一个随机 token 登记，前端凭 token 下载。下载后立即删除，
未下载的过期后由后台线程清理。

进程重启后登记表会清空，对应的临时目录由系统临时目录策略回收 ——
这符合「不长期保存用户文件」的定位。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from config import settings
from utils.files import remove_dir

logger = logging.getLogger(__name__)

# 任务类型。result 是处理结果，可以下载；input 是用户上传的原始文件，
# 只供后续操作读取，不能通过下载接口取回（下载会删掉它）。
KIND_RESULT = "result"
KIND_INPUT = "input"


@dataclass(slots=True)
class Job:
    """一次处理任务的结果。"""

    job_id: str
    directory: Path          # 该任务独占的临时目录
    path: Path               # 结果文件
    filename: str            # 下载时展示的文件名
    media_type: str
    size: int
    kind: str = KIND_RESULT  # result：可下载的结果；input：用户上传的原始文件
    ttl: int | None = None   # 该任务自己的存活时长，None 表示用全局 JOB_TTL_SECONDS
    created_at: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)

    def is_expired(self, ttl: int) -> bool:
        return (time.time() - self.created_at) > self.lifetime(ttl)

    def lifetime(self, default_ttl: int) -> int:
        """本任务实际使用的存活时长。"""
        return default_ttl if self.ttl is None else self.ttl


class JobStore:
    """线程安全的任务登记簿。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def add(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        """按 token 取任务，已过期的视为不存在。"""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.is_expired(settings.JOB_TTL_SECONDS):
            self.discard(job_id)
            return None
        return job

    def discard(self, job_id: str) -> None:
        """删除登记并清理磁盘文件。"""
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is not None:
            remove_dir(job.directory)

    def purge_expired(self) -> int:
        """清理所有过期任务，返回清理数量。"""
        now = time.time()
        ttl = settings.JOB_TTL_SECONDS
        with self._lock:
            expired = [
                jid
                for jid, job in self._jobs.items()
                if (now - job.created_at) > job.lifetime(ttl)
            ]
            jobs = [self._jobs.pop(jid) for jid in expired]
        for job in jobs:
            remove_dir(job.directory)
        return len(jobs)

    def purge_all(self) -> None:
        """清空所有任务，用于服务关闭时兜底清理。"""
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            remove_dir(job.directory)

    def count(self) -> int:
        with self._lock:
            return len(self._jobs)

    def live_dirs(self) -> set[Path]:
        """当前登记在册的临时目录，供孤儿目录清理跳过（§14）。"""
        with self._lock:
            return {job.directory for job in self._jobs.values()}


# 全局单例
job_store = JobStore()


class CleanupWorker:
    """定期清理过期临时文件与任务记录的后台线程。

    「还要清什么」由各个模块自己注册进来（:meth:`add_sweeper`）——
    清理线程不去反向依赖它们（任务队列本身就依赖着本模块，
    直接 import 会形成循环）。
    """

    def __init__(self, interval_seconds: int | None = None) -> None:
        self._interval = interval_seconds or settings.CLEANUP_INTERVAL_SECONDS
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sweepers: list[tuple[str, Callable[[], int]]] = []

    def add_sweeper(self, name: str, sweep: Callable[[], int]) -> None:
        """注册一个清理动作：无参数，返回清理掉的数量。"""
        if any(existing is sweep for _n, existing in self._sweepers):
            return
        self._sweepers.append((name, sweep))

    def sweep_once(self) -> int:
        """跑一遍全部清理动作，返回清理总数。"""
        removed = job_store.purge_expired()
        if removed:
            logger.info("已清理 %d 个过期的结果文件", removed)

        for name, sweep in self._sweepers:
            try:
                count = sweep()
            except Exception:  # pragma: no cover - 单个清理动作失败不影响其它
                logger.exception("清理%s时出错", name)
                continue
            if count:
                logger.info("已清理 %d 个过期%s", count, name)
            removed += count
        return removed

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="filetools-cleanup", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.sweep_once()
            except Exception:  # pragma: no cover - 清理失败不应终止线程
                logger.exception("清理过期内容时出错")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None


cleanup_worker = CleanupWorker()
