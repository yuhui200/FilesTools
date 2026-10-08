import { useCallback, useMemo, useRef, useState } from 'react'

import { Alert } from '@/components/Alert'
import { Button } from '@/components/Button'
import { OptionField } from '@/components/ConversionOptionField'
import { DesktopSavedFile } from '@/components/DesktopSavedFile'
import { Dropzone } from '@/components/Dropzone'
import { IconDownload, IconFile, IconX } from '@/components/Icons'
import { downloadResult, runPdfOperation } from '@/services/api'
import { isAbort, messageOf } from '@/services/taskRunner'
import type { ConversionCapability, ConversionFormatOption, PdfResultResponse } from '@/types'
import {
  defaultOptionValues,
  itemVisible,
  serializeOptions,
  type OptionValue,
  type OptionValues,
} from '@/utils/conversionOptions'
import { formatBytes } from '@/utils/format'
import type { UploadRules } from '@/utils/validation'


interface PdfOperationCardProps {
  entry: ConversionCapability
  /** 用来把 ``accepts`` 里的源类型翻成扩展名与中文名 */
  formats: ConversionFormatOption[]
  maxBytes: number
}

type Stage = 'idle' | 'uploading' | 'running'

/**
 * 一个 PDF 操作卡片（9b，决策 B）。
 *
 * 六张卡片共用这一个组件：**名字、放行哪些文件、参数面板、请求发到哪**
 * 全部读自服务端登记的那一条能力，页面上没有任何一句
 * 「合并收 PDF、合成收图片」这类判断。将来登记第七个操作，
 * 这里一行都不用改。
 *
 * 执行本身不重复实现：请求打到条目声明的 ``endpoint`` 上，
 * 也就是各工具原来的接口（§十二 的 ``operation`` 条目**不进统一队列**）。
 */
