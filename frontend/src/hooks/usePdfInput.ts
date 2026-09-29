import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, downloadResult, getTask, uploadPdf } from '@/services/api'
import { isAbort, messageOf, runTask } from '@/services/taskRunner'
import type {
  PdfInputResponse,
  PdfResultResponse,
  TaskCreated,
  TaskSnapshot,
} from '@/types'
import { historyFromTasks, recordHistory } from '@/utils/history'
import { checkFile, type UploadRules } from '@/utils/validation'

import type { PdfStage } from './usePdfProgress'

/** 一份已选 / 已上传的 PDF */
export interface PdfEntry {
  file: File
  /** 上传成功后的回执，含服务端数出来的真实页数 */
  input: PdfInputResponse | null
}

/**
 * 「先上传 PDF，再对它做各种操作」的共用流程。
 *
 * PDF 转图片、拆分、删除页面、提取页面、压缩都走这条路：
 * 上传一次拿到 input_id（顺带拿到真实页数），用户看着页数调参数，
 * 点按钮才真正处理。好处是同一份 PDF 可以连着做几个操作，
 * 换参数重试时也不必重新上传 —— 服务端会保留原始文件 30 分钟。
 *
 * 第四阶段加入两件事：
 *
 *   1. **多份 PDF**：转图片支持一次选多份，上传是逐份进行的，
 *      上传进度是「已传完的份数 + 当前这份的进度」的真实合成值；
 *   2. **异步任务**：转图片改走任务队列（:func:`runBatch`），
 *      进度与每个文件的状态都来自服务端快照，不再是一个等到底的请求。
 */
export interface PdfInputTask {
  /** 第一份文件（单文件页面用） */
  file: File | null
  /** 全部已选文件 */
  files: File[]
  /** 第一份文件的回执（单文件页面用） */
  input: PdfInputResponse | null
  /** 全部已上传文件的回执 */
  inputs: PdfInputResponse[]
  /** 全部页面数之和 */
  totalPages: number
  stage: PdfStage
  /** 0-100，仅上传阶段有值 */
  uploadPercent: number
  busy: boolean
  error: string | null
  /** 错误码，用于按 §13 给出中文标题与建议 */
  errorCode: string | null
  result: PdfResultResponse | null
  /** 异步任务的状态快照（整批进度 + 每个文件的状态） */
  snapshot: TaskSnapshot<PdfResultResponse> | null
  /** 结果是否已被下载（服务器上已删除） */
  resultExpired: boolean
  downloading: boolean
  downloadError: string | null
  selectFile: (file: File) => void
  /** 选多份（转图片页用） */
  selectFiles: (files: File[]) => void
  /** Dropzone 的 onSelect 直接接这个：只取第一个 */
  selectFirst: (files: File[]) => void
  /** 移除一份已上传的 PDF */
  removeEntry: (index: number) => void
  /** 执行一次同步处理；operation 收到 input_id，返回结果 */
  run: (operation: (inputId: string) => Promise<PdfResultResponse>) => Promise<void>
  /** 提交一个后台任务并轮询到结束（转图片） */
  runBatch: (
    operation: (
      inputId: string,
      inputIds: string[],
      signal: AbortSignal,
    ) => Promise<TaskCreated>,
  ) => Promise<void>
  download: () => Promise<void>
  /** 换一份文件（结果与已上传的文件一起丢弃） */
  reset: () => void
  /** 保留已上传的文件，回到参数设置重新来一次 */
  backToSettings: () => void
  dismissError: () => void
  reportError: (message: string) => void
}

export interface PdfInputOptions {
  /** 一次最多上传几份 PDF；单文件页面保持默认的 1 */
  maxFiles?: number
  /** 操作类型的中文名，「最近处理」里显示（§12） */
  label?: string
}

