/**
 * 错误码 → 用户看得懂的中文提示（第四阶段 §13）。
 *
 * 后端只负责说清「发生了什么」（``error.message``），
 * 「这说明什么、接下来该做什么」在这里按错误码补全。
 * 批量任务是异步的：失败发生在轮询阶段，前端拿到的是一个错误码而不是 HTTP 状态码，
 * 所以文案必须由错误码驱动。
 *
 * 后端返回的 message 始终优先展示（它更具体，例如「文件过大，上限 50 MB」）；
 * 这里给出的是标题与建议。收不到 message 时用标题兜底，
 * **任何情况下都不会把 Python 堆栈之类的内容展示给用户**。
 */

/** 前端自己产生的错误码（网络、超时、下载失败等），与后端错误码放在一起维护 */
export const CLIENT_ERROR_CODES = {
  network: 'network_error',
  timeout: 'timeout',
  download: 'download_failed',
  response: 'bad_response',
  send: 'send_failed',
  config: 'config_failed',
  unknown: 'error',
} as const

export interface ErrorExplanation {
  /** 一句话标题 */
  title: string
  /** 用户可以怎么做；没有合适建议时为 null */
  hint: string | null
}

/**
 * 全部错误码的文案。
 *
 * 键与后端 ``utils/errors.py`` 的 ``ErrorCode`` 一一对应，
 * 服务端 /api/config 的 ``error_codes`` 会返回后端全部错误码，
 * 出现没配文案的新码时 :func:`explainError` 会退回服务端原文案而不是空白。
 */
