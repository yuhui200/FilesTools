/**
 * 结果下载（规格 §十七）。
 *
 * ## 为什么下载结果必须落到本地
 *
 * 服务端的 `download_url` 是**一次性**的：`/api/download/{job_id}` 发完文件
 * 就会把目录删掉，`job_store` 里的令牌也一并作废。所以「下载」在这里是
 * **一次**操作 —— 下到本地之后，后续的「分享 / 打开 / 另存」全部用本地那份，
 * 谁都不许再点一次网络下载（第二次必然 404）。
 *
 * ## 平台差异（收在这一层）
 *
 * - **原生**：`File.downloadFileAsync` 直接流式写盘（Android 边下边写、
 *   iOS 先落临时位置再搬），全程不经过 JS 堆 —— 手机上几十 MB 的结果
 *   用 `fetch` 全读进内存会直接顶到堆上限。
 * - **Web**：`expo-file-system` 在 Web 上是**空实现**（调了只 `console.warn`
 *   然后 resolve `undefined`），所以这条分支必须走浏览器自己的下载路径。
 */

import { Platform } from 'react-native'
import { Directory, File, Paths } from 'expo-file-system'
import type { DownloadProgress } from 'expo-file-system'

import { API_BASE_URL, ApiError, CLIENT_CODES, apiUrl } from '@/services/api/client'

/** 结果落在缓存目录的子目录里 —— 系统清空间时可以回收，不会撑爆手机 */
const CACHE_DIR = 'filetools-results'

export interface DownloadedFile {
  /** 本地文件地址（原生是 `file://…`，Web 是 `blob:…`），可直接交给分享/打开 */
  uri: string
  /** 文件名，含扩展名 */
  name: string
  /** 字节数 */
  size: number
}

export interface DownloadRequest {
  /** 服务端给的 `download_url`（相对路径，如 `/api/download/xxxx`） */
  url: string
  /**
   * 结果文件名。Web 分支靠它决定另存为什么名字 ——
   * 浏览器只会拿 URL 的最后一段当文件名，那是一串令牌，不是用户认得的东西。
   */
  filename?: string
  /** 取消下载（组件卸载、用户返回时用） */
  signal?: AbortSignal
  /** 下载进度，0–1；`Content-Length` 缺失时一次都不会调 */
  onProgress?: (ratio: number) => void
}

/**
 * 把一次性结果地址取回本地。
 *
 * 失败时不留半个文件：Android 是边下边写，中断会剩半截，这里删干净再抛错。
 */
export async function downloadResult(request: DownloadRequest): Promise<DownloadedFile> {
  const url = apiUrl(request.url)

  if (Platform.OS === 'web') return downloadOnWeb(url, request)

  const directory = ensureCacheDirectory()
  let file: File
  try {
    file = await File.downloadFileAsync(url, directory, {
      // 目标目录是我们的缓存目录，同名文件只可能是上一次的残留 —— 覆盖它
      idempotent: true,
      ...(request.signal ? { signal: request.signal } : {}),
      ...(request.onProgress ? { onProgress: progressReporter(request.onProgress) } : {}),
    })
  } catch (error) {
    throw toDownloadError(error)
  }

  // 文档保证非 2xx 会 reject 且不落文件；走到这里却没有文件说明出了别的事，如实报错
  if (!file || !file.exists) {
    throw new ApiError('下载结果失败，请重试', CLIENT_CODES.file)
  }

  return { uri: file.uri, name: file.name, size: file.size }
}

/** `totalBytes` 为 -1 表示服务器没给 Content-Length —— 这时不报进度，编一个百分比比不报更糟 */
function progressReporter(onProgress: (ratio: number) => void) {
  return ({ bytesWritten, totalBytes }: DownloadProgress): void => {
    if (totalBytes > 0) onProgress(Math.min(1, bytesWritten / totalBytes))
  }
}

/** 缓存目录按需创建；已存在时 `create` 直接返回，不报错 */
function ensureCacheDirectory(): Directory {
  const directory = new Directory(Paths.cache, CACHE_DIR)
  if (!directory.exists) {
    try {
      directory.create({ intermediates: true, idempotent: true })
    } catch {
      // 竞态：另一个下载刚好建好了。真失败的话下面的下载自己会报错
    }
  }
  return directory
}