export function usePdfInput(rules: UploadRules, options: PdfInputOptions = {}): PdfInputTask {
  const { maxFiles = 1, label = 'PDF 处理' } = options

  const [entries, setEntries] = useState<PdfEntry[]>([])
  const [stage, setStage] = useState<PdfStage>('idle')
  const [uploadPercent, setUploadPercent] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [errorCode, setErrorCode] = useState<string | null>(null)
  const [result, setResult] = useState<PdfResultResponse | null>(null)
  const [snapshot, setSnapshot] = useState<TaskSnapshot<PdfResultResponse> | null>(null)
  const [resultExpired, setResultExpired] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  // 任务号：下载之后重新查一次快照，如实反映「结果已被取走」
  const statusUrlRef = useRef<string | null>(null)

  useEffect(
    () => () => {
      abortRef.current?.abort()
    },
    [],
  )

  const dismissError = useCallback(() => {
    setError(null)
    setErrorCode(null)
  }, [])
  const reportError = useCallback((message: string) => {
    setErrorCode(null)
    setError(message)
  }, [])

  const clearProgress = useCallback(() => {
    setResult(null)
    setSnapshot(null)
    setResultExpired(false)
    setDownloadError(null)
    statusUrlRef.current = null
  }, [])

  /**
   * 逐份上传。
   *
   * 串行而不是并发：一次 50 份 PDF 同时上传会瞬间占满连接与磁盘写入，
   * 而这里的耗时几乎全在客户端上行带宽上，并行并不会更快。
   */
  const startUploads = useCallback(
    (chosen: File[]) => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      setEntries(chosen.map((file) => ({ file, input: null })))
      clearProgress()
      setError(null)
      setErrorCode(null)
      setUploadPercent(0)
      setStage('uploading')

      void (async () => {
        try {
          for (const [index, file] of chosen.entries()) {
            const response = await uploadPdf({
              file,
              signal: controller.signal,
              onUploadProgress: (ratio) => {
                if (controller.signal.aborted) return
                // 已传完的份数 + 当前这份的进度，才是真实的总进度
                setUploadPercent(Math.round(((index + ratio) / chosen.length) * 100))
              },
            })
            if (controller.signal.aborted) return
            setEntries((current) =>
              current.map((entry, position) =>
                position === index ? { ...entry, input: response } : entry,
              ),
            )
          }
          if (controller.signal.aborted) return
          setUploadPercent(100)
          setStage('ready')
        } catch (caught) {
          if (controller.signal.aborted || isAbort(caught)) return
          setStage('idle')
          setEntries([])
          setError(messageOf(caught, '上传失败，请检查网络后重试'))
        }
      })()
    },
    [clearProgress],
  )

  const selectFiles = useCallback(
    (chosen: File[]) => {
      const accepted: File[] = []
      let rejection: string | null = null
      for (const file of chosen) {
        const message = checkFile(file, rules, '文件')
        if (message) rejection ??= message
        else accepted.push(file)
      }

      if (accepted.length === 0) {
        if (rejection) reportError(rejection)
        return
      }
      if (accepted.length > maxFiles) {
        reportError(`一次最多处理 ${maxFiles} 个文件，多出的已忽略。`)
      }
      startUploads(accepted.slice(0, maxFiles))
    },
    [maxFiles, reportError, rules, startUploads],
  )

  const selectFile = useCallback(
    (chosen: File) => {
      selectFiles([chosen])
    },
    [selectFiles],
  )

  const removeEntry = useCallback(
    (index: number) => {
      abortRef.current?.abort()
      setEntries((current) => current.filter((_, position) => position !== index))
      clearProgress()
      setError(null)
      setErrorCode(null)
      setStage('idle')
    },
    [clearProgress],
  )

  const run = useCallback(
    async (operation: (inputId: string) => Promise<PdfResultResponse>) => {
      const first = entries[0]?.input
      if (!first) {
        setError('文件尚未上传完成，请稍候')
        return
      }

      const controller = new AbortController()
      abortRef.current = controller

      setError(null)
      setErrorCode(null)
      setDownloadError(null)
      setStage('working')

      try {
        const response = await operation(first.input_id)
        if (controller.signal.aborted) return
        setResult(response)
        setResultExpired(false)
        setStage('done')
        // 同步处理的 PDF 工具没有逐文件状态，按上传的文件记一笔（§12）
        recordHistory(
          entries.flatMap((entry) =>
            entry.input
              ? [{ filename: entry.file.name, tool: label, ok: true, size: entry.file.size }]
              : [],
          ),
        )
      } catch (caught) {
        if (controller.signal.aborted || isAbort(caught)) return

        setError(messageOf(caught, '处理失败，请稍后重试'))
        setErrorCode(caught instanceof ApiError ? caught.code : null)

        // 输入文件过期时不能再留在「等待处理」——用户点了也没用，直接退回上传
        if (caught instanceof ApiError && caught.status === 404) {
          setEntries([])
          setStage('idle')
        } else {
          setStage('ready')
        }
      }
    },
    [entries, label],
  )

  const runBatch = useCallback(
    async (
      operation: (
        inputId: string,
        inputIds: string[],
        signal: AbortSignal,
      ) => Promise<TaskCreated>,
    ) => {
      const ready = entries.filter((entry) => entry.input !== null)
      const inputIds = ready.map((entry) => entry.input!.input_id)
      if (inputIds.length === 0) {
        setError('文件尚未上传完成，请稍候')
        return
      }

      const controller = new AbortController()
      abortRef.current = controller

      setError(null)
      setErrorCode(null)
      setDownloadError(null)
      setResult(null)
      setSnapshot(null)
      setResultExpired(false)
      setStage('working')

      try {
        const final = await runTask<PdfResultResponse>(
          (signal) => operation(inputIds[0]!, inputIds, signal),
          {
            signal: controller.signal,
            onSubmitted: (created) => {
              statusUrlRef.current = created.status_url
            },
            onSnapshot: setSnapshot,
          },
        )
        if (controller.signal.aborted) return

        recordHistory(historyFromTasks(final))

        if (final.state === 'done' && final.result) {
          setResult(final.result)
          setStage('done')
          return
        }

        setError(final.error?.message ?? '处理失败，请重试')
        setErrorCode(final.error?.code ?? 'PROCESSING_FAILED')
        setStage('ready')
      } catch (caught) {
        if (controller.signal.aborted || isAbort(caught)) return
        setError(messageOf(caught, '处理失败，请稍后重试'))
        setErrorCode(caught instanceof ApiError ? caught.code : null)
        setStage('ready')
      }
    },
    [entries],
  )

  const download = useCallback(async () => {
    if (!result) return
    setDownloading(true)
    setDownloadError(null)
    try {
      await downloadResult(result.download_url, result.filename)
      // 结果文件是一次性的：下载完就标记，页面不再留一个点不动的下载按钮
      setResultExpired(true)

      const statusUrl = statusUrlRef.current
      if (statusUrl) {
        try {
          setSnapshot(await getTask<PdfResultResponse>(statusUrl))
        } catch {
          // 查询失败不影响这次下载已经成功的事实
        }
      }
    } catch (caught) {
      setDownloadError(messageOf(caught, '下载失败，请重试'))
    } finally {
      setDownloading(false)
    }
  }, [result])

  const reset = useCallback(() => {
    abortRef.current?.abort()
    setEntries([])
    clearProgress()
    setError(null)
    setErrorCode(null)
    setUploadPercent(0)
    setStage('idle')
  }, [clearProgress])

  const backToSettings = useCallback(() => {
    clearProgress()
    setError(null)
    setErrorCode(null)
    setStage('ready')
  }, [clearProgress])

  const inputs = entries.flatMap((entry) => (entry.input ? [entry.input] : []))
  const totalPages = inputs.reduce((sum, item) => sum + item.page_count, 0)

  return {
    file: entries[0]?.file ?? null,
    files: entries.map((entry) => entry.file),
    input: entries[0]?.input ?? null,
    inputs,
    totalPages,
    stage,
    uploadPercent,
    busy: stage === 'uploading' || stage === 'working',
    error,
    errorCode,
    result,
    snapshot,
    resultExpired,
    downloading,
    downloadError,
    selectFile,
    selectFiles,
    selectFirst: (files: File[]) => {
      const [first] = files
      if (first) selectFile(first)
    },
    removeEntry,
    run,
    runBatch,
    download,
    reset,
    backToSettings,
    dismissError,
    reportError,
  }
}
