/**
 * 后端接口调用。
 *
 * 开发环境下 Vite 会把 /api 代理到 FastAPI，前后端同源；
 * 如果前后端分开部署，设置 VITE_API_BASE_URL=http://host:8000 即可。
 */

import type {
  ApiErrorBody,
  ConversionBatchCreated,
  ConversionBatchStatus,
  ConversionCapabilities,
  ConversionCapability,
  DocKind,
  DocTargetOption,
  DocumentResultResponse,
  OutputFormat,
  PdfCompressLevel,
  PdfFit,
  PdfInputResponse,
  PdfMargin,
  PdfOrientation,
  PdfPageSize,
  PdfResolution,
  PdfResultResponse,
  PdfSplitMode,
  PdfToWordProgress,
  PublicConfig,
  QualityPreset,
  TaskCreated,
  TaskSnapshot,
  TxtOrientation,
} from '@/types'

import { isDesktop, saveResultToDownloads } from './desktop'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

/** 带后端错误码的异常 */
export class ApiError extends Error {
  readonly code: string
  readonly status: number

  constructor(message: string, code: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

const NETWORK_ERROR_MESSAGE = '无法连接服务器，请确认后端服务已启动'

/** 从错误响应里取出后端给出的中文提示 */
async function readErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as Partial<ApiErrorBody>
    if (body?.error?.message) return body.error.message
  } catch {
    // 响应不是 JSON，退回到通用提示
  }
  if (response.status === 413) return '文件超过服务器允许的大小上限'
  if (response.status === 404) return '请求的资源不存在或已过期'
  if (response.status >= 500) return '服务器处理失败，请稍后重试'
  return `请求失败（HTTP ${response.status}）`
}

function parseErrorPayload(text: string, status: number): ApiError {
  try {
    const body = JSON.parse(text) as Partial<ApiErrorBody>
    if (body?.error?.message) {
      return new ApiError(body.error.message, body.error.code ?? 'error', status)
    }
  } catch {
    // 忽略解析失败
  }
  return new ApiError(`请求失败（HTTP ${status}）`, 'error', status)
}

/** 拉取服务端限制配置 */
export async function fetchPublicConfig(signal?: AbortSignal): Promise<PublicConfig> {
  return getJson<PublicConfig>('/api/config', { signal, code: 'config_failed' })
}

interface GetJsonOptions {
  signal?: AbortSignal
  /** 请求失败且服务端没给出错误码时使用的本地错误码 */
  code: string
  /**
   * 请求方法，默认 GET。
   * 取消 / 重试这类「参数全在路径里」的动作用 POST —— 没有请求体，
   * 套一层 multipart 只是白搭。
   */
  method?: 'GET' | 'POST'
}

/** 请求一个 JSON 接口；失败时抛出带错误码的 ApiError。 */
async function getJson<T>(path: string, options: GetJsonOptions): Promise<T> {
  const { signal, code, method = 'GET' } = options
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, { method, signal })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(NETWORK_ERROR_MESSAGE, 'network_error', 0)
  }

  if (!response.ok) {
    throw new ApiError(await readErrorMessage(response), code, response.status)
  }
  try {
    return (await response.json()) as T
  } catch {
    throw new ApiError('服务器返回了无法解析的数据', 'bad_response', response.status)
  }
}

/**
 * 查询任务状态（第四阶段 §3 / §4）。
 *
 * 轮询接口只要任务还在就返回 200：某个文件失败是任务的结果，
 * 不是这次查询的错误 —— 因此这里解析出的快照才是唯一的进度来源。
 */
export function getTask<R>(statusUrl: string, signal?: AbortSignal): Promise<TaskSnapshot<R>> {
  return getJson<TaskSnapshot<R>>(statusUrl, { signal, code: 'TASK_NOT_FOUND' })
}

export interface UploadOptions {
  /** 上传进度回调，参数为 0-1 */
  onUploadProgress?: (ratio: number) => void
  signal?: AbortSignal
}

