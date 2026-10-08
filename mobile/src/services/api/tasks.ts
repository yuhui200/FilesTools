/**
 * 任务状态查询与操作（规格 §十五 / §十六）。
 *
 * 全部落在既有的 `/api/conversion/tasks/...` 上。
 *
 * 一条容易踩的约定：**轮询永远返回 200**，即使这一批里有的文件失败了 ——
 * 某一项失败是任务的结果，不是这次查询的错误。所以界面不能把
 * 「批里有失败项」当成请求异常来处理，两者要分开显示。
 */

import type { ConversionBatchStatus, ConversionProgress } from '@/types'
import { getJson, postJson } from './client'

/** 轮询用的等待时限。比默认的 60 秒短得多 —— 一次轮询卡住一分钟，界面就假死了 */
export const POLL_TIMEOUT_MS = 15_000

interface QueryOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

/** 整批状态 + 每一项的明细、进度、失败原因与下载地址 */
export async function fetchBatch(
  batchId: string,
  options: QueryOptions = {},
): Promise<ConversionBatchStatus> {
  return getJson<ConversionBatchStatus>(
    `/api/conversion/tasks/${encodeURIComponent(batchId)}`,
    { signal: options.signal, timeoutMs: options.timeoutMs },
  )
}

/** 只回计数的轻量进度，高频轮询用（§二十八：轮询少传数据） */
export async function fetchProgress(
  batchId: string,
  options: QueryOptions = {},
): Promise<ConversionProgress> {
  return getJson<ConversionProgress>(
    `/api/conversion/tasks/${encodeURIComponent(batchId)}/progress`,
    { signal: options.signal, timeoutMs: options.timeoutMs },
  )
}

/**
 * 请求取消整批。
 *
 * **协作式，不是立刻停**：还在排队的会马上取消，**已经在转换的那个停不下来**
 * （重活在线程池里）。它的状态会如实变成 `cancelling`，等跑完才变 `cancelled` ——
 * 界面据此显示「正在取消」，不假装已经停了。
 */
export async function cancelBatch(
  batchId: string,
  options: QueryOptions = {},
): Promise<ConversionBatchStatus> {
  return postJson<ConversionBatchStatus>(
    `/api/conversion/tasks/${encodeURIComponent(batchId)}/cancel`,
    undefined,
    { signal: options.signal, timeoutMs: options.timeoutMs },
  )
}

/**
 * 重试单个失败项。
 *
 * `taskId` 是 `{batch_id}:{index}`，冒号在 URL 路径段里是合法字符，
 * **不要**整体 encode（编码后冒号变 %3A，服务端按最后一个冒号切分就找不到批次）。
 */
export async function retryTask(
  taskId: string,
  options: QueryOptions = {},
): Promise<ConversionBatchStatus> {
  return postJson<ConversionBatchStatus>(
    `/api/conversion/tasks/${taskId}/retry`,
    undefined,
    { signal: options.signal, timeoutMs: options.timeoutMs },
  )
}
