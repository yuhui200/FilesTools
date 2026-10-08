/**
 * 批次轮询（规格 §二十一）。
 *
 * 提交任务后服务端返回 202，真正的转换在后台队列里跑，界面只能问。
 * 这个 hook 就是那个「问」的循环，它只有四个职责，每一个都对应一条
 * 移动端特有的约束：
 *
 * 1. **有上限**。`MAX_POLL_MS` 后停手，不死循环。移动端尤其重要 ——
 *    用户把 App 揣兜里，一个不设上限的定时器会一路把电和流量吃光。
 * 2. **停下来就是真停**。整批进终态、组件卸载、批次号变了，都会
 *    `clearTimeout` + `abort()`。**卸载后还有请求在飞**是这个功能最典型的
 *    bug：回来时 setState 到已卸载的组件上，React 会警告，用户会看到
 *    一个永远转圈的旧批次。
 * 3. **单次失败不放弃**。手机网络本来就断断续续，一次请求失败就报错、
 *    让用户看一屏「无法连接服务器」，是把产品做坏。连续失败
 *    `MAX_CONSECUTIVE_FAILURES` 次才认输 —— 而且认输时如实说出是哪一次
 *    开始失败的。
 * 4. **轻量轮询 + 收尾拉全量**。轮询只打 `/progress`（几个整数），
 *    到终态才打一次完整的 `/tasks/{id}` 取每项的明细与下载地址（§二十八）。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api/client'
import { POLL_TIMEOUT_MS, cancelBatch, fetchBatch, fetchProgress } from '@/services/api/tasks'
import type { ConversionBatchState, ConversionBatchStatus, ConversionProgress } from '@/types'

/**
 * 轮询间隔。
 *
 * 500ms（Web 端那套）在手机上太急：一次 4G 往返就可能超过它，结果是
 * 请求排着队发。800ms 兼顾「进度看起来是连续的」与「不过度耗电」。
 * 第一轮**不等待**，立刻发 —— 提交完到第一个状态之间不该有 800ms 的空白。
 */
export const POLL_INTERVAL_MS = 800

/**
 * 最长轮询多久。
 *
 * 与服务端任务记录的存活时间（30 分钟）取齐：再往后 `/tasks/{id}` 必然
 * 404，继续问下去只会得到一个「任务已过期」的错误。
 */
export const MAX_POLL_MS = 30 * 60_000

/** 连续失败几次就认输。中间任何一次成功都会把这个计数清零 */
export const MAX_CONSECUTIVE_FAILURES = 3

/** 整批的终态。到这几个状态就不用再问了 */
const SETTLED_STATES: readonly ConversionBatchState[] = ['completed', 'failed', 'cancelled']

export interface BatchPolling {
  /** 轻量计数，轮询过程中一直在更新 */
  counts: ConversionProgress | null
  /** 完整体（含每项明细与下载地址）。到终态后一定会有；中途 `refresh()` 也能拿到 */
  snapshot: ConversionBatchStatus | null
  /** **轮询本身**出的错。注意与「任务失败」是两回事：任务是失败态时这里仍是 null */
  error: ApiError | null
  /** 还在问 */
  polling: boolean
  /** 轮到自己停手了（超时），不代表任务失败 —— 服务端可能还在跑 */
  gaveUp: boolean
  /** 立刻拉一次完整体。用户下拉刷新、或想看看现在的明细时用 */
  refresh: () => Promise<void>
  /**
   * 重新开始轮询。
   *
   * 循环到终态就停了，而「重试一个失败项」会让整批重新跑起来 ——
   * 那时必须显式重启，否则界面会停在失败态，用户以为重试没生效。
   * 与切批次不同，重启**不清空**已有的计数与快照，避免闪一下白屏。
   */
  restart: () => void
  /** 请求取消整批。它是**协作式**的，界面要继续显示「正在取消」而不是「已取消」 */
  cancel: () => Promise<void>
}

export interface BatchPollingOptions {
  /** 轮询哪个批次。传 null 表示没有在跑的任务（hook 会完全静默） */
  batchId: string | null
  /** 整批进终态时调一次（更新历史、播提示音之类）。可以做异步的事 */
  onSettled?: (snapshot: ConversionBatchStatus) => void
  /** 每拿到一次状态调一次，含轮询中的轻量计数 */
  onUpdate?: (snapshot: ConversionBatchStatus | null, counts: ConversionProgress) => void
}