/**
 * 上传表单并解析 JSON 响应。
 *
 * 用 XMLHttpRequest 而不是 fetch —— fetch 无法获取上传进度。
 * 所有图片与 PDF 接口共用这一份实现，超时、错误提示、取消行为完全一致。
 *
 * 超时给到 10 分钟：服务端自己的处理超时是 60–300 秒，会先返回明确的中文
 * 错误（例如「压缩超时」）。客户端必须等得比服务端久，否则用户看到的
 * 永远是「请求超时」，而不是真正的原因。
 */
function postForm<T>(path: string, form: FormData, options: UploadOptions = {}): Promise<T> {
  const { onUploadProgress, signal } = options
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API_BASE}${path}`)
    // 不要手动设置 Content-Type，浏览器需要自己补 multipart 边界
    xhr.timeout = 10 * 60 * 1000

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onUploadProgress?.(event.loaded / event.total)
      }
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as T)
        } catch {
          reject(new ApiError('服务器返回了无法解析的数据', 'bad_response', xhr.status))
        }
        return
      }
      reject(parseErrorPayload(xhr.responseText, xhr.status))
    }

    xhr.onerror = () => reject(new ApiError(NETWORK_ERROR_MESSAGE, 'network_error', 0))
    xhr.ontimeout = () => reject(new ApiError('请求超时，请稍后重试或换一个更小的文件', 'timeout', 0))
    xhr.onabort = () => reject(new DOMException('请求已取消', 'AbortError'))

    if (signal) {
      if (signal.aborted) {
        xhr.abort()
        return
      }
      signal.addEventListener('abort', () => xhr.abort(), { once: true })
    }

    try {
      xhr.send(form)
    } catch {
      reject(new ApiError('发送请求失败，请重试', 'send_failed', 0))
    }
  })
}

export interface CompressParams extends UploadOptions {
  files: File[]
  quality: QualityPreset
  /** 目标大小（字节），null 表示不限制 */
  targetBytes: number | null
}

/** 上传并压缩图片（可批量）。返回任务号，进度与结果走 /api/tasks。 */
export function compressImages({ files, quality, targetBytes, ...options }: CompressParams) {
  const form = new FormData()
  for (const file of files) {
    form.append('files', file)
  }
  form.append('quality', quality)
  if (targetBytes !== null) {
    form.append('target_bytes', String(targetBytes))
  }
  return postForm<TaskCreated>('/api/image/compress', form, options)
}

export interface ConvertParams extends UploadOptions {
  files: File[]
  targetFormat: OutputFormat
  /** 图片质量 1-100，null 表示使用服务端默认值 */
  qualityValue: number | null
  /** 目标大小（字节），null 表示不限制 */
  targetBytes: number | null
}

/** 上传并转换图片格式（可批量）。返回任务号，进度与结果走 /api/tasks。 */
export function convertImages({
  files,
  targetFormat,
  qualityValue,
  targetBytes,
  ...options
}: ConvertParams) {
  const form = new FormData()
  for (const file of files) {
    form.append('files', file)
  }
  form.append('target_format', targetFormat)
  if (qualityValue !== null) {
    form.append('quality_value', String(qualityValue))
  }
  if (targetBytes !== null) {
    form.append('target_bytes', String(targetBytes))
  }
  return postForm<TaskCreated>('/api/image/convert', form, options)
}

export interface ResizeParams extends UploadOptions {
  files: File[]
  /** 目标宽度（像素），null 表示按高度等比推导 */
  width: number | null
  /** 目标高度（像素），null 表示按宽度等比推导 */
  height: number | null
  keepAspect: boolean
  qualityValue: number | null
  targetBytes: number | null
}

/** 上传并调整图片尺寸（可批量）。返回任务号，进度与结果走 /api/tasks。 */
export function resizeImages({
  files,
  width,
  height,
  keepAspect,
  qualityValue,
  targetBytes,
  ...options
}: ResizeParams) {
  const form = new FormData()
  for (const file of files) {
    form.append('files', file)
  }
  if (width !== null) form.append('width', String(width))
  if (height !== null) form.append('height', String(height))
  form.append('keep_aspect', keepAspect ? 'true' : 'false')
  if (qualityValue !== null) {
    form.append('quality_value', String(qualityValue))
  }
  if (targetBytes !== null) {
    form.append('target_bytes', String(targetBytes))
  }
  return postForm<TaskCreated>('/api/image/resize', form, options)
}

/**
 * 一次下载的去向。
 *
 * 界面上「已取走」的判定两个平台一样（都算下载成功），但桌面端多一件事要做：
 * 把落盘路径显示出来，并给「打开 / 在文件夹中显示」两个动作当入参。
 */
export type DownloadOutcome =
  | { kind: 'browser' }
  | { kind: 'desktop'; path: string }

/**
 * 下载结果文件。
 *
 * Web 上先取回 blob 再触发下载，好处是：可以拿到真实文件名、能捕获失败并提示，
 * 也不会因为浏览器直接打开链接而在新标签页里预览图片。
 *
 * 桌面端（Tauri）走另一条路：WebView2 里 `<a download>` + `createObjectURL`
 * 是**静默 no-op**，点了没反应也不报错。所以那里把字节交给 Rust 直接写进
 * 「下载」目录，并把落盘路径返回给界面。见 `services/desktop.ts`。
 */
export async function downloadResult(
  path: string,
  filename: string,
): Promise<DownloadOutcome> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`)
  } catch {
    throw new ApiError(NETWORK_ERROR_MESSAGE, 'network_error', 0)
  }

  if (!response.ok) {
    throw new ApiError(await readErrorMessage(response), 'download_failed', response.status)
  }

  const blob = await response.blob()

  if (isDesktop) {
    try {
      const savedPath = await saveResultToDownloads(
        new Uint8Array(await blob.arrayBuffer()),
        filename,
      )
      return { kind: 'desktop', path: savedPath }
    } catch (caught) {
      // Rust 侧的 `Err` 里是给用户看的中文，别让它被通用文案吃掉
      const message =
        caught instanceof Error && caught.message ? caught.message : '保存失败，请重试'
      throw new ApiError(message, 'download_failed', 0)
    }
  }

  // ---- 以下是 Web 的原有路径，一行未改 ----
  const objectUrl = URL.createObjectURL(blob)

  try {
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = filename
    anchor.rel = 'noopener'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    // 交给浏览器读完再释放
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 10_000)
  }

  return { kind: 'browser' }
}

