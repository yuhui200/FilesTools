"""统一的业务异常与错误码（§13）。

路由层捕获这些异常并转换成结构一致的 JSON 错误响应，
避免把 Python 堆栈或内部路径暴露给前端。

错误码是**给机器看的**：前端 ``utils/errorMessages.ts`` 按它决定标题和
"接下来该怎么办"，后端给出的 ``message`` 则是给用户看的具体原因。
两者配合，用户看到的就不再是干巴巴的「处理失败」。

因此新增异常时的规则是：

1. 能归入下面某个通用错误码的就直接复用，不要为了「更精确」造新码；
2. 只有当用户**该做的事不一样**时才新增错误码
   （例如 PDF 加密和 PDF 页数超限，一个只能作罢、一个可以拆分后重试）；
3. ``status_code`` 沿用 HTTP 语义，改错误码时不要顺手改状态码。

**第六阶段的例外**：PDF → Word 那六个码（``PDF_EMPTY`` / ``PDF_NO_TEXT`` /
``OCR_UNAVAILABLE`` / ``OCR_FAILED`` / ``DOCX_GENERATION_FAILED`` /
``PDF_CONVERSION_TIMEOUT``）是用户规格里点名要求的，所以虽然其中几个按规则 1
本可以并进 ``PROCESSING_FAILED``，这里仍然单独成码。它们各自继承最接近的
已有异常类，``isinstance`` 判断与状态码都不受影响。
"""

from __future__ import annotations


class ErrorCode:
    """全部错误码。前端按这里的取值给出中文标题与建议。"""

    # ---- 通用 ----
    INVALID_REQUEST = "INVALID_REQUEST"        # 400 参数或用法不合法
    FILE_TOO_LARGE = "FILE_TOO_LARGE"          # 413 文件超过大小上限
    INVALID_FILE_TYPE = "INVALID_FILE_TYPE"    # 415 文件类型不在允许范围内
    CORRUPTED_FILE = "CORRUPTED_FILE"          # 422 文件能收下，但内容损坏 / 无法解析
    PROCESSING_FAILED = "PROCESSING_FAILED"    # 422 处理过程本身失败
    PROCESSING_TIMEOUT = "PROCESSING_TIMEOUT"  # 504 处理超时
    SERVER_ERROR = "SERVER_ERROR"              # 500 非预期错误
    JOB_NOT_FOUND = "JOB_NOT_FOUND"            # 404 下载令牌无效或已过期
    TASK_NOT_FOUND = "TASK_NOT_FOUND"          # 404 任务不存在或已过期

    # ---- PDF 专属：用户该做的事和通用错误不一样，所以单独成码 ----
    PDF_ENCRYPTED = "PDF_ENCRYPTED"            # 需要密码，用户只能换文件
    PDF_TOO_MANY_PAGES = "PDF_TOO_MANY_PAGES"  # 超出单次页数 / 份数上限，可以拆分后重试
    PDF_PAGE_NOT_FOUND = "PDF_PAGE_NOT_FOUND"  # 页码不存在，改页码即可

    # ---- 文档转换专属（第五阶段）----
    # 服务器少了 Office 转换组件。用户该做的事是找管理员，不是重新上传文件，
    # 所以不能复用 PROCESSING_FAILED —— 那会让用户一直重传一份没问题的文档。
    CONVERTER_UNAVAILABLE = "CONVERTER_UNAVAILABLE"  # 503 服务器缺少转换组件

    # ---- PDF → Word 专属（第六阶段 A）----
    PDF_EMPTY = "PDF_EMPTY"                            # 400 这份 PDF 一页都没有
    PDF_NO_TEXT = "PDF_NO_TEXT"                        # 422 抽不出文字，OCR 也没认出来
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"                # 503 需要 OCR，但服务器没装
    OCR_FAILED = "OCR_FAILED"                          # 422 OCR 组件在，但这次识别失败了
    DOCX_GENERATION_FAILED = "DOCX_GENERATION_FAILED"  # 422 文字拿到了，但 Word 文件写不出来
    PDF_CONVERSION_TIMEOUT = "PDF_CONVERSION_TIMEOUT"  # 504 超过本功能的处理时限

    # ---- 统一转换中心专属（第七阶段）----
    # 这两个码都满足规则 2：用户该做的事与既有码不同。
    # 「这组格式不支持」要换目标格式（不是重传文件），
    # 「这项不能重试」要放弃这一项（不是修文件）。
    UNSUPPORTED_CONVERSION = "UNSUPPORTED_CONVERSION"  # 400 组合不在能力矩阵里
    TASK_NOT_RETRYABLE = "TASK_NOT_RETRYABLE"          # 409 只有失败项能重试，且源文件得还在

    # ---- 任务调度专属（第八阶段）----
    # 四个码都满足规则 2（用户该做的事不一样），不是「更精确」的重复：
    # 超时要稍后重试、被中断要重新提交、磁盘忙要稍后重试、线程池排满要稍后重试。
    TASK_TIMEOUT = "TASK_TIMEOUT"                # 504 整个任务超过了自己的处理时限
    WORKER_LOST = "WORKER_LOST"                  # 500 执行这个任务的 worker 不在了
    TEMPORARY_IO_ERROR = "TEMPORARY_IO_ERROR"    # 503 读写临时文件时出错（磁盘满 / 文件被占用）
    SERVER_BUSY = "SERVER_BUSY"                  # 503 线程池排满了，任务还没真正开工


