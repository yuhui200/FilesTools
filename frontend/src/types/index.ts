/** 与后端接口对应的类型定义。 */

/** 压缩质量档位 */
export type QualityPreset = 'high' | 'balanced' | 'strong'

/** 目标大小选项（字节）。null 表示不限制大小。 */
export type TargetOption = 'none' | '500kb' | '1mb' | '2mb' | '5mb' | 'custom'

/** 支持的输出格式 */
export type OutputFormat = 'jpg' | 'png' | 'webp'

export interface FileInfo {
  filename: string
  /** 字节数 */
  size: number
  width: number | null
  height: number | null
  format: string | null
}

/** 批量处理中单个文件的结果 */
export interface ItemResult {
  /** 对应上传顺序，从 0 开始 */
  index: number
  original: FileInfo
  result: FileInfo
  /**
   * 结果图片的预览地址；**浏览器渲染不了的格式（TIFF / HEIC / ICO）为 null**。
   *
   * 第十阶段 C §五 之前这里恒为字符串，于是那几种格式的结果也带着一个地址
   * —— 而它发回来的 MIME 浏览器渲染不了，界面上就是一张破图。现在服务端与
   * 统一转换中心用**同一张**「可内联预览」表，给不出就说 null，界面渲染
   * 占位符而不是碎图标（`ImageCompare` 本来就支持 null）。
   */
  preview_url: string | null
  saved_bytes: number
  saved_percent: number
  target_met: boolean
  quality_used: number | null
  scale: number
  untouched: boolean
  note: string | null
}

/** 批量处理中单个文件的失败原因 */
export interface FailureInfo {
  index: number
  filename: string
  code: string
  message: string
}

/** POST /api/image/convert 与 /api/image/resize 的响应 */
export interface BatchResponse {
  job_id: string
  /** 结果多于一个文件时指向 ZIP 包 */
  download_url: string
  items: ItemResult[]
  failures: FailureInfo[]
  original_total: number
  result_total: number
  saved_bytes: number
  saved_percent: number
  target_bytes: number | null
  /** 结果是否打包成了 ZIP */
  archived: boolean
  archive_filename: string | null
}

/** GET /api/config 的响应 */
export interface PublicConfig {
  max_upload_bytes: number
  /** 单批最多处理多少个文件 */
  max_batch_files: number
  /** 单批所有文件加起来的大小上限 */
  max_batch_total_bytes: number
  allowed_image_extensions: string[]
  quality_presets: QualityPreset[]
  /** 格式转换 / 尺寸调整的默认质量 */
  default_quality_value: number
  /** 单边最大像素，超过会被服务端等比缩小 */
  max_image_edge: number
  output_formats: string[]
  process_timeout_seconds: number

  // ---- PDF（第三阶段）----
  allowed_pdf_extensions: string[]
  /** 单个 PDF 的页数上限 */
  max_pdf_pages: number
  /** 页面选择器最多渲染多少张缩略图 */
  pdf_thumbnail_max_pages: number
  pdf_thumbnail_width: number
  pdf_render_max_dpi: number
  pdf_render_default_dpi: number
  /** 一次最多把多少页导出成图片 */
  pdf_export_max_pages: number
  /** 可选的标准页面尺寸（不含 auto 与 custom） */
  pdf_page_sizes: string[]
  pdf_margins: string[]
  pdf_compress_levels: string[]
  default_pdf_compress_level: string
  /** 上传的原始 PDF 在服务器上的保留时长（秒） */
  pdf_input_ttl_seconds: number

  // ---- 文档转换（第五阶段）----
  allowed_word_extensions: string[]
  allowed_excel_extensions: string[]
  allowed_powerpoint_extensions: string[]
  allowed_text_extensions: string[]
  /** 服务端转换一份文档的超时时间（秒） */
  doc_timeout_seconds: number
  /**
   * 服务器是否装了 Office 转换组件（LibreOffice）。
   * 为 false 时前端在**上传之前**就提示并禁用按钮，不让用户白传一份文件。
   */
  doc_conversion_available: boolean
  /** 「最大文件大小」可选档位，前端照这个渲染单选项 */
  doc_target_presets: string[]
  /**
   * TXT 排版可选的字体。
   *
   * 由服务端**运行时探测本机字体**得到，不是写死的列表 ——
   * 服务器上没装的字体不该出现在选项里（选了也只会得到别的字体）。
   * 探测不到任何系统字体时只有「内置字体」一项。
   */
  txt_fonts: TxtFontOption[]
  txt_font_size: { min: number; max: number; default: number }
  txt_page_sizes: string[]
  txt_orientations: string[]