// ----------------------------------------------------------------------
// PDF 工具（第三阶段）
// ----------------------------------------------------------------------

/**
 * 上传一份 PDF，拿到后续操作要用的 input_id。
 *
 * 「PDF 转图片」「拆分」「删除页面」「提取页面」「压缩」都先走这一步：
 * 上传一次就能连续做几个操作，也能立刻拿到页数用于页面选择器。
 */
export function uploadPdf({ file, ...options }: UploadOptions & { file: File }) {
  const form = new FormData()
  form.append('file', file)
  return postForm<PdfInputResponse>('/api/pdf/upload', form, options)
}

export interface FromImagesParams extends UploadOptions {
  files: File[]
  pageSize: PdfPageSize
  orientation: PdfOrientation
  fit: PdfFit
  margin: PdfMargin
  /** 自定义页面尺寸（毫米），仅在 pageSize 为 custom 时使用 */
  customWidthMm: string | null
  customHeightMm: string | null
}

/** 把多张图片按顺序合并成一个 PDF。 */
export function imagesToPdf({
  files,
  pageSize,
  orientation,
  fit,
  margin,
  customWidthMm,
  customHeightMm,
  ...options
}: FromImagesParams) {
  const form = new FormData()
  for (const file of files) {
    form.append('files', file)
  }
  form.append('page_size', pageSize)
  form.append('orientation', orientation)
  form.append('fit', fit)
  form.append('margin', margin)
  if (pageSize === 'custom') {
    if (customWidthMm) form.append('custom_width_mm', customWidthMm)
    if (customHeightMm) form.append('custom_height_mm', customHeightMm)
  }
  return postForm<PdfResultResponse>('/api/pdf/from-images', form, options)
}

