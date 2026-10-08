import { useCallback, useMemo, useState } from 'react'

import { Alert } from '@/components/Alert'
import { Button } from '@/components/Button'
import { Dropzone } from '@/components/Dropzone'
import { IconArrowRight } from '@/components/Icons'
import { PdfProgressPanel } from '@/components/PdfProgressPanel'
import { PdfResultPanel, type ResultStat } from '@/components/PdfResultPanel'
import { RadioGroup, type RadioOption } from '@/components/RadioGroup'
import { SortableFileList } from '@/components/SortableFileList'
import { ToolPage } from '@/components/ToolPage'
import { useFilePreviews } from '@/hooks/useFilePreviews'
import { usePdfMultiTask } from '@/hooks/usePdfMultiTask'
import { usePdfProgress } from '@/hooks/usePdfProgress'
import { useServerConfig } from '@/hooks/useServerConfig'
import { imagesToPdf } from '@/services/api'
import type { PdfFit, PdfMargin, PdfOrientation, PdfPageSize } from '@/types'
import { formatBytes, formatDimensions } from '@/utils/format'

const PAGE_SIZE_OPTIONS: RadioOption<PdfPageSize>[] = [
  { value: 'auto', label: '自动', hint: '每页尺寸跟随图片本身' },
  { value: 'a4', label: 'A4', hint: '210 × 297 mm' },
  { value: 'a5', label: 'A5', hint: '148 × 210 mm' },
  { value: 'letter', label: 'Letter', hint: '216 × 279 mm' },
  { value: 'custom', label: '自定义', hint: '自己填写宽高（毫米）' },
]

const ORIENTATION_OPTIONS: RadioOption<PdfOrientation>[] = [
  { value: 'auto', label: '自动', hint: '按图片的横竖自动决定' },
  { value: 'portrait', label: '纵向', hint: '高大于宽' },
  { value: 'landscape', label: '横向', hint: '宽大于高' },
]

const FIT_OPTIONS: RadioOption<PdfFit>[] = [
  { value: 'contain', label: '保持比例', hint: '完整显示整张图片，四周可能留白' },
  { value: 'fill', label: '填充页面', hint: '铺满整页，超出部分会被裁掉' },
]

const MARGIN_OPTIONS: RadioOption<PdfMargin>[] = [
  { value: 'none', label: '无', hint: '图片顶到页边' },
  { value: 'small', label: '小', hint: '四周各留 5 毫米' },
  { value: 'medium', label: '中', hint: '四周各留 10 毫米' },
  { value: 'large', label: '大', hint: '四周各留 20 毫米' },
]

const PAGE_SIZE_LABEL: Record<PdfPageSize, string> = {
  auto: '自动',
  a4: 'A4',
  a5: 'A5',
  letter: 'Letter',
  custom: '自定义',
}

/**
 * 图片转 PDF（§3）。
 *
 * 顺序就是页面顺序，所以列表可以拖动排序，另有上移/下移按钮。
 */
