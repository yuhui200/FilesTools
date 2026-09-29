import { useCallback, useMemo, useState } from 'react'

import { Alert } from '@/components/Alert'
import { Button } from '@/components/Button'
import { Dropzone } from '@/components/Dropzone'
import { IconArrowRight } from '@/components/Icons'
import { PdfFileCard } from '@/components/PdfFileCard'
import { PdfProgressPanel } from '@/components/PdfProgressPanel'
import { PdfResultPanel, type ResultStat } from '@/components/PdfResultPanel'
import { RadioGroup, type RadioOption } from '@/components/RadioGroup'
import { ToolPage } from '@/components/ToolPage'
import { usePdfInput } from '@/hooks/usePdfInput'
import { usePdfProgress } from '@/hooks/usePdfProgress'
import { useServerConfig } from '@/hooks/useServerConfig'
import { compressPdf } from '@/services/api'
import type { PdfCompressLevel, PdfTargetOption } from '@/types'
import { formatBytes, formatPercent } from '@/utils/format'

const LEVEL_OPTIONS: RadioOption<PdfCompressLevel>[] = [
  { value: 'light', label: '轻度', hint: '分辨率上限 200 DPI，画质优先' },
  { value: 'balanced', label: '平衡', hint: '分辨率上限 150 DPI，体积与画质兼顾' },
  { value: 'strong', label: '高压缩', hint: '分辨率上限 96 DPI，体积优先' },
]

const TARGET_OPTIONS: RadioOption<PdfTargetOption>[] = [
  { value: 'none', label: '不限制', hint: '只按压缩等级处理' },
  { value: '5mb', label: '5 MB' },
  { value: '10mb', label: '10 MB' },
  { value: '20mb', label: '20 MB' },
  { value: 'custom', label: '自定义' },
]

const TARGET_MB: Record<'5mb' | '10mb' | '20mb', number> = { '5mb': 5, '10mb': 10, '20mb': 20 }

const LEVEL_LABEL: Record<PdfCompressLevel, string> = {
  light: '轻度',
  balanced: '平衡',
  strong: '高压缩',
}

/** 与服务端一致的取值范围（MB） */
const MIN_TARGET_MB = 0.1
const MAX_TARGET_MB = 500

