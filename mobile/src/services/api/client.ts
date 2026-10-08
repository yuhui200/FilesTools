/**
 * 与后端通信的唯一出口（规格 §十一 / §三十三）。
 *
 * 页面与业务 service **一律不直接 fetch**：错误归一、超时、基地址、
 * 卸载时中止，这四件事只要有一处漏掉，用户就会看到白屏或者
 * 「请求失败（HTTP 500）」这种没用的话。全部收在这里，只有一处要改。
 *
 * ## 地址
 *
 * 基地址来自 `EXPO_PUBLIC_API_BASE_URL`（规格 §十二）。**没有兜底值** ——
 * Expo 的 `EXPO_PUBLIC_*` 是在打包时**静态替换**进产物的，
 * 写一个 `http://localhost:8000` 当默认值，真机上连的就是手机自己，
 * 而且还能「构建成功」。宁可启动时明确报「没配服务器地址」，
 * 也不给一个装成能用的假地址。
 *
 * ## 错误
 *
 * 一律抛 `ApiError`。它的 `message` 来自后端 `error.message`（已经是中文、
 * 且后端保证不含堆栈与内部路径），`code` 是后端的错误码 —— 界面按码
 * 补「接下来该做什么」（`utils/errorMessages.ts`）。
 *
 * 网络层自己产生的失败（连不上 / 超时 / 响应不是 JSON）用 `client.ts`
 * 自己的三个码，与后端码放在同一个命名空间里，界面不必区分来源。
 */

import type { ApiErrorBody } from '@/types'

/**
 * 后端基地址。
 *
 * ⚠️ `process.env.EXPO_PUBLIC_API_BASE_URL` 必须写成**完整的字面量成员表达式** ——
 * babel-preset-expo 是在编译期按这个名字做字符串替换的，拆成
 * `process.env[name]` 或先赋给变量再取，替换都不会发生，运行时拿到 undefined。
 */
export const API_BASE_URL = (process.env.EXPO_PUBLIC_API_BASE_URL ?? '').replace(/\/+$/, '')

/** 有没有配置服务器地址。没配时界面要给出可操作的提示，而不是空列表 */
export const isApiConfigured = API_BASE_URL.length > 0

/** Mobile 自己产生的错误码，与后端错误码共用一套命名 */
export const CLIENT_CODES = {
  /** 连不上服务器（DNS / 拒绝连接 / 断网） */
  network: 'network_error',
  /** 超过本地设定的等待时限 */
  timeout: 'timeout',
  /** 响应不是预期的 JSON */
  response: 'bad_response',
  /** 没有配置 EXPO_PUBLIC_API_BASE_URL */
  config: 'config_failed',
  /** 本地文件读写失败（下载 / 分享） */
  file: 'file_error',
} as const

/**
 * 是不是开发构建。诊断日志只在开发构建里打。
 *
 * 用 `process.env.NODE_ENV` 而**不是** React Native 的 `__DEV__` 全局：
 * `scripts/verify_mobile_phase11a.py` 会用一份刻意最小的 tsconfig
 * （`types: ["node"]`）把 `errorMessages.ts` 连同它依赖的 `client.ts`
 * 一起编译出来跑，那份配置里没有 `__DEV__` 的声明，用它编译不过。
 * `NODE_ENV` 是同一个文件里已经在用的写法（`EXPO_PUBLIC_*` 也是这么读的），
 * 生产构建里会被替换成 `'production'`，这段日志随之消失。
 */
export const IS_DEV = process.env.NODE_ENV !== 'production'

/** 本地等待时限。比服务端最长任务（30 分钟）短得多 —— 这是**单次请求**的上限 */
export const DEFAULT_TIMEOUT_MS = 60_000

/** 上传可能很慢（手机上行、几十 MB），单独放宽 */
export const UPLOAD_TIMEOUT_MS = 5 * 60_000

export class ApiError extends Error {
  /** 后端错误码，或 `CLIENT_CODES` 里的一个 */
  readonly code: string
  /** HTTP 状态码；网络层失败时为 0 */
  readonly status: number

  constructor(message: string, code: string, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }

  /** 是不是「连不上 / 超时 / 没配地址」这一类 —— 界面统一走「重试」入口 */
  get isNetworkish(): boolean {
    return (
      this.code === CLIENT_CODES.network ||
      this.code === CLIENT_CODES.timeout ||
      this.code === CLIENT_CODES.config
    )
  }
}

/** 把一段可能不是 JSON 的响应体变成 ApiError */
function errorFromBody(text: string, status: number): ApiError {
  try {
    const body = JSON.parse(text) as Partial<ApiErrorBody>
    const message = body?.error?.message
    if (typeof message === 'string' && message.trim() !== '') {
      return new ApiError(message, body.error?.code ?? 'error', status)
    }
  } catch {
    // 不是 JSON：下面按状态码兜底
  }
  if (status >= 500) {
    return new ApiError('服务器处理失败，请稍后重试', 'SERVER_ERROR', status)
  }
  if (status === 404) {
    return new ApiError('请求的资源不存在或已过期', 'NOT_FOUND', status)
  }
  return new ApiError(`请求失败（HTTP ${status}）`, 'error', status)
}