export function useBatchPolling(options: BatchPollingOptions): BatchPolling {
  const { batchId } = options

  const [counts, setCounts] = useState<ConversionProgress | null>(null)
  const [snapshot, setSnapshot] = useState<ConversionBatchStatus | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [polling, setPolling] = useState(false)
  const [gaveUp, setGaveUp] = useState(false)
  /** 每次 `restart()` +1，用来自动重跑下面的 effect */
  const [runId, setRunId] = useState(0)

  // 回调放在 ref 里：调用方每次渲染新建一个箭头函数是常态，
  // 直接进依赖数组会让整个轮询在每次渲染时重启。
  const callbacks = useRef({ onSettled: options.onSettled, onUpdate: options.onUpdate })
  callbacks.current = { onSettled: options.onSettled, onUpdate: options.onUpdate }

  // 循环靠这个 ref 停：清理函数里只置一个标志 + abort，不等 promise
  const runRef = useRef<{ stopped: boolean; controller: AbortController } | null>(null)
  /** 上一次轮询的是哪个批次。只有换了批次才清空界面状态 */
  const lastBatch = useRef<string | null>(null)

  useEffect(() => {
    if (!batchId) {
      lastBatch.current = null
      setCounts(null)
      setSnapshot(null)
      setError(null)
      setPolling(false)
      setGaveUp(false)
      return
    }

    const run = { stopped: false, controller: new AbortController() }
    runRef.current = run
    const deadline = Date.now() + MAX_POLL_MS
    let failures = 0

    // 重启（重试之后）**不清空**：不清的话界面会先闪一下「正在获取任务状态」，
    // 而其实上一秒才刚拿到过状态
    if (lastBatch.current !== batchId) {
      lastBatch.current = batchId
      setCounts(null)
      setSnapshot(null)
    }
    setError(null)
    setGaveUp(false)
    setPolling(true)

    const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

    const finish = () => {
      if (run.stopped) return
      run.stopped = true
      setPolling(false)
    }

    /** 收尾：把完整体拉回来（明细、失败原因、下载地址都在里面） */
    const loadFull = async (): Promise<ConversionBatchStatus | null> => {
      try {
        const full = await fetchBatch(batchId, {
          signal: run.controller.signal,
          timeoutMs: POLL_TIMEOUT_MS,
        })
        if (run.stopped) return null
        setSnapshot(full)
        return full
      } catch {
        // 收尾失败不覆盖已有的计数：界面还能显示「已完成 N 个」，
        // 让用户点一次刷新重试，比整页变成错误页好
        return null
      }
    }

    const loop = async (): Promise<void> => {
      while (!run.stopped) {
        if (Date.now() >= deadline) {
          setGaveUp(true)
          finish()
          return
        }

        let progress: ConversionProgress
        try {
          progress = await fetchProgress(batchId, {
            signal: run.controller.signal,
            timeoutMs: POLL_TIMEOUT_MS,
          })
        } catch (caught) {
          if (run.stopped) return
          failures += 1
          if (failures >= MAX_CONSECUTIVE_FAILURES) {
            // 到这里才认输。CancelError（卸载时 abort）不会走到这 —— 上面已经 return
            setError(
              caught instanceof ApiError
                ? caught
                : new ApiError('无法获取任务状态', 'network_error'),
            )
            finish()
            return
          }
          await sleep(POLL_INTERVAL_MS)
          continue
        }

        if (run.stopped) return
        failures = 0
        setCounts(progress)
        setError(null)

        const settled = SETTLED_STATES.includes(progress.status)
        if (settled) {
          const full = await loadFull()
          if (run.stopped) return
          if (full) callbacks.current.onSettled?.(full)
          finish()
          return
        }

        callbacks.current.onUpdate?.(null, progress)
        await sleep(POLL_INTERVAL_MS)
      }
    }

    void loop()

    return () => {
      // 顺序要紧：先置标志（循环下一轮就会退出），再 abort 掉在飞的请求。
      // 反过来写的话，abort 触发的 rejection 会先被当成一次「失败」记上一笔。
      run.stopped = true
      run.controller.abort()
      if (runRef.current === run) runRef.current = null
    }
  }, [batchId, runId])

  const refresh = useCallback(async (): Promise<void> => {
    if (!batchId) return
    try {
      const full = await fetchBatch(batchId, { timeoutMs: POLL_TIMEOUT_MS })
      setSnapshot(full)
      setError(null)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError('无法获取任务状态', 'network_error'))
    }
  }, [batchId])

  const restart = useCallback((): void => {
    setRunId((previous) => previous + 1)
  }, [])

  const cancel = useCallback(async (): Promise<void> => {
    if (!batchId) return
    try {
      const full = await cancelBatch(batchId, { timeoutMs: POLL_TIMEOUT_MS })
      setSnapshot(full)
      setCounts((previous) => (previous ? { ...previous, cancelling: true } : previous))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError('取消失败', 'network_error'))
    }
  }, [batchId])

  return { counts, snapshot, error, polling, gaveUp, refresh, restart, cancel }
}