  // ---- PDF 转 Word（第六阶段 A）----
  /**
   * 服务器是否能生成 DOCX（只取决于 python-docx，与 LibreOffice 无关）。
   * 为 false 时前端在上传之前就提示并禁用按钮。
   */
  pdf_to_word_available: boolean
  /**
   * 服务器是否装了 OCR 组件。
   *
   * 为 false 时**只有扫描版 PDF 用不了**：文字版 PDF 不经过 OCR，
   * 仍然可以正常转换，所以这个开关不能拿去禁用整个页面。
   */
  ocr_available: boolean
  /**
   * OCR 内置模型覆盖的字符集。**只读**，不是可选的语言包 ——
   * 引擎（rapidocr）没有切换语言的参数，这里返回的是它认得的字符范围。
   */
  ocr_languages: string[]
  /** 一次转换里最多 OCR 多少页，超出只保留原图 */
  pdf_to_word_max_ocr_pages: number
  /** 服务端转换一份 PDF 的超时时间（秒） */
  pdf_to_word_timeout_seconds: number
}

/** TXT 排版字体：value 是传给接口的值，label 是显示名 */
export interface TxtFontOption {
  value: string
  label: string
}

// ----------------------------------------------------------------------
// PDF 工具
// ----------------------------------------------------------------------

/** 页面大小：自动 / A4 / A5 / Letter / 自定义 */
export type PdfPageSize = 'auto' | 'a4' | 'a5' | 'letter' | 'custom'

/** 页面方向 */
export type PdfOrientation = 'auto' | 'portrait' | 'landscape'

/** 图片适应方式：保持比例 / 填充页面 */
export type PdfFit = 'contain' | 'fill'

/** 页边距 */
export type PdfMargin = 'none' | 'small' | 'medium' | 'large'

/** PDF 拆分方式 */
export type PdfSplitMode = 'every' | 'ranges' | 'selected'

/** PDF 压缩等级 */
export type PdfCompressLevel = 'light' | 'balanced' | 'strong'

/** PDF 压缩的目标最大文件大小 */
export type PdfTargetOption = 'none' | '5mb' | '10mb' | '20mb' | 'custom'

/** PDF 转图片的清晰度档位 */
export type PdfResolution = 'standard' | 'high' | 'ultra'

/** POST /api/pdf/upload 的响应 */
export interface PdfInputResponse {
  /** 后续操作的凭据 */
  input_id: string
  filename: string
  size: number
  page_count: number
  /** 缩略图地址前缀，取第 N 页（从 0 开始）时拼上 /N */
  thumbnail_base: string
}

/** PDF 结果中的一个文件 */
export interface PdfFileResponse {
  filename: string
  size: number
  page_count: number | null
  /** 图片类结果的预览地址，不消耗下载令牌 */
  preview_url: string | null
}

/** 所有 PDF 处理接口的统一响应 */
export interface PdfResultResponse {
  job_id: string
  /** 结果多于一个文件时指向 ZIP */
  download_url: string
  /** 下载时的文件名 */
  filename: string
  size: number
  media_type: string
  archived: boolean
  archive_filename: string | null
  /** 结果 PDF 的页数 */
  page_count: number | null
  original_size: number | null
  original_pages: number | null
  saved_bytes: number | null
  saved_percent: number | null
  files: PdfFileResponse[]
  /** 需要主动告诉用户的说明 */
  notes: string[]
}

// ----------------------------------------------------------------------
// 文档转换（第五阶段）
// ----------------------------------------------------------------------

/**
 * 支持转换的文档类别。
 *
 * 每加一种格式，这个联合类型就多一个成员 —— 页面配置、接口路径都是
 * `Record<DocKind, …>`，漏改任何一处都编译不过。
 */