interface RequestOptions {
  method?: string
  body?: BodyInit | FormData
  signal?: AbortSignal
  timeoutMs?: number
}

/**
 * 发一次请求，成功返回 Response，失败一律抛 :class:`ApiError`。
 *
 * 超时与调用方自己的 `signal` 会**合流**：任一触发都中止这次请求。
 * 只监听一个的话，页面卸载时停不掉一个正在超时等待的请求。
 */
async function request(path: string, options: RequestOptions = {}): Promise<Response> {
  if (!isApiConfigured) {
    throw new ApiError(
      '没有配置服务器地址（EXPO_PUBLIC_API_BASE_URL）',
      CLIENT_CODES.config,
    )
  }

  const { method = 'GET', body, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options
  const controller = new AbortController()
  const onAbort = () => controller.abort()
  signal?.addEventListener('abort', onAbort)
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  // 注意**不要**自己设 multipart 的 Content-Type：boundary 由运行时生成，
  // 手写一个会把 boundary 写死，后端解不出来。JSON 反过来，必须自己声明。
  const headers: Record<string, string> =
    typeof body === 'string'
      ? { Accept: 'application/json', 'Content-Type': 'application/json' }
      : { Accept: 'application/json' }

  try {
    return await fetch(`${API_BASE_URL}${path}`, {
      method,
      body,
      signal: controller.signal,
      headers,
    })
  } catch (caught) {
    // 开发期把底层原因打出来。界面**只**显示下面那句归一后的话（规格 §三十三），
    // 但「连不上」底下可能是读不到文件、TLS、代理……不打出来就只能靠猜。
    if (IS_DEV) {
      const detail = caught as { name?: string; message?: string }
      console.warn(`[api] ${method} ${path} 底层失败：`, detail?.name, detail?.message)
    }
    // 调用方主动取消（页面卸载）与超时都会走到这里，靠 signal 区分：
    // 前者不该报错，原样抛出去由调用方忽略。
    if (signal?.aborted) throw caught
    if (controller.signal.aborted) {
      throw new ApiError('请求超时，请检查网络后重试', CLIENT_CODES.timeout)
    }
    throw new ApiError('无法连接服务器，请检查网络连接后重试', CLIENT_CODES.network)
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }
}

async function readJson<T>(response: Response): Promise<T> {
  const text = await response.text()
  if (!response.ok) throw errorFromBody(text, response.status)
  try {
    return JSON.parse(text) as T
  } catch {
    // 200 但内容不是 JSON：多半是被中间层（门户 / 代理）拦了
    throw new ApiError('服务器返回的内容无法解析', CLIENT_CODES.response, response.status)
  }
}

export async function getJson<T>(path: string, options: Omit<RequestOptions, 'method' | 'body'> = {}): Promise<T> {
  return readJson<T>(await request(path, { ...options, method: 'GET' }))
}

export async function postJson<T>(
  path: string,
  body?: unknown,
  options: Omit<RequestOptions, 'method' | 'body'> = {},
): Promise<T> {
  return readJson<T>(
    await request(path, {
      ...options,
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  )
}

/**
 * 提交一个 multipart 表单（上传文件走这条）。
 *
 * 这里只把 `FormData` 原样交给 `fetch`，**怎么拼 part 由调用方决定** ——
 * 拼法不是随意的，见 `services/api/conversion.ts` 顶部那段说明：
 * Expo SDK 57 起全局 `fetch` 换成了 `expo/fetch`，它的 FormData 编码器
 * 只认「字符串 / `Blob` / 带 `bytes()` 的对象」，React Native 那个流传很广的
 * `{uri, name, type}` 三件套会被它在**开 socket 之前**就抛掉
 * （`Unsupported FormDataPart implementation`，表现为「连不上服务器」）。
 *
 * 代价要说清楚：走 `bytes()` 意味着文件**会整个进一次内存**（RN 建不出带名字的
 * `Blob`，`expo/fetch` 又要把整个 body 拼成一个 `Uint8Array` 再发）。
 * 所以上传体积上限在服务端那一侧卡着，不在客户端做流式。
 */
export async function postForm<T>(
  path: string,
  form: FormData,
  options: Omit<RequestOptions, 'method' | 'body'> = {},
): Promise<T> {
  return readJson<T>(
    await request(path, { ...options, method: 'POST', body: form, timeoutMs: UPLOAD_TIMEOUT_MS }),
  )
}

/** 拼一个绝对地址（下载、`expo-sharing` 之类需要完整 URL 的地方用） */
export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`
}