/**
 * Web：走浏览器自己的下载。
 *
 * 先把响应体读成 blob 再触发另存，而不是直接把 URL 丢给 `<a download>` ——
 * 两个原因：一是这样才拿得到真实字节数（界面要显示），二是一旦把一次性
 * 地址交给浏览器，我们连它成没成功都不知道。
 */
async function downloadOnWeb(url: string, request: DownloadRequest): Promise<DownloadedFile> {
  let response: Response
  try {
    response = await fetch(url, request.signal ? { signal: request.signal } : {})
  } catch (error) {
    throw toDownloadError(error)
  }

  if (!response.ok) throw await errorFromResponse(response)

  const blob = await response.blob()
  const name = request.filename ?? nameFromDisposition(response) ?? 'result'
  const uri = URL.createObjectURL(blob)
  saveViaAnchor(uri, name)
  return { uri, name, size: blob.size }
}

/** 解析 `Content-Disposition` 里的文件名 —— 服务端知道结果该叫什么 */
function nameFromDisposition(response: Response): string | null {
  const header = response.headers.get('Content-Disposition')
  if (!header) return null
  // FastAPI 的 FileResponse 发的是 filename="..."，文件名非 ASCII 时还会带 filename*=utf-8''...
  const extended = /filename\*=utf-8''([^;]+)/i.exec(header)
  if (extended) return safeDecode(extended[1])
  const plain = /filename="?([^";]+)"?/i.exec(header)
  return plain ? safeDecode(plain[1]) : null
}

function safeDecode(text: string): string {
  try {
    return decodeURIComponent(text)
  } catch {
    return text
  }
}

function saveViaAnchor(uri: string, name: string): void {
  if (typeof document === 'undefined') return
  const anchor = document.createElement('a')
  anchor.href = uri
  anchor.download = name
  anchor.rel = 'noopener'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

/**
 * 非 2xx 的下载。
 *
 * `File.downloadFileAsync` 拒绝时**不给我们响应体**（文档明确：非 2xx 就 rejection，
 * 不落任何文件），所以这里只能从错误信息里认状态码。认不出来时按服务器错误处理 ——
 * **绝不假装成功**。
 */
function toDownloadError(error: unknown): ApiError {
  if (error instanceof ApiError) return error

  if (error instanceof Error && error.name === 'AbortError') {
    return new ApiError('下载已取消', CLIENT_CODES.timeout)
  }

  const status = statusFromMessage(error)
  if (status === 404 || status === 410) {
    // 结果只保留很短时间，且下载过一次就作废 —— 两种情形对用户都是「重新处理一次」
    return new ApiError('结果文件已过期', 'JOB_NOT_FOUND', status)
  }
  if (status !== null && status >= 500) {
    return new ApiError('服务器出错了', 'SERVER_ERROR', status)
  }
  if (status !== null) {
    return new ApiError('下载结果失败，请重试', 'PROCESSING_FAILED', status)
  }

  // 认不出状态码：区分「根本没配地址」和「连不上」
  if (!API_BASE_URL) return new ApiError('没有配置服务器地址', CLIENT_CODES.config)
  return new ApiError('无法连接服务器', CLIENT_CODES.network)
}

function statusFromMessage(error: unknown): number | null {
  const text = error instanceof Error ? error.message : ''
  const match = /\b([1-5]\d{2})\b/.exec(text)
  return match ? Number(match[1]) : null
}

/** Web 分支的失败：`fetch` 拿得到完整响应，直接用服务端的错误信封 */
async function errorFromResponse(response: Response): Promise<ApiError> {
  let code = 'SERVER_ERROR'
  let message = ''
  try {
    const body = (await response.json()) as { error?: { code?: unknown; message?: unknown } }
    if (typeof body?.error?.code === 'string') code = body.error.code
    if (typeof body?.error?.message === 'string') message = body.error.message
  } catch {
    // 不是 JSON（网关的 HTML 错误页之类），保留默认码
  }
  return new ApiError(message || `下载失败（HTTP ${response.status}）`, code, response.status)
}
