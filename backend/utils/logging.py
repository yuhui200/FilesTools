"""结构化任务日志（第八阶段 §二十四 / §二十五）。

任务事件只经由 :func:`log_event` 写出一条 ``event=... k=v ...`` 的记录，
字段走**白名单** —— 不在 :data:`_ALLOWED_FIELDS` 里的键一律丢掉。

把「不泄露」做成**一个函数**的性质，而不是几十个调用点各自的自觉：
后者只要有一次疏忽，用户的完整文件路径就进了日志，而且没人会发现。

永不记录（§二十五 原话）：源文件的完整路径与文件名、文件内容、
下载令牌、上传原文、服务器绝对路径。日志里只有任务号、类型、耗时、
状态与错误码。**文件名一次都不出现** —— 它能被 ``task_id`` 关联出来。

非预期异常的堆栈仍然走既有的 root logger（``logger.exception``），
那条路径从第四阶段起就是这样，本阶段不动它。

**不动 main.py 里的 basicConfig**：这里只取一个专用 logger 名，
格式交给进程已有的配置，其余模块的日志格式一行都不变。
"""

from __future__ import annotations

import logging
import time
from typing import Any

#: 任务事件的 logger 名。独立命名是为了让运维能单独按这个名字过滤，
#: 而不是为了让格式与别的模块不同。
LOGGER_NAME = "filetools.tasks"

logger = logging.getLogger(LOGGER_NAME)

# ----------------------------------------------------------------------
# 事件名（§二十四点名的就是这一套）
# ----------------------------------------------------------------------

EVENT_TASK_STARTED = "task_started"
EVENT_TASK_COMPLETED = "task_completed"
EVENT_TASK_FAILED = "task_failed"
EVENT_TASK_CANCELLED = "task_cancelled"
EVENT_TASK_TIMEOUT = "task_timeout"
EVENT_TASK_RETRIED = "task_retried"
EVENT_WORKER_STARTED = "worker_started"
EVENT_WORKER_STOPPED = "worker_stopped"
EVENT_WORKER_LOST = "worker_lost"
#: 被超时放弃的那条线程后来跑完了。**这是诚实的补记**：
#: 槽位当时就释放了，CPU 并没有。
EVENT_TASK_ABANDONED_FINISHED = "task_abandoned_finished"

# ----------------------------------------------------------------------
# 允许出现在日志里的字段
# ----------------------------------------------------------------------

#: 字段白名单。新增字段必须显式加进来 —— 这是**故意**的摩擦：
#: 顺手多打一个变量名，就是一次泄露的机会。
_ALLOWED_FIELDS: tuple[str, ...] = (
    "task_id",        # 形如 "<group_id>:<index>"，服务端内部标识
    "group_id",
    "worker_id",
    "pool",
    "resource_class",
    "source_type",
    "target_type",
    "status",
    "error_code",
    "duration_ms",
    "timeout_ms",
    "reason",
    "automatic",      # 自动重试还是手动重试
    "attempt",
    "generation",
    "recycles",
    "interruptible",  # 超时那一刻线程能不能真的被打断（进程内都是 false）
    "waiting",
    "active",
)

#: 字符串值的长度上限。正常取值（状态词、错误码、池名）都远小于它；
#: 长成这样的东西多半是路径或文件内容，宁可丢掉也不写进日志。
_MAX_VALUE_LENGTH = 64


def _format_value(value: Any) -> str | None:
    """把一个字段值转成可以安全写进日志的字符串；不安全就返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        # 只认标量。传进来一个 Path / 文件对象 / 异常，说明调用点写错了 ——
        # 丢掉它，而不是把它的 str() 写出去（Path 的 str() 就是绝对路径）。
        return None
    if len(value) > _MAX_VALUE_LENGTH:
        return None
    # 兜底：白名单字段也拦一道路径分隔符。正常取值里不会有它们
    #（池名、状态词、错误码都是大写字母 / 下划线），
    # 出现了就说明有人把路径塞进来了。
    if "\\" in value or "/" in value:
        return None
    return value


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """写一条任务事件。

    ``event`` 单独成参数而不是放进 ``fields``，是为了让「这一行是什么事件」
    不可能被调用点写漏 —— 事件名是所有过滤手段的锚点。
    """
    parts = [f"event={event}"]
    for key in _ALLOWED_FIELDS:
        if key not in fields:
            continue
        text = _format_value(fields[key])
        if text is not None:
            parts.append(f"{key}={text}")
    logger.log(level, " ".join(parts))


def duration_ms(started_at: float | None, finished_at: float | None) -> int | None:
    """两个时间戳之间的毫秒数；缺一个就返回 None（不猜、不补 0）。

    返回 ``None`` 而不是 0 很重要：0 会被读成「瞬间完成」，
    而真相是「没有开始时间」，两者在排查时是完全不同的结论。
    """
    if started_at is None or finished_at is None:
        return None
    return max(0, int((finished_at - started_at) * 1000))


def now() -> float:
    """统一的时间源，方便测试替换。"""
    return time.time()
