/**
 * 后端返回结构的类型（规格 §十三）。
 *
 * 这些**不是**重新设计的形状 —— 逐字对应 Web 端
 * `frontend/src/types/index.ts` 里同名的那几个接口，字段名与注释都照抄。
 * 后端是唯一的数据真相，两个客户端各自持有同一份描述。
 *
 * 为什么不直接把 Web 那份 import 进来：Metro 默认只在项目根内解析，
 * 要让 mobile 读 `../frontend/src/types` 得改 watchFolders 与
 * resolver.extraNodeModules —— 为一个纯类型文件去动打包器配置，
 * 换来的脆弱性比复制大得多。代价是两份可能漂移，
 * 所以 `scripts/verify_mobile_phase11a.py` 里有一条**机械对账**：
 * 两边的接口名集合、以及每个接口的字段名集合必须完全一致，
 * 少一个字段就红。改后端响应时忘了同步 mobile，会在那一步被抓住。
 */

/** 后端统一的错误体：`{"error": {"code", "message"}}`（main.py 的异常处理器） */
export interface ApiErrorBody {
  error: {
    code: string
    message: string
  }
}

/** 单个文件的状态 */
export type ConversionStatus =
  | 'queued'
  | 'processing'
  | 'cancelling'
  | 'completed'
  | 'failed'
  | 'cancelled'

/** 整批状态。没有 cancelling —— 「正在取消」是一个标志，不是整批的状态 */
export type ConversionBatchState =
  | 'queued'
  | 'processing'
  | 'completed'
  | 'failed'
  | 'cancelled'

/** 可选的目标格式 */
export interface ConversionTargetOption {
  value: string
  label: string
  /** 结果文件扩展名，例如 .pdf */
  extension: string
}

/** 一种源格式，以及它真的能转成什么 */
export interface ConversionSourceOption {
  value: string
  label: string
  /** 归入这一类的扩展名，例如 ['.jpg', '.jpeg'] */
  extensions: string[]
  targets: string[]
}

/** 界面上的一组：图片 / Office 文档 / 文本 / PDF */
export interface ConversionGroupOption {
  key: string
  label: string
  sources: ConversionSourceOption[]
  targets: string[]
}

/** 能力类别（图片 / 文档 / PDF） */
export interface ConversionCategoryOption {
  value: string
  label: string
}

/** 一种格式。`is_source` / `is_target` 决定它能出现在哪一侧 */
export interface ConversionFormatOption {
  value: string
  label: string
  category: string
  /** 归入这一类的扩展名，例如 ['.jpg', '.jpeg'] */
  extensions: string[]
  media_type: string
  is_source: boolean
  is_target: boolean
}

/** `options_schema` 里一个枚举取值 */
export interface ConversionOptionEnumValue {
  value: string
  label: string
}

/**
 * `options_schema` 里的一项。
 *
 * 键名是**扁平点号串**（`resize.width`），与提交时的 JSON 键逐字相同 ——
 * 界面渲染与提交序列化读的是同一份数据，没有第二处硬编码的键名。
 */
export interface ConversionOptionSpec {
  key: string
  type: 'integer' | 'number' | 'boolean' | 'enum' | 'string'
  label: string
  default?: unknown
  /** 枚举默认值对应的中文名，给折叠摘要用 */
  default_label?: string
  required: boolean
  min?: number
  max?: number
  step?: number
  unit?: string
  help?: string
  enum?: ConversionOptionEnumValue[]
  /** 快捷档（质量的那七档、目标大小的那几档）。值一律是字符串 */
  presets?: ConversionOptionEnumValue[]
  /** 依赖项：全部成立时这一项才显示 */
  visible_when?: Record<string, unknown>
  /** 非空表示枚举取值要在运行期填（目前只有 `"fonts"`） */
  dynamic?: string
}

/** 一条能力的参数描述 */
export interface ConversionOptionsSchema {
  version: number
  items: ConversionOptionSpec[]
}

/**
 * 一条能力（§十四）。
 *
 * `conversion` 与 `operation` 共用这个形状：前者走统一队列，
 * 后者带 `endpoint`、由各自的老接口执行。Mobile 只消费
 * `operation_type === 'conversion'` 的条目 —— 那些多进多出的工具
 * （合并 / 拆分 / 压缩）与 1→1 的批量模型结构上不同，
 * 本阶段不做它们的界面。
 */
export interface ConversionCapability {
  id: string
  source_type: string
  target_type: string
  operation_type: 'conversion' | 'operation'
  display_name: string
  category: string
  group: string
  requires: string
  worker_pool: string
  /** 这台服务器现在能不能用。不可用的条目**仍然在列表里**，只是标注出来 */
  available: boolean
  supports_batch: boolean
  supports_options: boolean
  supports_preview: boolean
  /** 操作类条目才有：执行它的老接口 */
  endpoint: string | null
  method: string | null
  /** 标签，目前只有 `recommended` */
  tags: string[]
  /** 需要说给用户听的一句话（如「多帧文件只取第一帧」） */
  note: string | null
  options_schema: ConversionOptionsSchema | null
  /** **只有操作类条目非空**：这份操作接受哪些源类型的文件 */
  accepts: string[]
  /** **只有操作类条目非空**：文件怎么交给 `endpoint` */
  input_field: 'files' | 'input_id' | null
}