export type DocKind = 'word' | 'excel' | 'ppt' | 'txt'

/** 文档转换的「最大文件大小」档位，与后端 DOC_TARGET_PRESETS 一致 */
export type DocTargetOption = 'none' | '500kb' | '1mb' | '2mb' | '5mb' | '10mb' | 'custom'

/** TXT 排版的页面方向 */
export type TxtOrientation = 'portrait' | 'landscape'

/** 后端统一错误响应体 */
export interface ApiErrorBody {
  error: {
    code: string
    message: string
  }
}

// ----------------------------------------------------------------------
// PDF 转 Word（第六阶段 A）
// ----------------------------------------------------------------------

/** 整份 PDF 的类型：全是文字页 / 全是扫描页 / 两者都有 */
export type DocumentKind = 'text' | 'scan' | 'mixed'

/** 单页的类型。同一份 PDF 里可以逐页不同 —— 封面是图、正文是字的很常见 */
export type PageKind = 'text' | 'scan'

/** 内容是怎么拿到的 */
export type ExtractionMethod = 'text' | 'ocr' | 'mixed'

/**
 * POST /api/office/pdf-to-word 的响应。
 *
 * 在 PDF 通用响应的基础上，多出「这份 PDF 是什么类型、内容是怎么取出来的」——
 * 结果页要把这几项如实摆出来：用户得知道哪些文字是 OCR 认的，可能认错。
 */
export interface DocumentResultResponse extends PdfResultResponse {
  document_kind: DocumentKind
  /** 类型的中文名，服务端给，前端不自己拼 */
  document_kind_label: string
  extraction_method: ExtractionMethod
  /** 识别方式的中文名 */
  extraction_label: string
  /** 每一页的类型，顺序与源 PDF 一致；文字版 PDF 全是 "text" */
  page_kinds: PageKind[]
  text_pages: number
  ocr_pages: number
  /** 这次用到的 OCR 字符集；没用 OCR 时是空数组 */
  ocr_languages: string[]
}

/**
 * GET /api/office/pdf-to-word/progress/{id} 的响应。
 *
 * 只有阶段名和 OCR 的页码 —— 服务端不存文件名和大小，所以猜到别人的 id
 * 也看不出这是谁的文件。``page`` 只在 OCR 阶段有值。
 */
export interface PdfToWordProgress {
  /** analyzing / detecting / extracting / ocr / writing / verifying */
  stage: string
  page: number | null
  page_count: number | null
}

// ----------------------------------------------------------------------
// 任务队列（第四阶段 §3 / §4 / §6）
// ----------------------------------------------------------------------

/** 任务与单个文件的状态 */
export type TaskState = 'waiting' | 'processing' | 'done' | 'failed'

/** 提交批量任务后的回执：批量接口立刻返回它，随后按 status_url 轮询 */
export interface TaskCreated {
  group_id: string
  /** 工具标识，例如 image.convert */
  tool: string
  /** 工具中文名，例如 图片压缩 */
  label: string
  /** 本批文件总数 */
  total: number
  status_url: string
}

/** 单个文件的处理结果摘要。字段随工具不同，这里只声明各工具共有的部分。 */
export interface TaskItemResult {
  filename?: string
  /** 处理后的文件信息（图片类工具） */
  result?: FileInfo
  /** 处理前的文件信息（图片类工具） */
  original?: FileInfo
  saved_bytes?: number
  saved_percent?: number
  /** PDF 转图片：这份 PDF 实际导出的页数 */
  pages?: number
  /** PDF 转图片：这份 PDF 的总页数 */
  total_pages?: number
  note?: string | null
}

/** 任务中的单个文件（§4） */
export interface TaskFile {
  /** 对应上传顺序，从 0 开始 */
  index: number
  filename: string
  size: number
  state: TaskState
  /** 状态的中文名：等待中 / 处理中 / 已完成 / 失败 */
  state_label: string
  error_code: string | null
  error_message: string | null
  /** 处理完成后才有 */
  result: TaskItemResult | null
}