export interface ToImagesParams extends UploadOptions {
  /** 已上传 PDF 的 input_id，可以多份一起导出 */
  inputIds: string[]
  targetFormat: OutputFormat
  /** 页面范围，null 表示全部；对所有 PDF 生效 */
  pages: string | null
  /** 图片质量 1-100，PNG 不发送 */
  quality: number | null
  resolution: PdfResolution
}

/** 把 PDF 的指定页面导出成图片（可多份）。返回任务号，进度与结果走 /api/tasks。 */
export function pdfToImages({
  inputIds,
  targetFormat,
  pages,
  quality,
  resolution,
  ...options
}: ToImagesParams) {
  const form = new FormData()
  for (const inputId of inputIds) {
    form.append('input_ids', inputId)
  }
  form.append('target_format', targetFormat)
  form.append('resolution', resolution)
  if (pages) form.append('pages', pages)
  if (quality !== null) form.append('quality', String(quality))
  return postForm<TaskCreated>('/api/pdf/to-images', form, options)
}

/** 按给定顺序把多个 PDF 合并成一个。 */
export function mergePdfs({ files, ...options }: UploadOptions & { files: File[] }) {
  const form = new FormData()
  for (const file of files) {
    form.append('files', file)
  }
  return postForm<PdfResultResponse>('/api/pdf/merge', form, options)
}

export interface SplitParams extends UploadOptions {
  inputId: string
  mode: PdfSplitMode
  /** ranges 为「一行一个范围」，selected 为「1,3,5,8」，every 不发送 */
  value: string | null
}

/** 拆分 PDF：每页一个、按范围分、或只留选中的页。 */
export function splitPdf({ inputId, mode, value, ...options }: SplitParams) {
  const form = new FormData()
  form.append('input_id', inputId)
  form.append('mode', mode)
  if (value) form.append('pages', value)
  return postForm<PdfResultResponse>('/api/pdf/split', form, options)
}

/** 删除 / 提取页面的公共入参 */
export interface PageEditParams extends UploadOptions {
  inputId: string
  /** 规范化的页码文本，例如 2,4,7-9 */
  pages: string
}

/** 删掉指定页面，其余页面按原顺序生成一份新 PDF。 */
export function deletePdfPages({ inputId, pages, ...options }: PageEditParams) {
  const form = new FormData()
  form.append('input_id', inputId)
  form.append('pages', pages)
  return postForm<PdfResultResponse>('/api/pdf/delete-pages', form, options)
}

/** 只保留指定页面，合成一份新的 PDF。 */
export function extractPdfPages({ inputId, pages, ...options }: PageEditParams) {
  const form = new FormData()
  form.append('input_id', inputId)
  form.append('pages', pages)
  return postForm<PdfResultResponse>('/api/pdf/extract-pages', form, options)
}

export interface CompressPdfParams extends UploadOptions {
  inputId: string
  level: PdfCompressLevel
  /** 目标大小档位 */
  target: string
  /** 自定义目标大小（MB），仅在 target 为 custom 时发送 */
  targetMb: string | null
}

/** 压缩 PDF。 */
export function compressPdf({
  inputId,
  level,
  target,
  targetMb,
  ...options
}: CompressPdfParams) {
  const form = new FormData()
  form.append('input_id', inputId)
  form.append('level', level)
  form.append('target', target)
  if (target === 'custom' && targetMb) form.append('target_mb', targetMb)
  return postForm<PdfResultResponse>('/api/pdf/compress', form, options)
}

