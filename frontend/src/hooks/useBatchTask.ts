import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { ApiError, downloadResult, getTask } from '@/services/api'
import { isAbort, messageOf, runTask } from '@/services/taskRunner'
import type { BatchResponse, TaskCreated, TaskSnapshot } from '@/types'
import { toBytes } from '@/utils/format'
import { historyFromTasks, recordHistory } from '@/utils/history'
import { checkFiles, type UploadRules } from '@/utils/validation'

/** 目标大小选项，三个图片工具共用 */
export type SizeOption = 'none' | '500kb' | '1mb' | '2mb' | '5mb' | 'custom'

/** 目标大小的固定档位（字节）。统一转换中心也用这套档位，所以导出出去 */
export const SIZE_BYTES: Record<'500kb' | '1mb' | '2mb' | '5mb', number> = {
  '500kb': 500 * 1024,
  '1mb': 1024 * 1024,
  '2mb': 2 * 1024 * 1024,
  '5mb': 5 * 1024 * 1024,
}

/** 质量选项：三档预设 + 自定义数值 */
export type QualityChoice = 'high' | 'balanced' | 'strong' | 'custom'

export const PRESET_QUALITY: Record<'high' | 'balanced' | 'strong', number> = {
  high: 92,
  balanced: 80,
  strong: 60,
}

/**
 * 任务状态。
 *
 *   idle       文件都还没提交
 *   uploading  正在把文件传给服务器（有真实的上传百分比）
 *   processing 服务器已经建好任务，正在逐个文件处理（进度来自轮询）
 *   done       整批完成，可以下载
 *   failed     整批失败（一个文件都没成功），每个失败文件的文件名与原因仍可查看
 */
export type TaskStatus = 'idle' | 'uploading' | 'processing' | 'done' | 'failed'

export interface BatchTask {
  files: File[]
  status: TaskStatus
  uploadPercent: number
  /** 服务端的任务快照：进度、每个文件的状态、最终结果 */
  snapshot: TaskSnapshot<BatchResponse> | null
  result: BatchResponse | null
  error: string | null
  /** 错误码，用于按 §13 给出中文标题与建议 */
  errorCode: string | null
  downloading: boolean
  downloadError: string | null
  /** 结果文件是否已被下载或过期（服务器上已删除） */
  resultExpired: boolean
  /** 目标大小解析结果（字节），null 表示不限制 */
  targetBytes: number | null
  /** 目标大小输入有误时的提示 */
  targetError: string | null
  /** 目标大小当前选中的档位 */
  targetOption: SizeOption
  /** 自定义目标大小的输入值（字符串，便于输入框受控） */
  customTargetValue: string
  customTargetUnit: 'KB' | 'MB'
  /** 质量当前选中的档位 */
  qualityChoice: QualityChoice
  /** 质量数值，始终是一个 1-100 的整数 */
  qualityValue: number
  resolvedFiles: File[]
  addFiles: (incoming: File[]) => void
  removeFile: (index: number) => void
  clearFiles: () => void
  setTargetOption: (option: SizeOption) => void
  setCustomTarget: (value: string, unit: 'KB' | 'MB') => void
  setQualityValue: (value: number) => void
  setQualityChoice: (choice: QualityChoice) => void
  start: () => void
  download: () => Promise<void>
  reset: () => void
  dismissError: () => void
  /** 展示一条错误提示（用于 Dropzone 的本地校验结果） */
  reportError: (message: string) => void
}

interface Options {
  /** 允许的最大文件数 */
  maxFiles: number
  /** 单个文件大小上限 */
  maxBytes: number
  /** 整批大小上限 */
  maxTotalBytes: number
  rules: UploadRules
  /** 服务端默认质量，用于「平衡」档 */
  defaultQuality: number
  /** 是否提供「自定义质量」档；图片压缩只用三档预设，设为 false */
  allowCustomQuality?: boolean
  /** 实际的上传动作，三个页面各自注入；返回任务号，进度由本 hook 轮询 */
  submit: (
    files: File[],
    params: {
      /** 选中的质量档位，压缩接口按档位名传参，转换 / 尺寸调整按数值传参 */
      qualityChoice: QualityChoice
      qualityValue: number
      targetBytes: number | null
      onUploadProgress: (ratio: number) => void
      signal: AbortSignal
    },
  ) => Promise<TaskCreated>
}