/**
 * 任务状态快照（轮询 GET /api/tasks/{group_id} 的响应）。
 *
 * 泛型参数是「整批结果」的类型：图片类工具是 BatchResponse，
 * PDF 转图片是 PdfResultResponse，两者的下载与展示逻辑各自独立。
 */
export interface TaskSnapshot<R = BatchResponse | PdfResultResponse> {
  group_id: string
  tool: string
  label: string
  /** 整批状态 */
  state: TaskState
  /** 总任务数 */
  total: number
  completed: number
  processing: number
  waiting: number
  failed: number
  /** 已完成 + 失败 */
  finished: number
  /** 0-100 */
  percent: number
  tasks: TaskFile[]
  /** 整批结果；下载过的结果 download_url 会变成 null 并标记 expired */
  result: (R & { expired?: boolean }) | null
  /** 整批失败时的原因 */
  error: { code: string; message: string } | null
}

// ----------------------------------------------------------------------
// 统一转换中心（第七阶段）
//
// 与上面第四阶段的批量任务**不是同一套词表**：状态叫 status 不叫 state，
// 取值是 queued / completed / cancelled，还多一个 cancelling。
// 规格 §八 是这么定的，队列那一侧则沿用第四阶段的词，翻译只在后端做一次，
// 所以前端这里是独立的一套类型，不复用 TaskSnapshot。
// ----------------------------------------------------------------------

/**
 * 单个文件的对外状态。
 *
 * ``cancelling`` **不是队列里的状态**，而是「正在处理 + 用户已请求取消」的组合：
 * 已经在跑的那一个停不下来（重活在线程池里，LibreOffice / OCR 取消不了），
 * 界面必须如实显示「已请求取消，正在等它跑完」，而不是假装已经停了（§十五）。
 */
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

/**
 * GET /api/conversion/capabilities 的响应。
 *
 * 服务器**当前真的能做**的转换：没装 LibreOffice，矩阵里就没有 Office 那几类。
 * 目标格式选项一律从这里取，页面上不写死一份（§十九）。
 *
 * 第九阶段在这份响应上**追加**了四个键（``categories`` / ``formats`` /
 * ``conversions`` / ``operations``），既有七个键的名字与语义一字未动 ——
 * 旧页面与旧测试读的还是同一批字段。
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

  // ---- 第九阶段新增：统一转换中心的唯一能力来源（§十五 / §十六）----
  /** 能力类别：image / document / pdf */
  categories: ConversionCategoryOption[]
  /** 系统认识的每一种格式，含扩展名、MIME、能不能当源 / 目标 */
  formats: ConversionFormatOption[]
  /** 一条条 1→1 的转换能力 */
  conversions: ConversionCapability[]
  /** 多进多出 / 页面级的工具（合并、拆分…），不走统一队列 */
  operations: ConversionCapability[]
}

/** 能力类别（图片 / 文档 / PDF） */
export interface ConversionCategoryOption {
  value: string
  label: string
}

/** 一种格式。``is_source`` / ``is_target`` 决定它能出现在哪一侧 */
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

/** ``options_schema`` 里一个枚举取值 */
export interface ConversionOptionEnumValue {
  value: string
  label: string
}

