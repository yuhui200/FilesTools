/**
 * 能力查询（规格 §八 / §三十四）。
 *
 * **这是 App 里工具列表的唯一来源。** Mobile 本地没有、也不允许有
 * 一份能力矩阵：后端加一种格式，App 不重新发版就能看到它。
 */

import type { ConversionCapabilities } from '@/types'
import { getJson } from './client'

export interface CapabilityQuery {
  /** 只看以这种格式为输入的转换 */
  sourceType?: string
  /** 只看能转成这种格式的转换 */
  targetType?: string
  /** 只看某一类：image / document / pdf */
  category?: string
  /** 只看某一类操作：conversion（1→1）/ operation（工具） */
  operationType?: string
}

function queryString(query: CapabilityQuery): string {
  // URLSearchParams 在 React Native 里有（Hermes 的实现），不引 query-string
  const params = new URLSearchParams()
  if (query.sourceType) params.set('source_type', query.sourceType)
  if (query.targetType) params.set('target_type', query.targetType)
  if (query.category) params.set('category', query.category)
  if (query.operationType) params.set('operation_type', query.operationType)
  const text = params.toString()
  return text === '' ? '' : `?${text}`
}

/**
 * 拉取服务器当前真的能做的转换。
 *
 * 不带查询参数时返回全量 —— App 启动时就是这么拉的，之后按 category
 * 过滤在前端做（数据量只有几十条，每次都回服务器反而更慢、更容易失败）。
 */
export async function fetchCapabilities(
  query: CapabilityQuery = {},
  options: { signal?: AbortSignal } = {},
): Promise<ConversionCapabilities> {
  return getJson<ConversionCapabilities>(
    `/api/conversion/capabilities${queryString(query)}`,
    { signal: options.signal },
  )
}