class FileToolsError(Exception):
    """所有业务异常的基类。"""

    status_code: int = 400
    code: str = ErrorCode.INVALID_REQUEST

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class ValidationError(FileToolsError):
    """上传的文件或参数不合法。"""

    status_code = 400
    code = ErrorCode.INVALID_REQUEST


class FileTooLargeError(FileToolsError):
    """上传文件超过大小限制。"""

    status_code = 413
    code = ErrorCode.FILE_TOO_LARGE


class UnsupportedTypeError(FileToolsError):
    """文件类型不在允许列表内。"""

    status_code = 415
    code = ErrorCode.INVALID_FILE_TYPE


class CorruptedFileError(ValidationError):
    """文件收下了，但内容损坏或无法解析。

    与 :class:`UnsupportedTypeError` 的区别：类型是对的（确实是一张图 / 一份 PDF），
    只是文件本身坏了。用户该做的事也不同 —— 类型不对要换格式，内容坏了要重新拿一份文件。
    """

    code = ErrorCode.CORRUPTED_FILE


class ProcessingError(FileToolsError):
    """处理过程中出现可预期的失败（如图片损坏）。"""

    status_code = 422
    code = ErrorCode.PROCESSING_FAILED


class ProcessingTimeoutError(FileToolsError):
    """处理超时。"""

    status_code = 504
    code = ErrorCode.PROCESSING_TIMEOUT


class JobNotFoundError(FileToolsError):
    """下载令牌不存在或已过期。"""

    status_code = 404
    code = ErrorCode.JOB_NOT_FOUND


class TaskNotFoundError(FileToolsError):
    """任务不存在或已过期（第四阶段的任务队列）。"""

    status_code = 404
    code = ErrorCode.TASK_NOT_FOUND


class ConverterUnavailableError(FileToolsError):
    """服务器缺少文档转换组件（LibreOffice 没装或没找到）。

    归入这一类而不是「处理失败」，是因为用户的下一步动作完全不同：
    文件本身没问题，重传多少次都一样，得有人去服务器上装组件。
    """

    status_code = 503
    code = ErrorCode.CONVERTER_UNAVAILABLE


# ----------------------------------------------------------------------
# PDF 专属错误（都继承 ValidationError，状态码与第三阶段保持一致）
# ----------------------------------------------------------------------


class PdfEncryptedError(ValidationError):
    """PDF 有密码，当前无法处理。"""

    code = ErrorCode.PDF_ENCRYPTED


class PdfTooManyPagesError(ValidationError):
    """超出单次可处理的页数 / 份数上限。"""

    code = ErrorCode.PDF_TOO_MANY_PAGES


class PdfPageNotFoundError(ValidationError):
    """指定的页码在当前文档中不存在。"""

    code = ErrorCode.PDF_PAGE_NOT_FOUND


# ----------------------------------------------------------------------
# PDF → Word（第六阶段 A）
#
# 六个码都继承最接近的已有异常类，所以 ``except CorruptedFileError`` 这类
# 既有判断照样接得住它们，状态码也不用重新想一遍。
# ----------------------------------------------------------------------


class PdfEmptyError(CorruptedFileError):
    """这份 PDF 一页都没有。

    继承 :class:`CorruptedFileError`：对用户来说要做的都是「换一份文件」，
    状态码（400）也保持一致。单独成码只是为了前端能说清是「空文件」
    而不是「文件坏了」。
    """

    code = ErrorCode.PDF_EMPTY