export function PdfFromImages() {
  const { config, offlineMessage } = useServerConfig()

  const [pageSize, setPageSize] = useState<PdfPageSize>('auto')
  const [orientation, setOrientation] = useState<PdfOrientation>('auto')
  const [fit, setFit] = useState<PdfFit>('contain')
  const [margin, setMargin] = useState<PdfMargin>('none')
  const [customWidth, setCustomWidth] = useState('210')
  const [customHeight, setCustomHeight] = useState('297')

  const rules = useMemo(
    () => ({
      allowedExtensions: config.allowed_image_extensions,
      maxBytes: config.max_upload_bytes,
    }),
    [config.allowed_image_extensions, config.max_upload_bytes],
  )

  /** 自定义尺寸的本地校验：与服务端 20–2000 毫米的限制一致 */
  const customError = useMemo(() => {
    if (pageSize !== 'custom') return null
    const width = Number(customWidth)
    const height = Number(customHeight)
    if (!customWidth.trim() || !customHeight.trim()) {
      return '选择自定义页面时，请填写宽度和高度。'
    }
    if (!Number.isFinite(width) || !Number.isFinite(height)) {
      return '自定义页面尺寸必须是数字（单位毫米）。'
    }
    for (const [label, value] of [
      ['宽度', width],
      ['高度', height],
    ] as const) {
      if (value < 20 || value > 2000) {
        return `自定义页面${label}需要在 20–2000 毫米之间。`
      }
    }
    return null
  }, [pageSize, customWidth, customHeight])

  const submit = useCallback(
    (
      files: File[],
      params: { onUploadProgress: (ratio: number) => void; signal: AbortSignal },
    ) =>
      imagesToPdf({
        files,
        pageSize,
        orientation,
        fit,
        margin,
        customWidthMm: pageSize === 'custom' ? customWidth : null,
        customHeightMm: pageSize === 'custom' ? customHeight : null,
        ...params,
      }),
    [pageSize, orientation, fit, margin, customWidth, customHeight],
  )

  const task = usePdfMultiTask({
    maxFiles: config.max_batch_files,
    maxTotalBytes: config.max_batch_total_bytes,
    rules,
    kind: '图片',
    label: '图片转 PDF',
    submit,
  })

  const previews = useFilePreviews(task.files)
  const progress = usePdfProgress(
    task.stage,
    task.uploadPercent,
    `正在上传 ${task.files.length} 个文件…`,
  )

  const entries = task.files.map((file, index) => ({
    file,
    previewUrl: previews[index]?.url ?? null,
    meta: formatDimensions(previews[index]?.width ?? null, previews[index]?.height ?? null),
  }))

  const result = task.result
  const stats: ResultStat[] = result
    ? [
        { label: '图片数', value: `${task.files.length} 张` },
        {
          label: 'PDF 页数',
          value: result.page_count === null ? '—' : `${result.page_count} 页`,
        },
        { label: '文件大小', value: formatBytes(result.size), tone: 'brand' },
        { label: '页面大小', value: PAGE_SIZE_LABEL[pageSize] },
      ]
    : []

  const canStart = task.files.length > 0 && !task.busy && !customError

  return (
    <ToolPage
      category="pdf"
      title="图片转 PDF"
      description={`一次最多 ${config.max_batch_files} 张图片，一张图片一页，可拖动调整顺序。`}
      offlineMessage={offlineMessage}
    >
      {task.error && (
        <Alert tone="error" onDismiss={task.dismissError}>
          {task.error}
        </Alert>
      )}

      {task.files.length === 0 && task.stage !== 'done' && (
        <Dropzone
          multiple
          onSelect={task.addFiles}
          onError={task.reportError}
          rules={rules}
          hint={`支持 ${config.allowed_image_extensions
            .map((ext) => ext.replace('.', '').toUpperCase())
            .join('、')}，单个最大 ${formatBytes(config.max_upload_bytes)}，最多 ${
            config.max_batch_files
          } 张`}
        />
      )}

      <PdfProgressPanel
        stage={task.stage}
        progress={progress}
        hasFiles={task.files.length > 0}
      />

      {task.stage === 'done' && result && (
        <PdfResultPanel
          result={result}
          stats={stats}
          onDownload={task.download}
          onReset={task.reset}
          resetLabel="调整设置"
          onRestart={task.clearFiles}
          downloading={task.downloading}
          downloadError={task.downloadError}
          savedPath={task.savedPath}
        />
      )}

      {task.files.length > 0 && task.stage === 'idle' && (
        <div className="card animate-fade-in-up p-5 sm:p-6">
          <SortableFileList
            entries={entries}
            onRemove={task.removeFile}
            onMove={task.moveFile}
            disabled={task.busy}
            unitLabel="页"
            hint="拖动左侧抓手可以调整顺序，这一顺序就是生成后的页面顺序。"
          />

          {task.files.length < config.max_batch_files && (
            <div className="mt-4">
              <Dropzone
                compact
                multiple
                onSelect={task.addFiles}
                onError={task.reportError}
                rules={rules}
                hint="可以继续添加，也可以直接把图片拖到这里"
              />
            </div>
          )}

          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">页面大小</legend>
            <p className="mt-1 text-xs text-slate-500">生成 PDF 的每一页用多大的纸张。</p>
            <div className="mt-3">
              <RadioGroup
                name="page-size"
                value={pageSize}
                options={PAGE_SIZE_OPTIONS}
                onChange={setPageSize}
                disabled={task.busy}
              />
            </div>

            {pageSize === 'custom' && (
              <div className="mt-3 flex flex-wrap items-end gap-3">
                <label className="text-xs text-slate-600">
                  <span className="mb-1 block font-medium text-slate-700">宽度（毫米）</span>
                  <input
                    type="number"
                    inputMode="decimal"
                    min={20}
                    max={2000}
                    value={customWidth}
                    disabled={task.busy}
                    onChange={(event) => setCustomWidth(event.target.value)}
                    className="h-10 w-28 rounded-lg border border-slate-300 px-3 text-sm tabular-nums outline-none focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20"
                  />
                </label>
                <label className="text-xs text-slate-600">
                  <span className="mb-1 block font-medium text-slate-700">高度（毫米）</span>
                  <input
                    type="number"
                    inputMode="decimal"
                    min={20}
                    max={2000}
                    value={customHeight}
                    disabled={task.busy}
                    onChange={(event) => setCustomHeight(event.target.value)}
                    className="h-10 w-28 rounded-lg border border-slate-300 px-3 text-sm tabular-nums outline-none focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20"
                  />
                </label>
                <span className="pb-2.5 text-xs text-slate-500">可填 20–2000 毫米</span>
              </div>
            )}

            {customError && (
              <div className="mt-3">
                <Alert tone="error">{customError}</Alert>
              </div>
            )}
          </fieldset>

          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">页面方向</legend>
            <div className="mt-3">
              <RadioGroup
                name="orientation"
                value={orientation}
                options={ORIENTATION_OPTIONS}
                onChange={setOrientation}
                disabled={task.busy}
              />
            </div>
          </fieldset>

          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">图片适应方式</legend>
            <p className="mt-1 text-xs text-slate-500">
              图片和纸张比例不一致时怎么处理。
            </p>
            <div className="mt-3">
              <RadioGroup
                name="fit"
                value={fit}
                options={FIT_OPTIONS}
                onChange={setFit}
                columns={2}
                disabled={task.busy}
              />
            </div>
          </fieldset>

          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">页边距</legend>
            <div className="mt-3">
              <RadioGroup
                name="margin"
                value={margin}
                options={MARGIN_OPTIONS}
                onChange={setMargin}
                disabled={task.busy}
              />
            </div>
          </fieldset>

          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="order-2 text-xs text-slate-500 sm:order-1">
              {task.files.length} 张图片 · 合计{' '}
              {formatBytes(task.files.reduce((sum, file) => sum + file.size, 0))}
            </p>
            <Button
              size="lg"
              onClick={task.start}
              disabled={!canStart}
              icon={<IconArrowRight className="h-4 w-4" />}
              className="order-1 sm:order-2"
            >
              生成 PDF
            </Button>
          </div>
        </div>
      )}
    </ToolPage>
  )
}
