"""运行状态接口（第八阶段 §二十六 / §二十七）。

两个只读接口，给运维（以及验收脚本）一个**不猜**的方式回答两个问题：

* ``GET /api/system/workers`` —— 现在有几个 worker、分别在哪个池、
  有几个在忙、各自忙了多久、队列里还压着多少条；
* ``GET /api/system/metrics`` —— 从启动到现在累计处理了多少、失败多少、
  平均与最长耗时、自动重试了多少次、回收过几个 worker。

## 为什么不给文件名、任务号、路径

``group_id`` 是 ``/api/tasks/{group_id}`` 的凭据，而那个接口会返回
用户的文件名 —— 把任务号放进这里，这个接口就成了第二个泄露面。
所以这里**只有槽位与聚合数字**，一个能关联到具体用户的字段都没有。
这不是省事，是刻意让「泄露」无从谈起：没有地方放。

## 两个接口都是 GET、无请求体

所以不需要进 ``main.py`` 的 ``_BATCH_PATHS``（那是个上传体量的白名单）。

## 关掉的方式

``FILETOOLS_SYSTEM_API=0`` 时 ``main.py`` **不注册**这个路由 ——
于是路径不存在（404），而不是「存在但返回 403」。一个不存在的接口
比一个会拒绝的接口更少话可说。
"""

from __future__ import annotations

from fastapi import APIRouter

from services.queue_service import task_queue
from services.worker_pool import timeout_table
from utils.metrics import metrics

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/workers", summary="各资源池与 worker 的实时状态")
async def workers_endpoint() -> dict:
    """当前 worker 池的实时快照。

    数字全部来自进程内的真实现状：``active`` 是**真的**在跑，
    ``queue_size`` 是**真的**还在排队 —— 没有估算、没有平滑、
    没有「大概还有 30%」。
    """
    snapshot = task_queue.pool_snapshot()
    return {
        **snapshot,
        # 池超时是配置、是常量，不是运行时状态，但运维要判断
        # 「这个池为什么 300 秒才超时」时必须能一眼看到它的来历。
        "timeouts": timeout_table(),
    }


@router.get("/metrics", summary="任务处理的累计指标")
async def metrics_endpoint() -> dict:
    """从进程启动到现在的累计计数与耗时。

    只报聚合值，且**重启即清零** —— 这里没有持久化，也不假装有。
    ``tasks.timeout`` 与 ``tasks.lost`` 是 ``tasks.failed`` 的**子集**，
    不能加进 ``tasks.total``（加了就对不上了）。
    """
    return metrics.snapshot()