export interface RunPdfOperationParams extends UploadOptions {
  /** 服务端登记的那一条能力（``endpoint`` / ``input_field`` 都从它读） */
  entry: ConversionCapability
  files: File[]
  /** 已序列化的选项，键名与端点的 Form 字段一一对应 */
  fields: Record<string, string>
  /** 上传与处理分两段时，告诉调用方现在在哪一段 */
  onPhase?: (phase: 'upload' | 'run') => void
}

/**
 * 执行一个 PDF 操作（第九阶段 §十二 的 ``operation`` 条目）。
 *
 * **不重复实现任何一样**：路径来自条目的 ``endpoint``，参数名就是各端点
 * 原来的 Form 字段，结果仍是各端点原来的 ``PdfResultResponse`` ——
 * 所以「合并」「拆分」这些行为的唯一真相还在那几个老接口里，
 * 统一中心只是它们的一个入口（决策 B）。
 *
 * 唯一的差别是文件怎么交过去，由条目的 ``input_field`` 说明：
 *
 * * ``files``：直接把文件塞进本次请求；
 * * ``input_id``：先逐份 ``POST /api/pdf/upload`` 换令牌，再带着令牌调用。
 *   令牌与内容绑定，所以这条路上**一次只处理一份**文件。
 */
export async function runPdfOperation({
  entry,
  files,
  fields,
  onPhase,
  ...options
}: RunPdfOperationParams): Promise<PdfResultResponse> {
  if (!entry.endpoint) {
    throw new ApiError('这个操作暂时不可用', 'operation_unavailable', 0)
  }

  const form = new FormData()

  if (entry.input_field === 'input_id') {
    onPhase?.('upload')
    const uploaded = await uploadPdf({ file: files[0]!, ...options })
    form.append('input_id', uploaded.input_id)
  } else {
    for (const file of files) form.append('files', file)
  }

  for (const [key, value] of Object.entries(fields)) form.append(key, value)

  onPhase?.('run')
  return postForm<PdfResultResponse>(entry.endpoint, form, options)
}

// ----------------------------------------------------------------------
// 文档转换（第五阶段）
// ----------------------------------------------------------------------

/** 文档类别 -> 接口路径。后端每个类别一个接口，路径由类别推出。 */
const DOC_ENDPOINTS: Record<DocKind, string> = {
  word: 'word-to-pdf',
  excel: 'excel-to-pdf',
  ppt: 'powerpoint-to-pdf',
  txt: 'txt-to-pdf',
}

/** TXT 的排版选项。会拼进表单的只有前四项，转成字符串发送。 */
export interface TxtLayoutParams {
  font: string
  fontSize: number
  pageSize: string
  orientation: TxtOrientation
}

export interface DocToPdfParams extends UploadOptions {
  kind: DocKind
  file: File
  /** 「最大文件大小」档位。TXT 不用它（产物大小由排版决定），传 null 表示不发 */
  target: DocTargetOption | null
  /** 自定义目标大小（MB），仅在 target 为 custom 时发送 */
  targetMb: string | null
  /** TXT 专用；其它格式不传，传了后端也会忽略 */
  txt?: TxtLayoutParams
}

/**
 * 把一份文档转成 PDF。
 *
 * **一发式**：上传和转换在同一次请求里完成，服务器不留原始文档 ——
 * 用户选好文件、选好选项，点一次按钮就拿到 PDF。
 * 代价是换选项重试时要重新上传一次（文件还在浏览器内存里）。
 *
 * TXT 走的是自己的一套排版参数（字体 / 字号 / 页面大小 / 方向），
 * 不发 ``target``：一份纯文本排出来的 PDF 谈目标大小没有意义。
 */
