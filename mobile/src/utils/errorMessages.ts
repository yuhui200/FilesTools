/**
 * 错误码 → 用户看得懂的中文（规格 §十四）。
 *
 * 这张表与 Web 端 `frontend/src/utils/errorMessages.ts` **同一套键、同一套话术**
 * —— 同一个后端返回同一个码，两个客户端给出同一句解释，用户换个客户端
 * 不该看到两种说法。**改动文案时两边一起改。**
 *
 * 与 Web 的差异只有三处，都是「页面」这个词在 App 里不成立：
 * 「刷新页面」→「重新打开 App / 下拉重试」，「页面提示支持的格式」→「App 列出的格式」，
 * 以及 `config_failed` 的含义完全变了（见下）。
 *
 * 原则（照抄 Web）：**后端返回的 `message` 始终优先**，它更具体
 * （例如「文件过大，上限 50 MB」）。收不到 message 时用标题兜底。
 * **任何情况下都不会把堆栈、文件路径、内部模块名展示给用户。**
 */

import { CLIENT_CODES } from '@/services/api/client'

export interface ErrorExplanation {
  /** 一句话标题 */
  title: string
  /** 用户可以怎么做；没有合适建议时为 null */
  hint: string | null
}

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
    hint: '请换成工具里列出的格式再试。',
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
    hint: '请稍后重试；问题持续出现时可以重新打开 App。',
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

  // ---- PDF 转 Word 专属 ----
  PDF_EMPTY: {
    title: '这个 PDF 是空的',
    hint: '文件里没有任何页面，请换一份文件。',
  },
  PDF_NO_TEXT: {
    title: '没有可以提取的内容',
    hint: '这份 PDF 没有文字层，识别也没有得到任何文字，请确认文件内容后重试。',
  },
  /** 缺 OCR 组件重试治不了 —— 建议必须是「联系管理员」而不是「请重试」 */
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

  // ---- 统一转换中心专属 ----
  UNSUPPORTED_CONVERSION: {
    title: '这个转换做不到',
    hint: '请确认文件的真实格式，并从工具里列出的目标格式重新选择。',
  },
  TASK_NOT_RETRYABLE: {
    title: '这个文件不能重试',
    hint: '只有服务器临时故障导致的失败才能重试一次，请重新上传文件。',
  },

  // ---- 任务队列与 Worker 专属 ----
  TASK_TIMEOUT: {
    title: '服务器等待超时',
    hint: '这个文件处理得太久了，请减少一次处理的文件数量，或把大文件拆分后重试。',
  },
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

  // ---- Mobile 本地 ----
  [CLIENT_CODES.network]: {
    title: '无法连接服务器',
    hint: '请检查网络连接后重试。如果换了网络仍连不上，可能是服务器地址不对。',
  },
  [CLIENT_CODES.timeout]: {
    title: '请求超时',
    hint: '请稍后重试，或换一个更小的文件。',
  },
  [CLIENT_CODES.response]: {
    title: '服务器返回了无法解析的数据',
    hint: '请稍后重试。',
  },
  /**
   * Mobile 与 Web 在这里**含义不同**：Web 是「读限制配置失败，用默认值继续」，
   * App 是「压根没配服务器地址」，功能整个不可用，所以话术要指到配置上。
   */
  [CLIENT_CODES.config]: {
    title: '没有配置服务器地址',
    hint: '请在 mobile/.env 里设置 EXPO_PUBLIC_API_BASE_URL，然后重新启动或重新构建 App。',
  },
  [CLIENT_CODES.file]: {
    title: '文件处理失败',
    hint: '无法读写手机上的文件，请检查存储空间与权限后重试。',
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

/** 全部错误码，用于自检有没有漏配文案（服务端 `/api/config` 会给一份权威清单） */
export const KNOWN_ERROR_CODES: readonly string[] = Object.freeze(Object.keys(EXPLANATIONS))

/** 未收录的码 —— 启动自检与验收脚本读它 */
export function missingErrorCodes(serverCodes: readonly string[]): string[] {
  const known = new Set(KNOWN_ERROR_CODES)
  return serverCodes.filter((code) => !known.has(code))
}
