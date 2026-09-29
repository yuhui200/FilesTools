import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, downloadResult } from '@/services/api'
import type { PdfResultResponse } from '@/types'
import { recordHistory } from '@/utils/history'
import { checkFiles, type UploadRules } from '@/utils/validation'

import type { PdfStage } from './usePdfProgress'

/**
 * 「多个文件一次请求」的共用流程：图片转 PDF、PDF 合并。
 *
 * 和图片批量处理（useBatchTask）是同一个套路，区别有两点：
 * 1. 文件顺序就是页面顺序，所以多了 moveFile —— 用户可以拖动、上移、下移；
 * 2. 结果形态是 PDF 文件的统一响应，不是逐文件的结果列表。
 *
 * 泛型参数是这次请求的响应类型。默认就是 ``PdfResultResponse``，
 * 所以原有的调用方一个字都不用改；PDF 转 Word 传自己的
 * ``DocumentResultResponse``（在通用响应上多了类型 / 识别方式等字段），
 * 结果面板就能直接用上那些字段，而不是靠断言把类型骗过去。
 */
export interface PdfMultiTask<T extends PdfResultResponse = PdfResultResponse> {
  files: File[]
  stage: PdfStage
  uploadPercent: number
  busy: boolean
  error: string | null
  /** 错误码，用于按 §13 给出中文标题与建议；本地校验失败时为 null */
  errorCode: string | null
  result: T | null
  /** 结果是否已被下载（服务器上已删除，下载令牌只能用一次） */
  resultExpired: boolean
  downloading: boolean
  downloadError: string | null
  addFiles: (incoming: File[]) => void
  removeFile: (index: number) => void
  moveFile: (from: number, to: number) => void
  clearFiles: () => void
  start: () => void
  download: () => Promise<void>
  /** 保留已选文件，回到参数设置重新来一次 */
  reset: () => void
  dismissError: () => void
  reportError: (message: string) => void
}

/** 提交时由调用方接手的部分：上传进度回调与取消信号 */
export interface PdfSubmitParams {
  onUploadProgress: (ratio: number) => void
  signal: AbortSignal
}

/** 各页面自己组装请求（多带的表单字段由页面决定，hook 不关心） */
export type PdfSubmit<T extends PdfResultResponse = PdfResultResponse> = (
  files: File[],
  params: PdfSubmitParams,
) => Promise<T>

interface Options<T extends PdfResultResponse> {
  maxFiles: number
  maxTotalBytes: number
  rules: UploadRules
  /** 校验失败时的名词，例如「PDF 文件」 */
  kind: string
  /** 操作类型的中文名，「最近处理」里显示（§12） */
  label: string
  submit: PdfSubmit<T>
}