/**
 * ``options_schema`` 里的一项。
 *
 * 键名是**扁平点号串**（``resize.width``），与提交时的 JSON 键逐字相同 ——
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
  /**
   * 快捷档（第十阶段 A §二十七 / §二十九）：质量的那七档、目标大小的那几档。
   *
   * 与 `enum` 的区别是**它不改变字段的类型** —— `enum` 是「只能从这几个里选」，
   * `presets` 是「这几个常用，其余也认」。所以它渲染成一行按钮，点了只是把值
   * 填进输入框，输入框照旧可以填任意合法值。
   *
   * 这张表由服务端下发，**前端一个数字都不许写死**：加一档、改一个数只改
   * 服务端一处。这里的值一律是字符串（与表单的字符串通道一致）。
   */
  presets?: ConversionOptionEnumValue[]
  /** 依赖项：全部成立时这一项才显示 */
  visible_when?: Record<string, unknown>
  /** 非空表示枚举取值要在运行期填（目前只有 ``"fonts"``） */
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
 * ``conversion`` 与 ``operation`` 共用这个形状：前者有 ``converter_key``
 * 隐含的执行路径、走统一队列；后者带 ``endpoint``，由各自的老接口执行。
 * 前端不需要知道这个区别来渲染选择器 —— 只有提交时才分岔。
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
  available: boolean
  supports_batch: boolean
  supports_options: boolean
  supports_preview: boolean
  /** 操作类条目才有：执行它的老接口 */
  endpoint: string | null
  method: string | null
  /** 标签，目前只有 ``recommended`` */
  tags: string[]
  /** 需要说给用户听的一句话（如「多帧文件只取第一帧」） */
  note: string | null
  options_schema: ConversionOptionsSchema | null
  /**
   * **只有操作类条目非空**：这份操作接受哪些源类型的文件。
   *
   * 与 ``source_type`` 不是一回事 —— 那是契约要求的单个标量，
   * 而「图片合成 PDF」收的是七种图片。文件白名单认这个字段。
   */
  accepts: string[]
  /**
   * **只有操作类条目非空**：文件怎么交给 ``endpoint``。
   *
   * * ``'files'`` —— 直接作为 multipart 文件字段发过去，可以一次多份；
   * * ``'input_id'`` —— 先 POST ``/api/pdf/upload`` 换一个令牌再调用，
   *   令牌与内容绑定，所以一次只处理一份。
   */
  input_field: 'files' | 'input_id' | null
}

/** 一个文件提交后的任务号（``{batch_id}:{index}``） */
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
  /** 与 `size` 同一个数（服务端一次赋值给两个键），压缩前后并排显示时用它 */
  output_size: number
  media_type: string
  /** **一次性**下载地址，下载过后就失效了 */
  download_url: string
  /**
   * 内联预览地址；这个格式没法在结果卡里显示时为 null（第十阶段 A §三十九–§四十一）。
   *
   * 能预览与否**由服务端判定** —— 前端不自己判断格式，那会变成第二份
   * 格式知识（加一种格式要改两处，漂移时没人发现）。这里只回答
   * 「有没有值」。预览**不消耗**下载令牌，可以反复看。
   */
  preview_url: string | null
  page_count: number | null
  width: number | null
  height: number | null
  /** 压缩前的字节数（§三十一）。非压缩类转换是 null —— 不报一个用不上的数 */
  original_size: number | null
  /** 用户要求的目标大小上限；没要求是 null */
  target_size: number | null
  /** 实际使用的质量；无损格式与未重编码的情形是 null */
  quality_used: number | null
  /**
   * 有没有达到目标大小。
   *
   * **null 表示「没有目标可谈」，不是「没达到」**（§三十二）。这两个的
   * 区别是这一节的要点：没要求却显示「未达成」，用户会以为服务器失手了。
   */
  target_reached: boolean | null
  notes: string[]
}

/** 批次里的单个文件（§八） */
export interface ConversionTaskStatus {
  task_id: string
  index: number
  source_filename: string
  /** 服务端按**真实内容**判定的源格式，可能与前端的分组不同 */
  source_type: string
  target_type: string
  status: ConversionStatus
  /**
   * **真实**百分比；无从得知时为 null。
   * 为 null 时界面上走不确定态动画，不编一个数字出来（§八 / §十二）。
   */
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
  /**
   * 服务端**自动**重新排队过几次（第八阶段）。与 `retry_count` 是两件事：
   * 后者是用户自己点重试按钮的次数。合成一个字段的话，
   * 「用户按了两次」和「服务器替你重排了两次」在界面上会长得一模一样。
   */
  auto_retry_count: number
  /**
   * 这一项被判过超时。**不代表活已经停了** —— 超时只释放调度槽位，
   * 底层线程会跑完，所以文案上不能说「已停止」（§十七）。
   */
  timeout_requested: boolean
  /**
   * 服务端命中的那条能力的 ID（第九阶段 §四十三），例如
   * ``image.jpg-to-png``。空串表示服务端没能判定出唯一一条。
   */
  conversion_id: string
  /**
   * 服务端**收敛后**的参数快照（第九阶段）：界面回显用的是它，
   * 而不是前端提交时的那份草稿 —— 用户看到的就是真正跑起来的那组值。
   */
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