class PdfNoTextError(ProcessingError):
    """文字层抽不出内容，OCR 也没能认出任何文字。

    注意与 :class:`OcrUnavailableError` 的分工：
    那个是「本来能认，但服务器没装 OCR」，这个是「认过了，确实什么都没有」。
    """

    code = ErrorCode.PDF_NO_TEXT


class OcrUnavailableError(ConverterUnavailableError):
    """需要 OCR，但服务器上没有装 OCR 组件。

    继承 :class:`ConverterUnavailableError`：用户的下一步和缺 LibreOffice 时
    一模一样 —— 文件本身没毛病，重传多少次都没用，得有人去装组件。
    ``503`` 而不是 ``500``：这是「服务暂时不可用」，不是「服务器出错了」。
    """

    code = ErrorCode.OCR_UNAVAILABLE


class OcrFailedError(ProcessingError):
    """OCR 组件在，但这一次识别失败了。"""

    code = ErrorCode.OCR_FAILED


class DocxGenerationError(ProcessingError):
    """文字已经拿到，但 Word 文件没能写出来。

    宁可在这里失败，也不把一份打不开的 DOCX 交给用户。
    """

    code = ErrorCode.DOCX_GENERATION_FAILED


class PdfConversionTimeoutError(ProcessingTimeoutError):
    """PDF → Word 超过了自己的处理时限。

    继承 :class:`ProcessingTimeoutError`（504），但单独成码是因为
    "接下来该怎么办" 不一样：这里用户该做的是减少页数，而不是降低清晰度。
    """

    code = ErrorCode.PDF_CONVERSION_TIMEOUT


# ----------------------------------------------------------------------
# 统一转换中心（第七阶段）
#
# 两个码都不是「文件坏了」，所以都不继承 CorruptedFileError：
# 一个是用户在界面上选错了目标格式，一个是这一项已经没有必要再试。
# ----------------------------------------------------------------------


class UnsupportedConversionError(ValidationError):
    """这一对「源格式 → 目标格式」不在能力矩阵里。

    与 :class:`UnsupportedTypeError` 的分工必须分清楚：

    * ``INVALID_FILE_TYPE``（415）—— 这个**文件**我们不收（换文件）；
    * ``UNSUPPORTED_CONVERSION``（400）—— 文件收下了，但**这个组合**转不了
      （换个目标格式，文件不用动）。

    混成一个码，前端就只能说一句笼统的「文件不支持」，用户会一直重传
    一份根本没有任何问题的 PDF。
    """

    status_code = 400
    code = ErrorCode.UNSUPPORTED_CONVERSION


class TaskNotRetryableError(FileToolsError):
    """这一项现在不能重试。

    两种情况：它根本不是失败项（还在排队 / 已经成功），
    或者它要用到的源文件已经随批次清理掉了。

    ``409`` 而不是 ``400``：请求本身没写错，是**当前状态**不允许这个操作 ——
    同一项在失败之后、清理之前重试是完全可以的。
    """

    status_code = 409
    code = ErrorCode.TASK_NOT_RETRYABLE


# ----------------------------------------------------------------------
# 任务调度（第八阶段）
#
# 四个码描述的都是「服务器这一侧的临时状况」，而不是用户文件的问题。
# 除了 TASK_TIMEOUT，其它三个在用户眼里都是「等一会儿再来」，
# 前端文案必须说清这一点 —— 否则用户会去重传一份根本没问题的文件。
# ----------------------------------------------------------------------


class TaskTimeoutError(ProcessingTimeoutError):
    """整个任务超过了自己的处理时限（§十七）。

    继承 :class:`ProcessingTimeoutError`（504）。与既有的
    ``PDF_CONVERSION_TIMEOUT`` / ``PROCESSING_TIMEOUT`` 的分工：
    那两个是**操作自己**的限额先到，这个是**池的兜底闸门**先到 ——
    正常配置下它几乎不会触发（生效超时永远不小于池内操作自己的限额）。
    """
    code = ErrorCode.TASK_TIMEOUT


class WorkerLostError(FileToolsError):
    """执行这个任务的 worker 没了，或者卡死到超过了任何合理的时限（§十九）。

    ``500``：这是服务器自己的问题，不是文件的问题。能自动重试就自动重试
    （见 :data:`RETRYABLE_CODES`），重试用尽后用户该做的是重新提交。
    """
    status_code = 500
    code = ErrorCode.WORKER_LOST


