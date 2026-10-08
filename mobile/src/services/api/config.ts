/**
 * 服务器配置（`GET /api/config`）。
 *
 * App 只取三样东西，每一样都有明确的理由，其余四十多个键一律不碰 ——
 * 那些是给 Web 端的表单用的，Mobile 的参数面板由能力的 `options_schema`
 * 驱动，不需要第二份限制配置。
 *
 * 1. `txt_fonts`：字体是服务端**运行时探测本机**得到的，客户端编不出来。
 *    `options_schema` 里 `dynamic: "fonts"` 的那一项靠它填。
 * 2. `error_codes`：「我的」页拿它自检有没有漏配文案 —— 服务端加了一个新
 *    错误码而 App 没跟上时，那两个列表就对不上，界面会如实报出来。
 * 3. `task_ttl_seconds` / `file_ttl_seconds`：结果能留多久。客户端不靠它
 *    做判断（服务端说了算），只用来说人话（「结果保留 30 分钟」）。
 */

import { getJson } from './client'

export interface ConfigFont {
  value: string
  label: string
}

export interface ServerConfig {
  /** 这台服务器真的装了的字体。没装的不会出现在这里 */
  txt_fonts: ConfigFont[]
  /** 服务端可能返回的全部错误码，供客户端自检文案 */
  error_codes: string[]
  /** 任务记录保留秒数 */
  task_ttl_seconds: number
  /** 结果文件保留秒数 */
  file_ttl_seconds: number
}

/**
 * 拉配置。
 *
 * **不求全**：这个接口的响应体很大且大部分字段我们不用，所以只挑出上面
 * 四个键，其余原样丢掉。用 `Partial` 读、缺键时给安全默认值 ——
 * 服务端将来删掉某个字段时，App 不该整页崩掉。
 */
export async function fetchConfig(options: { signal?: AbortSignal } = {}): Promise<ServerConfig> {
  const raw = await getJson<Partial<ServerConfig>>('/api/config', { signal: options.signal })
  return {
    txt_fonts: Array.isArray(raw.txt_fonts) ? raw.txt_fonts : [],
    error_codes: Array.isArray(raw.error_codes) ? raw.error_codes : [],
    task_ttl_seconds: typeof raw.task_ttl_seconds === 'number' ? raw.task_ttl_seconds : 0,
    file_ttl_seconds: typeof raw.file_ttl_seconds === 'number' ? raw.file_ttl_seconds : 0,
  }
}