/**
 * GET /api/conversion/capabilities 的响应。
 *
 * 服务器**当前真的能做**的转换：没装 LibreOffice，矩阵里就没有 Office 那几类。
 * Mobile 的整个工具列表都由它派生（规格 §八）—— 本地**不允许**再维护一份
 * 能力矩阵，否则后端加一种格式，App 要跟着发版。
 */
export interface ConversionCapabilities {
  matrix: Record<string, string[]>
  groups: ConversionGroupOption[]
  targets: ConversionTargetOption[]
  office_available: boolean
  pdf_to_word_available: boolean
  ocr_available: boolean
  /** 能力缺失的原因说明；不缺就是空数组 */
  notes: string[]
  /** PDF 转 Word 的文字识别提示 */
  pdf_to_word_note: string
  /** 能力类别：image / document / pdf */
  categories: ConversionCategoryOption[]
  /** 系统认识的每一种格式，含扩展名、MIME、能不能当源 / 目标 */
  formats: ConversionFormatOption[]
  /** 一条条 1→1 的转换能力 */
  conversions: ConversionCapability[]
  /** 多进多出 / 页面级的工具（合并、拆分…），本阶段不消费 */
  operations: ConversionCapability[]
}

/** 一个文件提交后的任务号（`{batch_id}:{index}`） */
export interface ConversionTaskRef {
  task_id: string
  index: number
  filename: string
  status: string
}

/** POST /api/conversion/tasks 的响应（202） */
export interface ConversionBatchCreated {
  batch_id: string
  total: number
  tasks: ConversionTaskRef[]
  status_url: string
  progress_url: string
  cancel_url: string
}

/** 单项结果摘要。用不上的字段为 null */
export interface ConversionItemResult {
  filename: string
  size: number
  /** 与 `size` 同一个数（服务端一次赋值给两个键） */
  output_size: number
  media_type: string
  /** **一次性**下载地址，下载过后就失效了 */
  download_url: string
  /** 内联预览地址；这个格式没法预览时为 null。预览不消耗下载令牌 */
  preview_url: string | null
  page_count: number | null
  width: number | null
  height: number | null
  original_size: number | null
  target_size: number | null
  quality_used: number | null
  /** **null 表示「没有目标可谈」，不是「没达到」** */
  target_reached: boolean | null
  notes: string[]
}

/** 批次里的单个文件（§八） */
export interface ConversionTaskStatus {
  task_id: string
  index: number
  source_filename: string
  /** 服务端按**真实内容**判定的源格式，可能与客户端的分组不同 */
  source_type: string
  target_type: string
  status: ConversionStatus
  /** **真实**百分比；无从得知时为 null。为 null 时走不确定态动画 */
  progress: number | null
  /** 真实阶段，例如 ocr / writing */
  stage: string | null
  page: number | null
  page_count: number | null
  error_code: string | null
  error_message: string | null
  result: ConversionItemResult | null
  retry_count: number
  /** 现在能不能重试（服务端说了算：失败、源文件还在、还没重试过） */
  can_retry: boolean
  /** 服务端**自动**重新排队过几次，与用户点的 `retry_count` 是两件事 */
  auto_retry_count: number
  /** 被判过超时。**不代表活已经停了**，文案上不能说「已停止」 */
  timeout_requested: boolean
  /** 服务端命中的那条能力 ID，例如 `image.jpg-to-png`。空串表示没能判定 */
  conversion_id: string
  /** 服务端**收敛后**的参数快照，界面回显用的是它，不是提交时的草稿 */
  options: Record<string, unknown> | null
}

/** 整批结果摘要 */
export interface ConversionBatchSummary {
  total: number
  completed: number
  failed: number
  cancelled: number
  archived: boolean
  archive_filename: string | null
  items: ConversionItemResult[]
  notes: string[]
  /** 整批下载入口：1 个成功项就是那个文件，≥2 个才是 ZIP；失效后为 null */
  download_url: string | null
  /** 结果已经被下载过或已过期（服务器上已删除） */
  expired: boolean
}

/** GET /api/conversion/tasks/{batch_id} 的响应（§八） */
export interface ConversionBatchStatus {
  batch_id: string
  status: ConversionBatchState
  /** 用户请求过取消。**不代表活已经停了** */
  cancelling: boolean
  /** 已定下来的文件数 / 总数，0-100 */
  progress: number
  total: number
  queued: number
  processing: number
  completed: number
  failed: number
  cancelled: number
  tasks: ConversionTaskStatus[]
  result: ConversionBatchSummary | null
  error: { code: string; message: string } | null
}

/** GET /api/conversion/tasks/{batch_id}/progress 的响应（轻量，轮询用） */
export interface ConversionProgress {
  batch_id: string
  status: ConversionBatchState
  cancelling: boolean
  progress: number
  total: number
  queued: number
  processing: number
  completed: number
  failed: number
  cancelled: number
}

/** GET /api/health 的响应 */
export interface HealthResponse {
  status: string
  version: string
}

/**
 * GET /api/config 里 Mobile 真正用到的那几项。
 *
 * 只列用得到的：那个响应有四十多个键，把整份抄过来等于维护一份
 * 用不上的表。字段名与后端逐字一致。
 */
export interface PublicConfig {
  max_upload_bytes: number
  max_batch_files: number
  max_batch_total_bytes: number
  /** 服务端可能返回的全部错误码，用来自检 Mobile 有没有漏配文案 */
  error_codes: string[]
  task_ttl_seconds: number
  file_ttl_seconds: number
}