export function PdfOperationCard({ entry, formats, maxBytes }: PdfOperationCardProps) {
  const [files, setFiles] = useState<File[]>([])
  const [values, setValues] = useState<OptionValues>(() =>
    defaultOptionValues(entry.options_schema),
  )
  const [stage, setStage] = useState<Stage>('idle')
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<PdfResultResponse | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)
  // 桌面端落盘后的绝对路径；Web 上恒为 null（见 components/DesktopSavedFile.tsx）
  const [savedPath, setSavedPath] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const busy = stage !== 'idle'

  /**
   * 一次收几份文件。
   *
   * 令牌是**一份文件一个**（``/api/pdf/upload`` 的产物），所以走令牌的
   * 操作一次只处理一份；直接收文件的那两个可以一次多份，顺序即页序。
   * 这是 ``input_field`` 的结构性含义，不是又一张手抄表。
   */
  const multiple = entry.input_field === 'files'

  const rules: UploadRules = useMemo(() => {
    const allowed = new Set<string>()
    for (const source of entry.accepts) {
      const format = formats.find((item) => item.value === source)
      for (const extension of format?.extensions ?? []) allowed.add(extension)
    }
    return { allowedExtensions: [...allowed], maxBytes }
  }, [entry.accepts, formats, maxBytes])

  const hint = useMemo(() => {
    const names = entry.accepts
      .map((source) => formats.find((item) => item.value === source)?.label ?? source)
      .join('、')
    return names ? `支持 ${names}` : undefined
  }, [entry.accepts, formats])

  const items = (entry.options_schema?.items ?? []).filter((item) => itemVisible(item, values))
  /** 标了必填又没填的项 —— 拦在发请求之前，别让用户等一趟服务端 */
  const missing = items.filter(
    (item) => item.required && !String(values[item.key] ?? '').trim(),
  )

  const onSelect = useCallback(
    (picked: File[]) => {
      setError(null)
      setResult(null)
      setSavedPath(null)
      setFiles(multiple ? picked : picked.slice(0, 1))
    },
    [multiple],
  )

  const change = useCallback((key: string, value: OptionValue) => {
    setValues((current) => ({ ...current, [key]: value }))
  }, [])

  const start = useCallback(async () => {
    if (files.length === 0 || missing.length > 0) return
    const controller = new AbortController()
    abortRef.current = controller
    setError(null)
    setDownloadError(null)
    setResult(null)
    setSavedPath(null)
    setStage('uploading')

    try {
      const fields = serializeOptions(entry.options_schema, values)
      const response = await runPdfOperation({
        entry,
        files,
        // 端点的 Form 字段吃的都是字符串，序列化层已经把类型收敛过
        fields: Object.fromEntries(
          Object.entries(fields).map(([key, value]) => [key, String(value)]),
        ),
        onPhase: (phase) => setStage(phase === 'upload' ? 'uploading' : 'running'),
        signal: controller.signal,
      })
      setResult(response)
    } catch (caught) {
      if (isAbort(caught)) return
      setError(messageOf(caught))
    } finally {
      abortRef.current = null
      setStage('idle')
    }
  }, [entry, files, missing.length, values])

  const download = useCallback(async () => {
    if (!result?.download_url) return
    setDownloading(true)
    setDownloadError(null)
    try {
      const outcome = await downloadResult(result.download_url, result.filename)
      // 桌面端是「落盘」，Web 是「交给浏览器」；结果卡随即收起，位置先记下来
      setSavedPath(outcome.kind === 'desktop' ? outcome.path : null)
      // 结果文件下载后立即从服务器删除，这里如实反映
      setResult(null)
      setFiles([])
    } catch (caught) {
      setDownloadError(messageOf(caught))
    } finally {
      setDownloading(false)
    }
  }, [result])

  const blocked = !entry.available

  return (
    <div className="card p-5 sm:p-6">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-semibold text-slate-900">{entry.display_name}</h3>
          <p className="mt-1 text-xs text-slate-500">
            {entry.input_field === 'input_id'
              ? '一次处理一份文件'
              : '可以一次选多份，按你选定的顺序处理'}
          </p>
        </div>
      </div>

      {/* 服务端说这条能力现在不可用（缺组件）时如实说明，不装作能用 */}
      {!entry.available && (
        <div className="mt-4">
          <Alert tone="warning">这个操作当前不可用，服务器缺少所需的组件。</Alert>
        </div>
      )}

      {/* 放行清单与服务端登记的 accepts 同源：改注册表，这里跟着变 */}
      {entry.available && files.length === 0 && (
        <div className="mt-4">
          <Dropzone
            multiple={multiple}
            disabled={blocked}
            onSelect={onSelect}
            onError={setError}
            rules={rules}
            kind="文件"
            hint={hint}
          />
        </div>
      )}

      {files.length > 0 && (
        <>
          <ul className="mt-4 space-y-2">
            {files.map((file) => (
              <li
                key={`${file.name}-${file.size}-${file.lastModified}`}
                className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5"
              >
                <IconFile className="h-4 w-4 shrink-0 text-slate-400" />
                <span className="min-w-0 flex-1 truncate text-sm text-slate-900" title={file.name}>
                  {file.name}
                </span>
                <span className="shrink-0 text-xs text-slate-500">{formatBytes(file.size)}</span>
                {!busy && (
                  <button
                    type="button"
                    aria-label={`移除 ${file.name}`}
                    onClick={() => {
                      setFiles((current) => current.filter((item) => item !== file))
                      setResult(null)
                    }}
                    className="shrink-0 rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-200 hover:text-slate-600"
                  >
                    <IconX className="h-4 w-4" />
                  </button>
                )}
              </li>
            ))}
          </ul>

          {items.length > 0 && (
            <div className="mt-5 space-y-6 border-t border-slate-200 pt-5">
              {items.map((item) => (
                <OptionField
                  key={item.key}
                  groupId={entry.id}
                  item={item}
                  value={values[item.key]}
                  disabled={busy}
                  onChange={(value) => change(item.key, value)}
                />
              ))}
            </div>
          )}

          {/* 「每张图片一页」这类必须说给用户听的事实来自条目的 note */}
          {entry.note && (
            <div className="mt-4">
              <Alert tone="info">{entry.note}</Alert>
            </div>
          )}

          {error && (
            <div className="mt-4">
              <Alert tone="error">{error}</Alert>
            </div>
          )}

          {missing.length > 0 && (
            <p className="mt-3 text-xs text-red-600">
              还有必填项没填：{missing.map((item) => item.label).join('、')}
            </p>
          )}

          <div className="mt-5 flex items-center gap-3">
            <Button
              onClick={() => void start()}
              disabled={blocked || busy || missing.length > 0}
              loading={busy}
            >
              {stage === 'uploading' ? '正在上传…' : stage === 'running' ? '正在处理…' : '开始处理'}
            </Button>
            {/* 处理期间更要能取消：服务端的取消/超时都会回一条中文错误，
                但用户不该被迫等完 —— 所以这一颗按钮一直在 */}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                abortRef.current?.abort()
                setFiles([])
                setResult(null)
                setError(null)
                setDownloadError(null)
              }}
            >
              {busy ? '取消处理' : '取消'}
            </Button>
          </div>
        </>
      )}

      {result && (
        <div className="mt-5 rounded-xl border border-emerald-200 bg-emerald-50/70 p-4">
          <p className="text-sm font-medium text-emerald-900">✓ 处理完成</p>
          <div className="mt-2 flex items-center gap-3 rounded-lg bg-white px-3 py-2.5">
            <IconFile className="h-4 w-4 shrink-0 text-slate-400" />
            <span className="min-w-0 flex-1 truncate text-sm text-slate-900" title={result.filename}>
              {result.filename}
            </span>
            <span className="shrink-0 text-xs text-slate-500">
              {result.archived && result.files.length > 0
                ? `${result.files.length} 个文件 · ${formatBytes(result.size)}`
                : formatBytes(result.size)}
            </span>
          </div>

          {result.notes.map((note) => (
            <p key={note} className="mt-2 text-xs text-emerald-800">
              {note}
            </p>
          ))}

          {downloadError && (
            <p className="mt-2 text-xs text-red-600">{downloadError}</p>
          )}

          <div className="mt-3">
            <Button onClick={() => void download()} disabled={downloading} loading={downloading}>
              <IconDownload className="h-4 w-4" />
              下载结果
            </Button>
          </div>
        </div>
      )}

      {/* 桌面端落盘之后的位置与后续动作。
          这一块**必须放在结果卡外面**：上面那一段下载成功后就把 result 清掉了
          （服务端的临时文件同一时刻也删了），挂在里面等于永远看不到。
          Web 上这个组件不渲染 —— isDesktop 是构建期常量，整块会被摇掉。 */}
      <DesktopSavedFile path={savedPath} />
    </div>
  )
}
