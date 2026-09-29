"""任务状态接口（第四阶段 §3 / §4 / §6）。

批量接口不再「一个请求等到处理完」，而是立刻返回 ``group_id``，
前端按 ``status_url`` 轮询任务状态，拿到：

- 整批进度：总数 / 已完成 / 处理中 / 等待 / 失败 + 百分比（§3）
- 每个文件的名称、大小、状态与失败原因（§4）
- 处理完成后的一次性下载地址（§6）

轮询本身**永远返回 200**（只要任务还在）：某个文件失败是任务的结果，
不是这次查询的 HTTP 错误 —— 把两者混在一起，前端就要在两层里各写一份错误处理。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from services.queue_service import TaskGroup, task_queue
from utils.errors import TaskNotFoundError

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreatedResponse(BaseModel):
    """提交批量任务后的回执。"""

    group_id: str
    tool: str = Field(description="工具标识，例如 image.convert")
    label: str = Field(description="工具中文名，用于「最近处理」记录")
    total: int = Field(description="本批文件总数")
    status_url: str = Field(description="轮询任务状态的地址")


class TaskFileResponse(BaseModel):
    """批量任务中的单个文件（§4）。"""

    index: int = Field(description="对应上传顺序，从 0 开始")
    filename: str
    size: int = Field(description="原始文件字节数")
    state: str
    state_label: str = Field(
        description="状态的中文名：等待中 / 处理中 / 已完成 / 失败 / 已取消"
    )
    error_code: str | None = None
    error_message: str | None = None
    result: dict | None = Field(
        default=None, description="该文件的处理结果摘要，处理完成后才有"
    )
    source_type: str = Field(default="", description="转换中心：源格式，其它工具为空")
    target_type: str = Field(default="", description="转换中心：目标格式，其它工具为空")
    retry_count: int = Field(default=0, description="已经手动重试过几次")
    can_retry: bool = Field(default=False, description="现在能不能重试这一项")
    auto_retry_count: int = Field(
        default=0,
        description=(
            "服务器自己重排过几次队（第八阶段）。与 retry_count 是两回事："
            "那个记的是用户点的手动重试，这个记的是服务器遇到临时故障后的自动重试"
        ),
    )
    timeout_requested: bool = Field(
        default=False,
        description=(
            "这一轮被服务器判过超时（第八阶段）。**不代表活真的停了** —— "
            "超时只释放了队列槽位，底层线程会继续跑完"
        ),
    )


class TaskErrorResponse(BaseModel):
    """整批失败时的原因。"""

    code: str
    message: str


class TaskSnapshotResponse(BaseModel):
    """任务状态快照。"""

    group_id: str
    tool: str
    label: str
    state: str = Field(
        description="整批状态：waiting / processing / done / failed / cancelled"
    )

    total: int
    completed: int
    processing: int
    waiting: int
    failed: int
    cancelled: int = Field(default=0, description="整批取消时被取消的文件数")
    finished: int = Field(
        description="已完成 + 失败 + 已取消，用于进度条"
    )
    percent: float = Field(description="进度百分比，0-100")
    cancelling: bool = Field(
        default=False,
        description=(
            "用户请求过取消。**不代表活已经停了** —— 在跑的转换取消不了，"
            "界面应把「处理中」显示成「正在取消」（§十五）"
        ),
    )

    tasks: list[TaskFileResponse] = Field(default_factory=list)
    result: dict | None = Field(
        default=None,
        description="整批结果（与原同步接口的响应体一致）；下载过的结果会标记 expired",
    )
    error: TaskErrorResponse | None = None


def build_task_response(group: TaskGroup) -> TaskCreatedResponse:
    """提交任务后的统一回执，四个批量接口共用。"""
    return TaskCreatedResponse(
        group_id=group.group_id,
        tool=group.tool,
        label=group.label,
        total=len(group.items),
        status_url=f"/api/tasks/{group.group_id}",
    )


@router.get("/{group_id}", response_model=TaskSnapshotResponse, summary="查询任务状态")
async def task_status_endpoint(group_id: str) -> TaskSnapshotResponse:
    """按任务号查询进度；任务过期或不存在时返回 404。"""
    group = task_queue.get(group_id)
    if group is None:
        raise TaskNotFoundError("任务不存在或已过期，请重新提交")
    return TaskSnapshotResponse(**task_queue.snapshot(group))
