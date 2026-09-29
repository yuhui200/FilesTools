"""统一转换中心的响应模型（第七阶段 §八）。

词汇在这里做**唯一一次**翻译：队列内部用 ``waiting / processing / done /
failed / cancelled``（第四阶段就定下的，前端批量页面与四个测试模块都依赖），
规格 §八 要求的是 ``queued / processing / completed / failed / cancelled``。
两边都不能改名，所以映射放在这一层 —— 队列不用动，前端拿到的是规格里的词。

状态取值：

===============  ==========================
内部             对外（§八）
===============  ==========================
waiting          queued
processing       processing / **cancelling**
done             completed
failed           failed
cancelled        cancelled
===============  ==========================

``cancelling`` 不是队列里的状态，而是「处理中 + 用户已请求取消」的组合。
单独给一个取值，是为了让前端**如实**显示「已请求取消，正在等待当前文件
处理完」，而不是假装活已经停了（§十五）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.queue_service import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_PROCESSING,
    STATE_WAITING,
    TaskGroup,
    TaskItem,
)
from services.job_store import job_store
from services.progress import progress_store

#: 队列内部状态 -> 对外状态
STATUS_BY_STATE: dict[str, str] = {
    STATE_WAITING: "queued",
    STATE_PROCESSING: "processing",
    STATE_DONE: "completed",
    STATE_FAILED: "failed",
    STATE_CANCELLED: "cancelled",
}

STATUS_CANCELLING = "cancelling"


def status_of(item: TaskItem, group: TaskGroup) -> str:
    """单项对外状态。在跑且已请求取消 -> ``cancelling``。"""
    if item.state == STATE_PROCESSING and group.cancel_requested:
        return STATUS_CANCELLING
    return STATUS_BY_STATE.get(item.state, item.state)


def task_id_for(batch_id: str, index: int) -> str:
    """任务号 = 批次号 + 序号。

    合成一个字符串是为了让 ``POST .../{task_id}/retry`` 只凭它就能定位到
    具体哪一项，不必再维护一张全局 task 索引表（那会是第二份真相）。
    ``batch_id`` 是服务端生成的 token（不含冒号），所以按最后一个冒号切开是
    没有歧义的。冒号在 URL 路径段里是合法字符，不需要转义。
    """
    return f"{batch_id}:{index}"


def split_task_id(task_id: str) -> tuple[str, int] | None:
    """把 ``{batch_id}:{index}`` 拆回两半；形状不对返回 None。"""
    batch_id, separator, raw_index = task_id.rpartition(":")
    if not separator or not batch_id:
        return None
    try:
        index = int(raw_index)
    except ValueError:
        return None
    if index < 0:
        return None
    return batch_id, index


def item_progress(item: TaskItem) -> dict:
    """这一项的真实进度。

    **只报真实值**：只有 PDF → Word 会往进度表里写真实的页码
    （``progress_store``），其它转换没有可比对的刻度，于是
    ``progress`` 就是 ``None``，前端显示不确定态的动画而不是编出来的百分比
    （§八：「如果无法计算真实百分比，就不要伪造百分比」）。
    """
    progress_id = (item.payload or {}).get("progress_id")
    state = progress_store.get(progress_id) if isinstance(progress_id, str) else None

    if not state:
        return {"stage": None, "page": None, "page_count": None, "progress": None}

    page = state.get("page")
    page_count = state.get("page_count")
    percent = None
    if isinstance(page, int) and isinstance(page_count, int) and page_count > 0:
        percent = round(min(page, page_count) / page_count * 100, 1)

    return {
        "stage": state.get("stage"),
        "page": page,
        "page_count": page_count,
        "progress": percent,
    }


# ----------------------------------------------------------------------
# 能力矩阵（§七）
# ----------------------------------------------------------------------

class ConversionTargetOption(BaseModel):
    value: str = Field(description="目标格式标识，例如 jpg / pdf / docx")
    label: str = Field(description="界面显示名")
    extension: str = Field(description="结果文件扩展名")


class ConversionSourceOption(BaseModel):
    value: str = Field(description="源格式标识，例如 png / docx")
    label: str
    extensions: list[str] = Field(description="归入这一类的扩展名，例如 .jpg / .jpeg")
    targets: list[str] = Field(description="这个源格式能转成什么")


class ConversionGroupOption(BaseModel):
    key: str
    label: str
    sources: list[ConversionSourceOption]
    targets: list[str] = Field(description="这一组里出现过的全部目标格式（去重）")


class ConversionCategoryOption(BaseModel):
    value: str = Field(description="image / document / pdf")
    label: str


class ConversionFormatInfo(BaseModel):
    """系统认识的一种格式（§十六 的 ``formats[]``）。"""

    value: str = Field(description="格式标识，如 png / docx")
    label: str
    category: str = Field(description="归属类别：image / document / pdf")
    extensions: list[str] = Field(description="归入这一格式的扩展名，如 .jpg / .jpeg")
    media_type: str = Field(description="下载时使用的 MIME")
    is_source: bool = Field(description="能不能作为输入")
    is_target: bool = Field(description="能不能作为输出")


class ConversionCapabilityItem(BaseModel):
    """一条能力条目（§十四）。conversion 与 operation 共用这一个形状。

    ``options_schema`` 用裸 ``dict`` 而不是再建一套 Pydantic 模型：
    它的线上形状由 ``conversion.capability.OptionSpec.to_json`` 唯一决定，
    在这里再翻译一次就会多出一份「字段取什么名」的真相，而两份迟早会分叉。
    """

    id: str = Field(description="稳定唯一 ID，如 image.jpg-to-png / op.pdf-merge")
    source_type: str
    target_type: str
    operation_type: str = Field(description="conversion（1→1，走统一队列）/ operation（走各自接口）")
    display_name: str
    category: str = Field(description="image / document / pdf")
    group: str = Field(description="界面分组：image / office / text / pdf")
    requires: str = Field(description="需要哪个组件才能用")
    worker_pool: str = Field(description="排在哪个资源池")
    available: bool = Field(description="这台服务器现在能不能用")
    supports_batch: bool
    supports_options: bool
    supports_preview: bool
    endpoint: str | None = Field(default=None, description="operation 的执行入口")
    method: str | None = Field(default=None, description="operation 的 HTTP 方法")
    tags: list[str] = Field(default_factory=list)
    note: str | None = None
    options_schema: dict | None = Field(
        default=None,
        description=(
            '参数描述：{"version":1,"items":[{key,type,label,default,min,max,...}]}；'
            "没有可调参数时为 null"
        ),
    )
    accepts: list[str] = Field(
        default_factory=list,
        description=(
            "**只有 operation 非空**：这份操作接受哪些源类型的文件。"
            "文件放行清单认它，不认 source_type —— 那是契约要求的单个标量，"
            "而「图片合成 PDF」收的是七种图片"
        ),
    )
    input_field: str | None = Field(
        default=None,
        description=(
            "**只有 operation 非空**：文件怎么交给 endpoint。"
            "files=直接作为 multipart 文件字段发过去（可多份）；"
            "input_id=先 POST /api/pdf/upload 换令牌再调用（一次一份）"
        ),
    )


class ConversionCapabilitiesResponse(BaseModel):
    """服务器当前真的能做的转换。**不可用的能力不会出现在 matrix 里**。

    前七个键是第七阶段就有的，第九阶段**一字未改**（新前端除外，
    旧前端继续照常工作）；后四个键是第九阶段新增的能力目录（§十五/§十六）。
    """

    matrix: dict[str, list[str]] = Field(description="源格式 -> 可选目标格式")
    groups: list[ConversionGroupOption]
    targets: list[ConversionTargetOption]
    office_available: bool = Field(description="是否装了 LibreOffice（决定 Office 转 PDF）")
    pdf_to_word_available: bool = Field(description="是否能生成 Word（只看 python-docx）")
    ocr_available: bool = Field(description="是否启用了文字识别")
    notes: list[str] = Field(
        default_factory=list, description="能力缺失的原因说明；不缺就是空数组"
    )
    pdf_to_word_note: str = Field(description="PDF 转 Word 的 OCR 提示")
    # ---- 第九阶段新增 ----
    categories: list[ConversionCategoryOption] = Field(
        default_factory=list, description="全部能力类别"
    )
    formats: list[ConversionFormatInfo] = Field(
        default_factory=list, description="系统认识的格式及其扩展名 / MIME"
    )
    conversions: list[ConversionCapabilityItem] = Field(
        default_factory=list,
        description=(
            "全部已登记的转换（含当前不可用的，用 available 标注）。"
            "与 matrix 的差异见 conversion_service.conversion_capabilities 的说明"
        ),
    )
    operations: list[ConversionCapabilityItem] = Field(
        default_factory=list, description="全部已登记的工具类操作（合并 / 拆分 / 压缩等）"
    )


# ----------------------------------------------------------------------
# 提交（§二十八）
# ----------------------------------------------------------------------

class ConversionTaskRef(BaseModel):
    task_id: str
    index: int
    filename: str
    status: str = Field(description="新建的任务一律是 queued")


class ConversionBatchCreatedResponse(BaseModel):
    batch_id: str
    total: int
    tasks: list[ConversionTaskRef]
    status_url: str
    progress_url: str
    cancel_url: str


# ----------------------------------------------------------------------
# 状态（§八）
# ----------------------------------------------------------------------

class ConversionTaskStatus(BaseModel):
    task_id: str
    index: int
    source_filename: str
    source_type: str = Field(description="服务端按真实内容判定的源格式")
    target_type: str
    conversion_id: str = Field(
        default="",
        description=(
            "命中的能力 ID（第九阶段 §二十三），如 image.jpg-to-png。"
            "仅供界面把结果映射回它当初选的那条能力，**不参与执行**"
        ),
    )
    options: dict | None = Field(
        default=None,
        description=(
            "这一项提交的参数（已通过校验的扁平点号键）；"
            "没有记录时为 null —— 不要把它当成「参数为空」"
        ),
    )
    status: str = Field(
        description="queued / processing / cancelling / completed / failed / cancelled"
    )
    progress: float | None = Field(
        default=None, description="**真实**百分比；无从得知时为 null，不要伪造"
    )
    stage: str | None = Field(default=None, description="真实阶段，例如 ocr / writing")
    page: int | None = None
    page_count: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    result: dict | None = Field(default=None, description="成功后的结果摘要")
    retry_count: int = 0
    can_retry: bool = Field(default=False, description="现在能不能重试这一项")
    # 第八阶段（纯追加，向后兼容）。
    #
    # ``auto_retry_count`` 与 ``retry_count`` 是**两件事**：后者是用户点了
    # 重试按钮的次数（前端「已重试过」的文案靠它），前者是服务器自己重排的
    # 次数。合成一个字段会让「用户按了两次」和「服务器替你重排了两次」
    # 长得一模一样，而这两件事对用户的意思完全不同。
    auto_retry_count: int = Field(
        default=0, description="服务器自动重新排队过几次（与用户点的 retry_count 分开计）"
    )
    timeout_requested: bool = Field(
        default=False,
        description=(
            "这一项被判过超时。**不代表活已经停了** —— 超时只释放槽位，"
            "底层线程会跑完，所以如实说「已请求超时」而不是「已停止」（§十七）"
        ),
    )


class ConversionBatchError(BaseModel):
    code: str
    message: str


class ConversionBatchStatusResponse(BaseModel):
    batch_id: str
    status: str = Field(
        description="整批状态：queued / processing / completed / failed / cancelled"
    )
    cancelling: bool = Field(
        default=False,
        description=(
            "用户请求过取消。**不代表活已经停了** —— 正在跑的转换取消不了，"
            "界面应显示「正在取消」而不是「已取消」（§十五）"
        ),
    )
    progress: float = Field(description="整批进度：已定下来的文件数 / 总数，0-100")
    total: int
    queued: int
    processing: int
    completed: int
    failed: int
    cancelled: int
    tasks: list[ConversionTaskStatus]
    result: dict | None = Field(
        default=None,
        description="整批结果；其中 download_url 是整批入口（1 个成功=该文件，≥2 个=ZIP）",
    )
    error: ConversionBatchError | None = None


class ConversionProgressResponse(BaseModel):
    """轻量进度，供轮询时少传数据（§二十八）。"""

    batch_id: str
    status: str
    cancelling: bool
    progress: float
    total: int
    queued: int
    processing: int
    completed: int
    failed: int
    cancelled: int


def build_batch_status(group: TaskGroup) -> ConversionBatchStatusResponse:
    """把队列里的任务组翻译成对外的批次状态。"""
    counts = group.counts()
    finished = counts["completed"] + counts["failed"] + counts["cancelled"]
    total = counts["total"]
    percent = round(finished / total * 100, 1) if total else 0.0

    result = None
    if group.summary is not None:
        result = dict(group.summary)
        if group.job_id is not None:
            alive = job_store.get(group.job_id) is not None
            result["download_url"] = group.download_url if alive else None
            result["expired"] = not alive

    error = None
    if group.error_code:
        error = ConversionBatchError(
            code=group.error_code, message=group.error_message or ""
        )

    return ConversionBatchStatusResponse(
        batch_id=group.group_id,
        status=STATUS_BY_STATE.get(group.state, group.state),
        cancelling=group.cancel_requested,
        progress=percent,
        total=total,
        queued=counts["waiting"],
        processing=counts["processing"],
        completed=counts["completed"],
        failed=counts["failed"],
        cancelled=counts["cancelled"],
        tasks=[
            ConversionTaskStatus(
                task_id=task_id_for(group.group_id, item.index),
                index=item.index,
                source_filename=item.filename,
                source_type=item.source_type,
                target_type=item.target_type,
                conversion_id=item.conversion_id,
                options=item.options,
                status=status_of(item, group),
                error_code=item.error_code,
                error_message=item.error_message,
                result=item.result,
                retry_count=item.retry_count,
                can_retry=group.retryable(item) and not group.cancel_requested,
                auto_retry_count=item.auto_retry_count,
                timeout_requested=item.timeout_requested,
                **item_progress(item),
            )
            for item in group.items
        ],
        result=result,
        error=error,
    )