export function usePdfMultiTask<T extends PdfResultResponse = PdfResultResponse>(
  options: Options<T>,
): PdfMultiTask<T> {
  const { maxFiles, maxTotalBytes, rules, kind, label, submit } = options

  const [files, setFiles] = useState<File[]>([])
  const [stage, setStage] = useState<PdfStage>('idle')
  const [uploadPercent, setUploadPercent] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [errorCode, setErrorCode] = useState<string | null>(null)
  const [result, setResult] = useState<T | null>(null)
  const [resultExpired, setResultExpired] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)

  const abortRef = useRef<AbortController | null>(null)

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
  /** 前端的本地校验失败（文件太大、格式不对）没有服务端错误码 */
  const reportError = useCallback((message: string) => {
    setErrorCode(null)
    setError(message)
  }, [])

  const addFiles = useCallback(
    (incoming: File[]) => {
      const room = maxFiles - files.length
      if (room <= 0) {
        setError(`一次最多处理 ${maxFiles} 个文件，请先移除一些再添加。`)
        return
      }

      const { accepted, error: rejection } = checkFiles(incoming, rules, kind)
      const next = [...files, ...accepted].slice(0, maxFiles)
      const dropped = accepted.length - (next.length - files.length)

      if (rejection) {
        setError(rejection)
      } else if (dropped > 0) {
        setError(`一次最多处理 ${maxFiles} 个文件，多出的 ${dropped} 个已忽略。`)
      } else {
        setError(null)
      }

      if (next.reduce((sum, file) => sum + file.size, 0) > maxTotalBytes) {
        setError(
          `一次最多处理 ${Math.round(maxTotalBytes / (1024 * 1024))} MB 的文件，请分批上传。`,
        )
      }

      setFiles(next)
      setResult(null)
      setStage('idle')
    },
    [files, kind, maxFiles, maxTotalBytes, rules],
  )

  const removeFile = useCallback((index: number) => {
    setFiles((current) => current.filter((_, position) => position !== index))
    setResult(null)
    setDownloadError(null)
    setStage('idle')
  }, [])

  /** 把第 from 个文件移动到 to 位置（拖动排序与上移/下移共用）。 */
  const moveFile = useCallback((from: number, to: number) => {
    setFiles((current) => {
      if (from === to || from < 0 || to < 0 || from >= current.length || to >= current.length) {
        return current
      }
      const next = [...current]
      const [moved] = next.splice(from, 1)
      if (!moved) return current
      next.splice(to, 0, moved)
      return next
    })
    setResult(null)
    setDownloadError(null)
    setStage('idle')
  }, [])

  const clearFiles = useCallback(() => {
    abortRef.current?.abort()
    setFiles([])
    setUploadPercent(0)
    setStage('idle')
    setResult(null)
    setError(null)
    setDownloadError(null)
  }, [])

  const start = useCallback(() => {
    if (stage === 'uploading' || stage === 'working' || files.length === 0) return

    const controller = new AbortController()
    abortRef.current = controller

    setError(null)
    setErrorCode(null)
    setDownloadError(null)
    // 上一次的结果已经下载掉了，这次是新的一份
    setResultExpired(false)
    setUploadPercent(0)
    setStage('uploading')

    submit(files, {
      onUploadProgress: (ratio) => {
        setUploadPercent(ratio * 100)
        // 上传结束即进入处理阶段：服务端这时才开始解析和生成
        if (ratio >= 1) setStage('working')
      },
      signal: controller.signal,
    })
      .then((response) => {
        if (controller.signal.aborted) return
        setResult(response)
        setStage('done')
        // 一次请求处理一批文件，成功即整批成功，按上传的每个文件各记一笔（§12）
        recordHistory(
          files.map((file) => ({
            filename: file.name,
            tool: label,
            ok: true,
            size: file.size,
          })),
        )
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        setError(caught instanceof Error ? caught.message : '处理失败，请重试')
        setErrorCode(caught instanceof ApiError ? caught.code : null)
        setStage('idle')
      })
      .finally(() => {
        abortRef.current = null
      })
  }, [files, label, stage, submit])

  const download = useCallback(async () => {
    if (!result) return
    setDownloading(true)
    setDownloadError(null)
    try {
      await downloadResult(result.download_url, result.filename)
      // 下载令牌是一次性的：下载成功后服务器就删了文件，
      // 标记一下，页面不再留一个点了必然报错的下载按钮
      setResultExpired(true)
    } catch (caught) {
      setDownloadError(caught instanceof Error ? caught.message : '下载失败，请重试')
    } finally {
      setDownloading(false)
    }
  }, [result])

  const reset = useCallback(() => {
    setResult(null)
    setDownloadError(null)
    setStage('idle')
  }, [])

  return {
    files,
    stage,
    uploadPercent,
    busy: stage === 'uploading' || stage === 'working',
    error,
    errorCode,
    result,
    resultExpired,
    downloading,
    downloadError,
    addFiles,
    removeFile,
    moveFile,
    clearFiles,
    start,
    download,
    reset,
    dismissError,
    reportError,
  }
}