const EXPLANATIONS: Record<string, ErrorExplanation> = {
  // ---- 通用 ----
  INVALID_REQUEST: {
    title: '请求参数不正确',
    hint: '请检查文件与设置后重试。',
  },
  FILE_TOO_LARGE: {
    title: '文件太大',
    hint: '请压缩后重传，或减少一次处理的文件数量。',
  },
  INVALID_FILE_TYPE: {
    title: '文件格式不支持',
    hint: '请换成页面提示支持的格式再试。',
  },
  CORRUPTED_FILE: {
    title: '文件已损坏',
    hint: '这个文件无法解析，请换一份完好的文件。',
  },
  PROCESSING_FAILED: {
    title: '处理失败',
    hint: '请重试；如果一直失败，换一份文件试试。',
  },
  PROCESSING_TIMEOUT: {
    title: '处理超时',
    hint: '文件较大或页数较多，请减少数量、降低清晰度后分批重试。',
  },
  SERVER_ERROR: {
    title: '服务器出错了',
    hint: '请稍后重试；问题持续出现时可以刷新页面。',
  },
  JOB_NOT_FOUND: {
    title: '文件已过期',
    hint: '结果文件只保留很短时间，请重新处理一次。',
  },
  TASK_NOT_FOUND: {
    title: '任务已过期',
    hint: '任务记录只保留 30 分钟，请重新提交。',
  },

  // ---- 文档转换专属 ----
  CONVERTER_UNAVAILABLE: {
    title: '服务器缺少转换组件',
    hint: '这是服务器端缺少 Office 转换组件导致的，重新上传同样会失败，请联系管理员。',
  },

  // ---- PDF 专属 ----
  PDF_ENCRYPTED: {
    title: 'PDF 有密码',
    hint: '请先用其他工具去掉密码，再上传处理。',
  },
  PDF_TOO_MANY_PAGES: {
    title: '超出单次处理上限',
    hint: '请拆分文件或缩小页面范围后分批处理。',
  },
  PDF_PAGE_NOT_FOUND: {
    title: '页码不存在',
    hint: '请按文件的真实页数修改页码后重试。',
  },

  // ---- PDF 转 Word 专属（第六阶段 A）----
  PDF_EMPTY: {
    title: '这个 PDF 是空的',
    hint: '文件里没有任何页面，请换一份文件。',
  },
  PDF_NO_TEXT: {
    title: '没有可以提取的内容',
    hint: '这份 PDF 没有文字层，识别也没有得到任何文字，请确认文件内容后重试。',
  },
  /**
   * 缺 OCR 组件重试治不了 —— 建议必须是「联系管理员」而不是「请重试」。
   * 同时说清文字版 PDF 不受影响：扫描件用不了，不代表这个功能整个不能用。
   */
  OCR_UNAVAILABLE: {
    title: '服务器未安装 OCR 组件',
    hint: '当前服务器未安装 OCR 组件，暂时无法处理扫描 PDF。文字版 PDF 仍可正常转换；需要处理扫描件请联系管理员。',
  },
  OCR_FAILED: {
    title: '文字识别失败',
    hint: '请重试；如果一直失败，可能是扫描件太模糊，换一份更清晰的试试。',
  },
  DOCX_GENERATION_FAILED: {
    title: '生成 Word 文件失败',
    hint: '请重试；如果一直失败，换一份文件试试。',
  },
  PDF_CONVERSION_TIMEOUT: {
    title: '转换超时',
    hint: '页数较多或需要逐页识别时容易超时，请拆分文件后分批转换。',
  },

  // ---- 统一转换中心专属（第七阶段）----
  /**
   * 界面上本来就不该出现选不了的组合（目标格式来自 /api/conversion/capabilities），
   * 所以这条通常是「文件真实内容与扩展名不符」——说清这一点比笼统的「失败」有用。
   */
  UNSUPPORTED_CONVERSION: {
    title: '这个转换做不到',
    hint: '请确认文件的真实格式，并从界面列出的目标格式里重新选择。',
  },
  /**
   * 重试只对「服务器一时忙不过来」这类失败有意义，
   * 文件本身有问题、或已经重试过一次的，只能重新上传。
   */
  TASK_NOT_RETRYABLE: {
    title: '这个文件不能重试',
    hint: '只有服务器临时故障导致的失败才能重试一次，请重新上传文件。',
  },

  // ---- 任务队列与 Worker 专属（第八阶段）----
  /**
   * 「服务器等不下去了」和「这个文件本身有问题」是两件事：前者重试有意义。
   * 所以这里的建议是「减少数量 / 拆分」，而不是「换个文件」。
   */
  TASK_TIMEOUT: {
    title: '服务器等待超时',
    hint: '这个文件处理得太久了，请减少一次处理的文件数量，或把大文件拆分后重试。',
  },
  /**
   * 自动重试已经替用户重排过一次队，所以文案要说清「可能还会再试」，
   * 而不是直接让人重新上传 —— 那会让用户以为任务已经彻底结束了。
   */
  WORKER_LOST: {
    title: '任务被中断',
    hint: '服务器正在重新排队处理这个文件；如果稍后仍然失败，请重新上传。',
  },
  TEMPORARY_IO_ERROR: {
    title: '服务器临时出错',
    hint: '服务器磁盘或临时目录一时繁忙，稍后重试通常就能成功。',
  },
  SERVER_BUSY: {
    title: '服务器正忙',
    hint: '同时处理的任务太多了，请稍等片刻再重试。',
  },

  // ---- 前端本地 ----
  [CLIENT_ERROR_CODES.network]: {
    title: '无法连接服务器',
    hint: '请确认网络正常，然后重试。',
  },
  [CLIENT_ERROR_CODES.timeout]: {
    title: '请求超时',
    hint: '请稍后重试，或换一个更小的文件。',
  },
  [CLIENT_ERROR_CODES.download]: {
    title: '下载失败',
    hint: '下载链接只能使用一次，请重新处理后再下载。',
  },
  [CLIENT_ERROR_CODES.response]: {
    title: '服务器返回了无法解析的数据',
    hint: '请刷新页面后重试。',
  },
  [CLIENT_ERROR_CODES.send]: {
    title: '发送请求失败',
    hint: '请检查网络后重试。',
  },
  [CLIENT_ERROR_CODES.config]: {
    title: '读取服务配置失败',
    hint: '页面会使用默认限制，仍可正常处理文件。',
  },
}

const UNKNOWN: ErrorExplanation = {
  title: '处理失败',
  hint: '请重试。',
}

/** 取错误码对应的中文说明；未收录的错误码返回通用文案。 */
export function explainError(
  code: string | null | undefined,
  message?: string | null,
): ErrorExplanation {
  const known = code ? EXPLANATIONS[code] : undefined
  const fallback = known ?? UNKNOWN
  const detail = message?.trim()
  if (!detail) return fallback
  // 服务端的 message 更具体，优先展示；但只有它和标题不同才替换，避免重复一句话
  return detail === fallback.title ? fallback : { title: detail, hint: fallback.hint }
}