export function docToPdf({ kind, file, target, targetMb, txt, ...options }: DocToPdfParams) {
  const form = new FormData()
  form.append('file', file)

  if (txt) {
    form.append('font', txt.font)
    form.append('font_size', String(txt.fontSize))
    form.append('page_size', txt.pageSize)
    form.append('orientation', txt.orientation)
  } else if (target) {
    form.append('target', target)
    if (target === 'custom' && targetMb) form.append('target_mb', targetMb)
  }

  return postForm<PdfResultResponse>(`/api/office/${DOC_ENDPOINTS[kind]}`, form, options)
}

// ----------------------------------------------------------------------
// PDF 转 Word（第六阶段 A）
// ----------------------------------------------------------------------

/**
 * 生成一个进度 id。
 *
 * 形状必须匹配服务端的 ``^[A-Za-z0-9_-]{8,64}$`` —— 它被当成不可信输入校验。
 * ``crypto.randomUUID`` 只在安全上下文（https 或 localhost）里有，
 * 局域网 http 访问时是 undefined，所以备一条时间戳 + 随机数的退路：
 * 进度 id 只用于「读回自己这次转换的阶段」，不需要不可预测。
 */
export function newProgressId(): string {
  const webcrypto = globalThis.crypto as Crypto | undefined
  if (typeof webcrypto?.randomUUID === 'function') return webcrypto.randomUUID()
  return `pw${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
}

export interface PdfToWordParams extends UploadOptions {
  file: File
  /** 轮询阶段进度用的标识；服务端只校验形状，不合法当作没有 */
  progressId: string | null
}

/**
 * 把一份 PDF 转成 Word（.docx）。
 *
 * **一发式**，和文档转 PDF 一样：上传与转换在同一次请求里完成，
 * 服务端不留原始 PDF。响应里会说明这份 PDF 是文字版还是扫描版、
 * 内容是怎么取出来的 —— 扫描件是 OCR 认的，可能认错字，结果页要如实告诉用户。
 */
export function pdfToWord({ file, progressId, ...options }: PdfToWordParams) {
  const form = new FormData()
  form.append('file', file)
  if (progressId) form.append('progress_id', progressId)
  return postForm<DocumentResultResponse>('/api/office/pdf-to-word', form, options)
}

/**
 * 查询一次转换走到哪一步了。
 *
 * 只读接口：查不到（id 不合法、已过期、转换已经结束）就是 404，
 * 调用方静默处理即可 —— 进度只是锦上添花，拿不到不该影响转换本身。
 */
export function getPdfToWordProgress(
  progressId: string,
  signal?: AbortSignal,
): Promise<PdfToWordProgress> {
  return getJson<PdfToWordProgress>(
    `/api/office/pdf-to-word/progress/${encodeURIComponent(progressId)}`,
    { signal, code: 'JOB_NOT_FOUND' },
  )
}

// ----------------------------------------------------------------------
// 统一转换中心（第七阶段）
//
// 六个端点都在 /api/conversion 下。前六个阶段的接口一个都没动：
// 这里是**新增入口**，旧的十四个专用工具继续走它们原来的接口。
// ----------------------------------------------------------------------

/**
 * 服务器当前**真的**能做的转换。
 *
 * 目标格式选项一律取自这里，页面上不写死 —— 服务器缺组件时那些能力
 * 会从矩阵里消失，界面必须跟着消失，而不是让用户选了再报错（§十九）。
 */
export function fetchConversionCapabilities(
  signal?: AbortSignal,
): Promise<ConversionCapabilities> {
  return getJson<ConversionCapabilities>('/api/conversion/capabilities', {
    signal,
    code: 'config_failed',
  })
}

export interface ConversionSubmitParams extends UploadOptions {
  files: File[]
  /** 这一批统一的目标格式，取值来自 capabilities */
  targetType: string
  /**
   * 选中的那条能力的 ID（``image.jpg-to-png``）。
   *
   * 声明之后服务端把参数白名单收窄成**这一条能力自己的** schema，
   * 于是「旧界面配上新能力」这类版本错配会当场得到一句明确的错误，
   * 而不是被静默忽略。空串表示不声明。
   */
  capabilityId: string
  /**
   * 第九阶段的统一参数（扁平点号键，如 ``{"resize.mode":"small"}``）。
   *
   * 由 ``utils/conversionOptions.serializeOptions`` 从**同一份** schema 生成 ——
   * 界面上渲染的是它，发出去的也是它。空对象表示只用下面那些扁平字段。
   */
  options: Record<string, unknown>
  /** 目标最大字节数；null 表示不限制 */
  targetBytes: number | null
  /** 与 files 顺序一一对齐的进度 id（服务端只校验形状） */
  progressIds: string[]
}

/**
 * 提交一批同源同目标的文件，返回 202。
 *
 * 每个文件的**真实格式由服务端按内容判定**，客户端的分组只用于界面排版：
 * 声明与真实内容不符、或组合不在矩阵里的文件，会作为一项失败出现在结果里，
 * 并带上具体原因（§五）。
 *
 * 第七阶段的那几个扁平字段（``quality_preset`` / ``quality_value`` /
 * ``width`` / ``height`` / ``keep_aspect``）**仍然在线**，但第九阶段的前端
 * 不再用它们：选项一律走 ``options``，两处发同一件事只会有一处是旧的。
 * 只有 ``target_bytes`` 还走扁平字段 —— 「结果不超过多大」是跨格式的
 * 后处理，不在任何一条能力的 ``options_schema`` 里。
 */
export function createConversionTasks({
  files,
  targetType,
  capabilityId,
  options,
  targetBytes,
  progressIds,
  ...rest
}: ConversionSubmitParams) {
  const form = new FormData()
  for (const file of files) {
    form.append('files', file)
  }
  // 进度 id 必须与 files 一一对齐，所以哪怕这一项没有也发一个空串 ——
  // 服务端只认形状合法的，空串当作没传，而位置不会错开。
  for (const progressId of progressIds) {
    form.append('progress_ids', progressId)
  }
  form.append('target_type', targetType)
  if (capabilityId) form.append('capability_id', capabilityId)
  if (Object.keys(options).length > 0) form.append('options', JSON.stringify(options))
  if (targetBytes !== null) form.append('target_bytes', String(targetBytes))
  return postForm<ConversionBatchCreated>('/api/conversion/tasks', form, rest)
}

/**
 * 查询一批转换的状态。
 *
 * 和第四阶段一样：只要任务还在，轮询永远返回 200 ——
 * 某个文件失败是任务的结果，不是这次查询的错误。
 */
export function getConversionBatch(
  statusUrl: string,
  signal?: AbortSignal,
): Promise<ConversionBatchStatus> {
  return getJson<ConversionBatchStatus>(statusUrl, { signal, code: 'TASK_NOT_FOUND' })
}

/**
 * 请求取消整批。
 *
 * **协作式，不是立刻停**（§十五）：还在排队的文件会立刻取消，已经在转换的
 * 那一个停不下来，会如实显示成 ``cancelling``。返回的是请求之后的最新状态。
 */
export function cancelConversionBatch(
  cancelUrl: string,
  signal?: AbortSignal,
): Promise<ConversionBatchStatus> {
  return getJson<ConversionBatchStatus>(cancelUrl, {
    signal,
    code: 'TASK_NOT_FOUND',
    method: 'POST',
  })
}

/**
 * 重试单个失败的文件。
 *
 * 每项最多一次，上限在服务端强制；排队中 / 已完成 / 已取消的项
 * 会返回 ``TASK_NOT_RETRYABLE``（409）。
 */
export function retryConversionTask(
  taskId: string,
  signal?: AbortSignal,
): Promise<ConversionBatchStatus> {
  return getJson<ConversionBatchStatus>(
    `/api/conversion/tasks/${encodeURIComponent(taskId)}/retry`,
    { signal, code: 'TASK_NOT_RETRYABLE', method: 'POST' },
  )
}