export function PdfCompress() {
  const { config, offlineMessage } = useServerConfig()

  const [level, setLevel] = useState<PdfCompressLevel>(
    (config.default_pdf_compress_level as PdfCompressLevel) || 'balanced',
  )
  const [target, setTarget] = useState<PdfTargetOption>('none')
  const [customMb, setCustomMb] = useState('2')

  const rules = useMemo(
    () => ({
      allowedExtensions: config.allowed_pdf_extensions,
      maxBytes: config.max_upload_bytes,
    }),
    [config.allowed_pdf_extensions, config.max_upload_bytes],
  )

  const task = usePdfInput(rules, { label: 'PDF 压缩' })
  const progress = usePdfProgress(task.stage, task.uploadPercent, '正在上传 PDF…')

  const originalSize = task.input?.size ?? 0

  const targetError = useMemo(() => {
    if (target !== 'custom') return null
    if (customMb.trim() === '') return '选择自定义目标大小时，请填写目标大小（MB）。'
    const value = Number(customMb)
    if (!Number.isFinite(value)) return '目标大小必须是数字（单位 MB）。'
    if (value < MIN_TARGET_MB || value > MAX_TARGET_MB) {
      return `目标大小需要在 ${MIN_TARGET_MB}–${MAX_TARGET_MB} MB 之间。`
    }
    return null
  }, [target, customMb])

  /** 目标大小（字节），仅用于界面提示，真正的判断在服务端 */
  const targetBytes = useMemo(() => {
    if (target === 'none' || targetError) return null
    const mb = target === 'custom' ? Number(customMb) : TARGET_MB[target]
    return Number.isFinite(mb) ? Math.round(mb * 1024 * 1024) : null
  }, [target, customMb, targetError])

  const run = useCallback(
    () =>
      task.run((inputId) =>
        compressPdf({
          inputId,
          level,
          target,
          targetMb: target === 'custom' ? customMb : null,
        }),
      ),
    [task, level, target, customMb],
  )

  const canStart = task.stage === 'ready' && !targetError

  const result = task.result
  const stats: ResultStat[] = result
    ? [
        {
          label: '原文件',
          value: formatBytes(result.original_size ?? originalSize),
        },
        { label: '压缩后', value: formatBytes(result.size), tone: 'brand' },
        {
          label: '节省',
          value:
            result.saved_percent === null || (result.saved_bytes ?? 0) <= 0
              ? '—'
              : formatPercent(result.saved_percent),
          tone: (result.saved_bytes ?? 0) > 0 ? 'emerald' : 'muted',
        },
        {
          label: '页数',
          value: result.page_count === null ? '—' : `${result.page_count} 页`,
        },
      ]
    : []

  return (
    <ToolPage
      category="pdf"
      title="PDF 压缩"
      description="重新编码页面里的图片来减小体积。扫描件、图片型 PDF 效果最明显。"
      offlineMessage={offlineMessage}
    >
      {task.error && (
        <Alert tone="error" onDismiss={task.dismissError}>
          {task.error}
        </Alert>
      )}

      {task.stage === 'idle' && (
        <Dropzone
          onSelect={task.selectFirst}
          onError={task.reportError}
          rules={rules}
          kind="文件"
          hint={`支持 PDF 文件，单个最大 ${formatBytes(config.max_upload_bytes)}，最多 ${config.max_pdf_pages} 页`}
        />
      )}

      <PdfProgressPanel stage={task.stage} progress={progress} hasFiles={task.input !== null} />

      {task.stage === 'done' && result && (
        <PdfResultPanel
          result={result}
          stats={stats}
          doneTitle="✓ 压缩完成"
          onDownload={task.download}
          onReset={task.backToSettings}
          resetLabel="换个等级再压"
          onRestart={task.reset}
          downloading={task.downloading}
          downloadError={task.downloadError}
        />
      )}

      {task.stage === 'ready' && task.input && (
        <div className="card animate-fade-in-up p-5 sm:p-6">
          <PdfFileCard
            filename={task.input.filename}
            size={task.input.size}
            pageCount={task.input.page_count}
            onRemove={task.reset}
            disabled={task.busy}
          />

          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">压缩等级</legend>
            <p className="mt-1 text-xs text-slate-500">
              等级决定页面图片的分辨率上限和重编码质量。等级越高，体积越小，画质损失越明显。
            </p>
            <div className="mt-3">
              <RadioGroup
                name="compress-level"
                value={level}
                options={LEVEL_OPTIONS}
                onChange={setLevel}
                disabled={task.busy}
              />
            </div>
          </fieldset>

          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">目标最大文件大小</legend>
            <p className="mt-1 text-xs text-slate-500">
              选择后会在所选等级的基础上逐档加强，直到压到目标以内。
            </p>
            <div className="mt-3">
              <RadioGroup
                name="target-size"
                value={target}
                options={TARGET_OPTIONS}
                onChange={setTarget}
                disabled={task.busy}
              />
            </div>

            {target === 'custom' && (
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <input
                  type="number"
                  inputMode="decimal"
                  min={MIN_TARGET_MB}
                  max={MAX_TARGET_MB}
                  step="0.1"
                  value={customMb}
                  disabled={task.busy}
                  onChange={(event) => setCustomMb(event.target.value)}
                  aria-label="自定义目标大小"
                  className="field w-28"
                />
                <span className="text-xs text-slate-500">
                  MB，可填 {MIN_TARGET_MB}–{MAX_TARGET_MB}
                </span>
              </div>
            )}

            {targetError && (
              <div className="mt-3">
                <Alert tone="error">{targetError}</Alert>
              </div>
            )}
          </fieldset>

          {!targetError && (
            <div className="mt-4">
              <Alert tone="info">
                原文件 {formatBytes(task.input.size)}，共 {task.input.page_count} 页，
                将按「{LEVEL_LABEL[level]}」压缩
                {targetBytes === null
                  ? '，不限制目标大小。'
                  : `，并尽量压到 ${formatBytes(targetBytes)} 以内。`}
                {targetBytes !== null && targetBytes > task.input.size
                  ? ' 目标大小已经大于原文件，服务端仍会按等级压缩，但不会给你一个更大的文件。'
                  : ''}
              </Alert>
            </div>
          )}

          {level === 'strong' && (
            <div className="mt-3">
              <Alert tone="warning">
                高压缩会把页面图片限制到 96 DPI、质量 55，文字可能发虚。
                如果这份 PDF 是用来打印或存档的，建议选择「平衡」。
              </Alert>
            </div>
          )}

          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="order-2 text-xs text-slate-500 sm:order-1">
              {task.input.filename} · {formatBytes(task.input.size)} · {task.input.page_count} 页
            </p>
            <Button
              size="lg"
              onClick={run}
              disabled={!canStart}
              icon={<IconArrowRight className="h-4 w-4" />}
              className="order-1 sm:order-2"
            >
              开始压缩
            </Button>
          </div>
        </div>
      )}
    </ToolPage>
  )
}
