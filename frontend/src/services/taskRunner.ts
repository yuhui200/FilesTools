/**
 * 批量任务的「提交 + 轮询」流程（第四阶段 §5 / §6）。
 *
 * 批量接口不再一个请求等到处理完，而是：
 *
 *     提交（上传文件）→ 202 + group_id / status_url
 *     轮询 status_url → 直到整批 done 或 failed
 *
 * 这里把它写成一个普通的异步函数而不是 hook：上传图片、上传 PDF、
 * PDF 转图片三条路径的**界面状态各不相同**，但「提交完就轮询到结束」这件事
 * 完全一样。调用方各自用自己的 useState 接住 onSnapshot 回调即可。
 *
 * 轮询本身不产生错误提示：某个文件失败会出现在快照里，
 * 只有网络中断、任务过期（404）才会抛异常。
 */

import { ApiError, getConversionBatch, getTask } from './api'

import type { ConversionBatchStatus, TaskCreated, TaskSnapshot } from '@/types'

/** 轮询间隔。任务通常几百毫秒就有变化，太快只是白跑请求。 */
export const POLL_INTERVAL_MS = 500

export interface RunTaskOptions<R> {
  signal: AbortSignal
  /** 每拿到一份新快照就回调一次，用来驱动进度界面 */
  onSnapshot?: (snapshot: TaskSnapshot<R>) => void
  /** 拿到任务号时回调一次，此时还没有任何进度 */
  onSubmitted?: (created: TaskCreated) => void
  intervalMs?: number
}

/** 轮询到整批结束，返回最后一份快照（失败的任务同样返回，由调用方读 state）。 */
export async function runTask<R>(
  request: (signal: AbortSignal) => Promise<TaskCreated>,
  options: RunTaskOptions<R>,
): Promise<TaskSnapshot<R>> {
  const { signal, onSnapshot, onSubmitted, intervalMs = POLL_INTERVAL_MS } = options

  const created = await request(signal)
  if (signal.aborted) throw abortError()
  onSubmitted?.(created)

  for (;;) {
    const snapshot = await getTask<R>(created.status_url, signal)
    if (signal.aborted) throw abortError()

    onSnapshot?.(snapshot)
    if (snapshot.state === 'done' || snapshot.state === 'failed') return snapshot

    await wait(intervalMs, signal)
  }
}

// ----------------------------------------------------------------------
// 统一转换中心（第七阶段）
//
// 为什么不再包一层复用上面的 runTask：那一套的词表是第四阶段的
// ``state: waiting / processing / done / failed``，而转换中心用的是
// 规格 §八 的 ``status: queued / processing / cancelling / completed /
// failed / cancelled``。两者对「整批结束了没有」的判断完全不同，
// 硬套一层适配只会得到一份谁也不敢改的翻译代码。
//
// 有一点是共用的：**取消之后必须继续轮询**。队列里的取消是协作式的，
// 已经在跑的那一个还没停，界面要一直显示「正在取消」直到它真的结束（§十五）。
// ----------------------------------------------------------------------

/** 整批定下来的状态。到了这三个就不再轮询了 */
const SETTLED_STATES: ReadonlySet<string> = new Set(['completed', 'failed', 'cancelled'])

export interface PollConversionOptions {
  signal: AbortSignal
  /** 每拿到一份新快照就回调一次，用来驱动进度界面 */
  onSnapshot?: (snapshot: ConversionBatchStatus) => void
  intervalMs?: number
}

/**
 * 轮询一个转换批次直到它定下来，返回最后一份快照。
 *
 * 轮询本身不产生错误提示：单个文件失败会出现在快照里，
 * 只有网络中断、任务过期（404）才抛异常。
 */
export async function pollConversionBatch(
  statusUrl: string,
  options: PollConversionOptions,
): Promise<ConversionBatchStatus> {
  const { signal, onSnapshot, intervalMs = POLL_INTERVAL_MS } = options

  for (;;) {
    const snapshot = await getConversionBatch(statusUrl, signal)
    if (signal.aborted) throw abortError()

    onSnapshot?.(snapshot)
    if (SETTLED_STATES.has(snapshot.status)) return snapshot

    await wait(intervalMs, signal)
  }
}

function wait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError())
      return
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    function onAbort() {
      window.clearTimeout(timer)
      reject(abortError())
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

function abortError(): DOMException {
  return new DOMException('请求已取消', 'AbortError')
}

/** 取消导致的异常：调用方据此安静退出，不提示错误。 */
export function isAbort(caught: unknown): boolean {
  return caught instanceof DOMException && caught.name === 'AbortError'
}

/** 把任意异常转成一句可以直接展示的中文提示。 */
export function messageOf(caught: unknown, fallback = '处理失败，请重试'): string {
  if (caught instanceof ApiError || caught instanceof Error) return caught.message
  return fallback
}
