"""PDF → Word 的真实阶段进度（第六阶段 A）。

规格里明确要求：**不要用假的固定进度**，拿不到准确百分比就用阶段状态。
所以这里存的是一串真实的阶段名，加上 OCR 期间真实的「第几页 / 共几页」——
那是唯一一个能诚实算出来的百分比。

**只存 ``{stage, page, page_count}``，不存文件名、不存大小。**
进度 id 是前端生成并直接摆在请求里的，猜到别人的 id 也只能看出
「有人在转一份 12 页的 PDF」，看不出是谁的、转的是什么。

和 ``job_store`` 一样是单进程内存态。这是现有架构的既有约束
（后端本来就是单进程），不是这一阶段新引入的。
"""

from __future__ import annotations

import re
import threading
import time

__all__ = [
    "STAGE_ANALYZING",
    "STAGE_DETECTING",
    "STAGE_EXTRACTING",
    "STAGE_OCR",
    "STAGE_VERIFYING",
    "STAGE_WRITING",
    "ProgressStore",
    "progress_store",
    "valid_progress_id",
]

#: 分阶段状态。顺序就是实际发生的顺序。
STAGE_ANALYZING = "analyzing"    # 正在分析 PDF...
STAGE_DETECTING = "detecting"    # 正在检测文字层...
STAGE_EXTRACTING = "extracting"  # 正在提取内容...
STAGE_OCR = "ocr"                # 正在进行 OCR...
STAGE_WRITING = "writing"        # 正在生成 Word...
STAGE_VERIFYING = "verifying"    # 正在验证 Word...

#: 进度条目的存活时间。10 分钟足够一次转换跑完并且前端把最后几帧读走；
#: 再久就是泄漏 —— 用户关掉页面之后没人会来读这些条目。
DEFAULT_TTL_SECONDS = 600

#: 进度 id 的形状。由前端生成，所以必须当成不可信输入：
#: 限定字符集和长度，避免有人拿超长字符串把内存撑起来。
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def valid_progress_id(value: str | None) -> bool:
    """进度 id 形状是否合法。不合法一律当作「不存在」，不报参数错误。"""
    return bool(value) and bool(_ID_PATTERN.match(value))


class ProgressStore:
    """带 TTL 的阶段进度表。

    ``max_entries`` 是硬上限：即使有人拿一堆合法 id 不停地开新条目，
    也不会把内存吃光 —— 满了就先把过期的清掉，还满就丢弃最旧的一条。
    """

    __slots__ = ("_entries", "_lock", "_ttl", "_max_entries")

    def __init__(self, *, ttl: int = DEFAULT_TTL_SECONDS, max_entries: int = 512) -> None:
        self._entries: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._max_entries = max_entries

    # -- 写 --------------------------------------------------------------

    def start(self, progress_id: str, *, page_count: int | None = None) -> None:
        """登记一次转换，初始阶段是「正在分析 PDF」。"""
        if not valid_progress_id(progress_id):
            return
        self._put(
            progress_id,
            {"stage": STAGE_ANALYZING, "page": None, "page_count": page_count},
        )

    def update(
        self,
        progress_id: str,
        stage: str,
        *,
        page: int | None = None,
        page_count: int | None = None,
    ) -> None:
        """推进到下一个阶段。

        还没登记过的 id 会顺手补一条（而不是丢弃）—— 服务里
        ``start`` 与实际处理之间只隔几毫秒，前端可能刚好卡在中间来读。
        """
        if not valid_progress_id(progress_id):
            return
        with self._lock:
            current = self._entries.get(progress_id)
        state = dict(current[1]) if current else {"stage": stage, "page": None, "page_count": None}
        state["stage"] = stage
        if page is not None:
            state["page"] = page
        if page_count is not None:
            state["page_count"] = page_count
        self._put(progress_id, state)

    def finish(self, progress_id: str) -> None:
        """转换结束，立刻删掉。前端这时候已经拿到最终响应，不需要再轮询了。"""
        if not valid_progress_id(progress_id):
            return
        with self._lock:
            self._entries.pop(progress_id, None)

    def _put(self, progress_id: str, state: dict) -> None:
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            if len(self._entries) >= self._max_entries:
                # 还满就丢最旧的一条：正常使用下这里永远不会触发
                oldest = min(self._entries, key=lambda key: self._entries[key][0])
                self._entries.pop(oldest, None)
            self._entries[progress_id] = (now, state)

    # -- 读 --------------------------------------------------------------

    def get(self, progress_id: str) -> dict | None:
        """取一条进度。已过期、形状不合法、没登记过，一律返回 None。"""
        if not valid_progress_id(progress_id):
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(progress_id)
            if entry is None:
                return None
            if now - entry[0] > self._ttl:
                self._entries.pop(progress_id, None)
                return None
            return dict(entry[1])

    def purge_expired(self) -> int:
        """清掉过期条目，返回清掉的条数。交给后台清理任务定期调用。"""
        with self._lock:
            return self._purge_locked(time.monotonic())

    def _purge_locked(self, now: float) -> int:
        stale = [
            key for key, (stamp, _) in self._entries.items() if now - stamp > self._ttl
        ]
        for key in stale:
            self._entries.pop(key, None)
        return len(stale)

    def __len__(self) -> int:  # pragma: no cover - 排查用
        with self._lock:
            return len(self._entries)


#: 全局唯一实例。路由和服务都用它。
progress_store = ProgressStore()