class TemporaryIoError(FileToolsError):
    """读写临时文件时出错：磁盘满、Windows 上文件被别的进程占用等。

    单独成码是因为它**值得再试一次**，而 ``PROCESSING_FAILED`` 不是 ——
    用户看到「处理失败，请重试」和看到「服务器磁盘繁忙，请稍后重试」，
    该做的事确实不一样。
    """
    status_code = 503
    code = ErrorCode.TEMPORARY_IO_ERROR


class ServerBusyError(FileToolsError):
    """线程池排满了，这个任务还没真正开工就已经等到了超时。

    与「处理超时」必须分开：处理超时可以建议用户换个小文件，
    而这个情况下文件大小根本不是原因 —— 重试或稍后再来才有用。
    谎称超时会让用户白白去压缩一张本来没问题的图片。
    """
    status_code = 503
    code = ErrorCode.SERVER_BUSY


# ----------------------------------------------------------------------
# 错误码 → HTTP 状态码
# ----------------------------------------------------------------------

_ERROR_CLASSES: tuple[type[FileToolsError], ...] = (
    ValidationError,
    FileTooLargeError,
    UnsupportedTypeError,
    CorruptedFileError,
    ProcessingError,
    ProcessingTimeoutError,
    JobNotFoundError,
    TaskNotFoundError,
    PdfEncryptedError,
    PdfTooManyPagesError,
    PdfPageNotFoundError,
    ConverterUnavailableError,
    PdfEmptyError,
    PdfNoTextError,
    OcrUnavailableError,
    OcrFailedError,
    DocxGenerationError,
    PdfConversionTimeoutError,
    UnsupportedConversionError,
    TaskNotRetryableError,
    TaskTimeoutError,
    WorkerLostError,
    TemporaryIoError,
    ServerBusyError,
)

#: 每个错误码「如果同步抛出」会对应的状态码。
#: 批量任务是异步的：失败发生在轮询阶段（HTTP 200 + 任务状态为 failed），
#: 前端和测试仍然需要知道这个错误的性质，因此映射集中在这里生成，
#: 新增异常类会自动出现在表里，不必两处维护。
STATUS_BY_CODE: dict[str, int] = {
    cls.code: cls.status_code for cls in _ERROR_CLASSES
}

#: 兜底的 500 没有对应的异常类（由 main.py 的 catch-all 处理器直接构造响应），
#: 但它确实会返回给客户端。``/api/config`` 的 ``error_codes`` 承诺的是
#: 「服务端可能返回的全部错误码」，而前端拿它自检有没有漏配文案 ——
#: 少列一个，漏配就查不出来。所以这里显式补上。
STATUS_BY_CODE[ErrorCode.SERVER_ERROR] = 500


def status_for_code(code: str) -> int:
    """错误码对应的状态码；未知错误码按 500 处理。"""
    return STATUS_BY_CODE.get(code, 500)


# ----------------------------------------------------------------------
# 哪些失败值得自动重试（第八阶段 §二十一）
# ----------------------------------------------------------------------

#: 可以**自动**重试的错误码 —— 只有「服务器侧的偶发问题」。
#:
#: 这是一份**白名单**，不在里面的都不自动重试。用白名单而不是黑名单，
#: 是因为黑名单每加一个新错误码都要有人记得回来补一笔，漏掉的那次
#: 重试可能就是白白多跑一遍 LibreOffice。
#:
#: 明确**不在**表里的几类，以及为什么：
#:
#: * ``UNSUPPORTED_CONVERSION`` / ``INVALID_FILE_TYPE`` / ``PDF_EMPTY`` /
#:   ``PDF_NO_TEXT`` / ``DOCX_GENERATION_FAILED`` —— 文件本身的问题，
#:   再跑一百次结果一样，重试只是浪费用户的时间；
#: * ``OCR_UNAVAILABLE`` / ``CONVERTER_UNAVAILABLE`` —— 服务器缺组件或
#:   组件被占用。缺组件重试无用；被占用时用户手上还有**手动重试**这一个
#:   明确的动作（第七阶段的接口），自动重试只会在锁上再排一次队。
#: * ``SERVER_BUSY`` —— 线程池排满时立刻重试，只会重新把池排满一次。
RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        ErrorCode.WORKER_LOST,
        ErrorCode.TEMPORARY_IO_ERROR,
        ErrorCode.TASK_TIMEOUT,
    }
)


def is_retryable_code(code: str | None) -> bool:
    """这个错误码属不属于「可自动重试」那一类。

    注意它只回答**类别**问题；「这一次要不要真的重试」还要看次数上限，
    以及超时那条策略开关（``settings.TASK_TIMEOUT_AUTO_RETRY`` 默认关）。
    """
    return code in RETRYABLE_CODES