/**
 * 图片批量任务的共用状态机（压缩 / 格式转换 / 尺寸调整）。
 *
 * 三个页面除了参数面板不同，其余流程完全一致，因此共用这一个 hook：
 * 选文件 → 本地校验 → 上传 → **轮询任务状态** → 结果 / 失败 → 下载 → 重置。
 *
 * 进度不再由前端估算：上传阶段用 XHR 的真实上传百分比，
 * 上传完成后一切以服务端快照为准（§3）。
 */
export function useBatchTask(options: Options): BatchTask {
  const {
    maxFiles,
    maxBytes,
    maxTotalBytes,
    rules,
    defaultQuality,
    allowCustomQuality = true,
    submit,
  } = options

  const [files, setFiles] = useState<File[]>([])
  const [status, setStatus] = useState<TaskStatus>('idle')
  const [uploadPercent, setUploadPercent] = useState(0)
  const [snapshot, setSnapshot] = useState<TaskSnapshot<BatchResponse> | null>(null)
  const [result, setResult] = useState<BatchResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [errorCode, setErrorCode] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)

  const [targetOption, setTargetOptionState] = useState<SizeOption>('none')
  const [customValue, setCustomValue] = useState('1')
  const [customUnit, setCustomUnitState] = useState<'KB' | 'MB'>('MB')
  const [qualityChoice, setQualityChoiceState] = useState<QualityChoice>('balanced')
  const [customQuality, setCustomQuality] = useState(defaultQuality)

  const abortRef = useRef<AbortController | null>(null)
  // 任务号：下载之后要重新查一次快照，才能如实反映「结果已被取走」
  const statusUrlRef = useRef<string | null>(null)
  const busy = status === 'uploading' || status === 'processing'

  // 卸载时取消进行中的请求与轮询
  useEffect(
    () => () => {
      abortRef.current?.abort()
    },
    [],
  )

  const { targetBytes, targetError } = useMemo(() => {
    if (targetOption === 'none') return { targetBytes: null, targetError: null }
    if (targetOption === 'custom') {
      const bytes = toBytes(Number(customValue), customUnit)
      if (bytes === null) return { targetBytes: null, targetError: '请输入有效的目标大小' }
      if (bytes > maxBytes) return { targetBytes: null, targetError: '目标大小超出单个文件的大小上限' }
      return { targetBytes: bytes, targetError: null }
    }
    return { targetBytes: SIZE_BYTES[targetOption], targetError: null }
  }, [targetOption, customValue, customUnit, maxBytes])

  const qualityValue = useMemo(() => {
    if (allowCustomQuality && qualityChoice === 'custom') return clampQuality(customQuality)
    // 不提供自定义档时，档位一定是三档预设之一
    return qualityChoice === 'custom' ? PRESET_QUALITY.balanced : PRESET_QUALITY[qualityChoice]
  }, [allowCustomQuality, qualityChoice, customQuality])

  /** 回到「还没开始处理」的状态，同时清掉上一轮的任务痕迹 */
  const clearProgress = useCallback(() => {
    setSnapshot(null)
    setResult(null)
    setErrorCode(null)
    setDownloadError(null)
    setUploadPercent(0)
    statusUrlRef.current = null
  }, [])

  const addFiles = useCallback(
    (incoming: File[]) => {
      const room = maxFiles - files.length
      if (room <= 0) {
        setErrorCode(null)
        setError(`一次最多处理 ${maxFiles} 个文件，请先移除一些再添加。`)
        return
      }

      const { accepted, error: rejection } = checkFiles(incoming, rules)
      const next = [...files, ...accepted].slice(0, maxFiles)
      const dropped = accepted.length - (next.length - files.length)

      setErrorCode(null)
      if (rejection) {
        setError(rejection)
      } else if (dropped > 0) {
        setError(`一次最多处理 ${maxFiles} 个文件，多出的 ${dropped} 个已忽略。`)
      } else {
        setError(null)
      }

      const total = next.reduce((sum, file) => sum + file.size, 0)
      if (total > maxTotalBytes) {
        setError(
          `一次最多处理 ${Math.round(maxTotalBytes / (1024 * 1024))} MB 的文件，请分批上传。`,
        )
      }

      setFiles(next)
      clearProgress()
      setStatus('idle')
    },
    [files, maxFiles, maxTotalBytes, rules, clearProgress],
  )

  const removeFile = useCallback(
    (index: number) => {
      setFiles((current) => current.filter((_, position) => position !== index))
      clearProgress()
      setStatus('idle')
    },
    [clearProgress],
  )

  const clearFiles = useCallback(() => {
    abortRef.current?.abort()
    setFiles([])
    setError(null)
    clearProgress()
    setStatus('idle')
  }, [clearProgress])

  const start = useCallback(() => {
    if (busy || files.length === 0 || targetError) return

    const controller = new AbortController()
    abortRef.current = controller

    setError(null)
    setErrorCode(null)
    setDownloadError(null)
    setResult(null)
    setSnapshot(null)
    setUploadPercent(0)
    setStatus('uploading')

    runTask<BatchResponse>(
      (signal) =>
        submit(files, {
          qualityChoice,
          qualityValue,
          targetBytes,
          onUploadProgress: (ratio) => {
            setUploadPercent(ratio * 100)
            // 上传一结束就切到「处理中」：后面的进度全部来自服务端快照
            if (ratio >= 1) setStatus('processing')
          },
          signal,
        }),
      {
        signal: controller.signal,
        onSubmitted: (created) => {
          statusUrlRef.current = created.status_url
          setStatus('processing')
        },
        onSnapshot: setSnapshot,
      },
    )
      .then((final) => {
        if (controller.signal.aborted) return

        // 整批结束就记一笔「最近处理」（§12），成功与失败的文件都记，
        // 文件名与结果面板用的是同一份服务端快照
        recordHistory(historyFromTasks(final))

        if (final.state === 'done' && final.result) {
          setResult(final.result)
          setStatus('done')
          return
        }

        // 整批失败：错误码 + 每个文件的原因都已经在快照里，
        // 页面会把「哪些文件失败、为什么」如实展示出来（§4）
        setError(final.error?.message ?? '处理失败，请重试')
        setErrorCode(final.error?.code ?? 'PROCESSING_FAILED')
        setStatus('failed')
      })
      .catch((caught: unknown) => {
        if (isAbort(caught)) return
        setError(messageOf(caught))
        setErrorCode(caught instanceof ApiError ? caught.code : 'PROCESSING_FAILED')
        setStatus('idle')
      })
      .finally(() => {
        abortRef.current = null
      })
  }, [busy, files, qualityChoice, qualityValue, submit, targetBytes, targetError])

  const download = useCallback(async () => {
    if (!result?.download_url) return
    setDownloading(true)
    setDownloadError(null)
    try {
      const filename = result.archived
        ? (result.archive_filename ?? 'filetools.zip')
        : (result.items[0]?.result.filename ?? 'result')
      await downloadResult(result.download_url, filename)

      // 结果文件是一次性的：下载完再查一次，页面就能如实显示「已取走」
      const statusUrl = statusUrlRef.current
      if (statusUrl) {
        try {
          setSnapshot(await getTask<BatchResponse>(statusUrl))
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
    clearProgress()
    setStatus('idle')
  }, [clearProgress])

  return {
    files,
    status,
    uploadPercent,
    snapshot,
    result,
    error,
    errorCode,
    downloading,
    downloadError,
    resultExpired: snapshot?.result?.expired === true,
    targetBytes,
    targetError,
    targetOption,
    customTargetValue: customValue,
    customTargetUnit: customUnit,
    qualityChoice,
    qualityValue,
    resolvedFiles: files,
    addFiles,
    removeFile,
    clearFiles,
    setTargetOption: setTargetOptionState,
    setCustomTarget: (value, unit) => {
      setCustomValue(value)
      setCustomUnitState(unit)
    },
    setQualityValue: setCustomQuality,
    setQualityChoice: setQualityChoiceState,
    start,
    download,
    reset,
    dismissError: () => {
      setError(null)
      setErrorCode(null)
    },
    reportError: (message: string) => {
      setErrorCode(null)
      setError(message)
    },
  }
}

function clampQuality(value: number): number {
  if (!Number.isFinite(value)) return 80
  return Math.min(100, Math.max(1, Math.round(value)))
}
