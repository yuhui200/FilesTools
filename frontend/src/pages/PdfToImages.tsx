import { useCallback, useMemo, useState } from 'react'

import { Alert } from '@/components/Alert'
import { Button } from '@/components/Button'
import { Dropzone } from '@/components/Dropzone'
import { IconArrowRight } from '@/components/Icons'
import { PdfFileCard } from '@/components/PdfFileCard'
import { PdfProgressPanel } from '@/components/PdfProgressPanel'
import { PdfResultPanel, type ResultStat } from '@/components/PdfResultPanel'
import { QualityField } from '@/components/QualityField'
import { RadioGroup, type RadioOption } from '@/components/RadioGroup'
import { TaskProgressPanel } from '@/components/TaskProgressPanel'
import { ToolPage } from '@/components/ToolPage'
import { PRESET_QUALITY, type QualityChoice } from '@/hooks/useBatchTask'
import { usePdfInput } from '@/hooks/usePdfInput'
import { usePdfProgress } from '@/hooks/usePdfProgress'
import { useServerConfig } from '@/hooks/useServerConfig'
import { pdfToImages } from '@/services/api'
import type { OutputFormat, PdfResolution } from '@/types'
import { formatBytes } from '@/utils/format'
import { parsePageSelection } from '@/utils/pdf'

const FORMAT_OPTIONS: RadioOption<OutputFormat>[] = [
  { value: 'jpg', label: 'JPG', hint: '体积最小，适合照片型页面' },
  { value: 'png', label: 'PNG', hint: '无损，文字边缘更清晰' },
  { value: 'webp', label: 'WEBP', hint: '体积更小，现代浏览器支持好' },
]

const RESOLUTION_OPTIONS: RadioOption<PdfResolution>[] = [
  { value: 'standard', label: '标准', hint: '96 DPI，屏幕阅读' },
  { value: 'high', label: '高清', hint: '150 DPI，日常归档（推荐）' },
  { value: 'ultra', label: '超清', hint: '300 DPI，打印质量，文件较大' },
]

const FORMAT_LABEL: Record<OutputFormat, string> = { jpg: 'JPG', png: 'PNG', webp: 'WEBP' }

export function PdfToImages() {
  const { config, offlineMessage } = useServerConfig()

  const [targetFormat, setTargetFormat] = useState<OutputFormat>('jpg')
  const [resolution, setResolution] = useState<PdfResolution>('high')
  const [rangeText, setRangeText] = useState('')
  const [qualityChoice, setQualityChoice] = useState<QualityChoice>('balanced')
  const [customQuality, setCustomQuality] = useState(config.default_quality_value)

  const rules = useMemo(
    () => ({
      allowedExtensions: config.allowed_pdf_extensions,
      maxBytes: config.max_upload_bytes,
    }),
    [config.allowed_pdf_extensions, config.max_upload_bytes],
  )

  // 一次可以选多份 PDF（§2 的「PDF 批量转图片」），上限与服务端一致
  const task = usePdfInput(rules, { maxFiles: config.max_batch_files, label: 'PDF 转图片' })
  const progress = usePdfProgress(task.stage, task.uploadPercent, '正在上传 PDF…')

  const { inputs } = task
  const firstPages = inputs[0]?.page_count ?? 0

  /**
   * 页面范围对所有 PDF 生效，因此每一份都按**自己的页数**解析一次：
   * 「1-3」在 10 页的文档上是 3 页，在 2 页的文档上就是 2 页。
   * 只要有一份解析失败就报错，避免用户以为全部按预期导出了。
   *
   * 留空等于 all：这和输入框的提示、以及后端「pages 为空即全部页面」的
   * 处理是一致的，界面上的「共导出 N 张」才对得上真正会导出的张数。
   */
  const selections = useMemo(
    () => inputs.map((item) => parsePageSelection(rangeText.trim() || 'all', item.page_count)),
    [inputs, rangeText],
  )
  const selectionError = selections.find((item) => item.error)?.error ?? null
  const selectedPages = selections.map((item) => item.pages.length)
  const maxSelectedPages = selectedPages.length > 0 ? Math.max(...selectedPages) : 0
  const totalSelectedPages = selectedPages.reduce((sum, count) => sum + count, 0)
  const overExportLimit = maxSelectedPages > config.pdf_export_max_pages

  /** §4 的四种页面范围写法，按第一份文档的页数给出，不够页数就不显示 */
  const presets = useMemo(() => {
    if (firstPages === 0) return []
    const items: { label: string; value: string }[] = [{ label: '全部页面', value: 'all' }]
    if (firstPages >= 3) items.push({ label: '第 1-3 页', value: '1-3' })
    if (firstPages >= 5) items.push({ label: '第 1、3、5 页', value: '1,3,5' })
    if (firstPages >= 6) items.push({ label: '第 2-6 页', value: '2-6' })
    return items
  }, [firstPages])

  const qualityValue =
    qualityChoice === 'custom'
      ? Math.min(100, Math.max(1, Math.round(customQuality)))
      : PRESET_QUALITY[qualityChoice]

  const run = useCallback(
    () =>
      task.runBatch((_inputId, inputIds, signal) =>
        pdfToImages({
          inputIds,
          targetFormat,
          // PNG 是无损格式，不发送质量参数（§4：选 PNG 时不显示 JPG 的质量选项）
          quality: targetFormat === 'png' ? null : qualityValue,
          resolution,
          pages: rangeText.trim() && rangeText.trim() !== 'all' ? rangeText.trim() : null,
          signal,
        }),
      ),
    [task, targetFormat, qualityValue, resolution, rangeText],
  )

  const canStart =
    task.stage === 'ready' &&
    inputs.length > 0 &&
    !selectionError &&
    !overExportLimit &&
    totalSelectedPages > 0

  const uploading = task.stage === 'uploading'
  const working = task.stage === 'working'
  // 整批失败后停在 ready：每个文件的原因仍然留在页面上（§4）
  const batchFailed = task.stage === 'ready' && task.snapshot?.state === 'failed'
  const showTaskPanel = uploading || working || batchFailed

  const result = task.result
  const stats: ResultStat[] = result
    ? [
        { label: '导出张数', value: `${result.files.length} 张` },
        { label: '格式', value: FORMAT_LABEL[targetFormat] },
        { label: '合计大小', value: formatBytes(result.size), tone: 'brand' },
        { label: '打包', value: result.archived ? 'ZIP' : '单个文件' },
      ]
    : []

  return (
    <ToolPage
      category="pdf"
      title="PDF 转图片"
      description={`把 PDF 的页面导出成图片，可一次选多份 PDF，每份最多导出 ${config.pdf_export_max_pages} 页，多于一张时自动打包成 ZIP。`}
      offlineMessage={offlineMessage}
    >
      {task.error && !batchFailed && (
        <Alert tone="error" onDismiss={task.dismissError}>
          {task.error}
        </Alert>
      )}

      {task.stage === 'idle' && (
        <Dropzone
          multiple
          onSelect={task.selectFiles}
          onError={task.reportError}
          rules={rules}
          kind="文件"
          hint={`支持 PDF 文件，单个最大 ${formatBytes(config.max_upload_bytes)}，最多 ${
            config.max_batch_files
          } 份，每份最多 ${config.max_pdf_pages} 页`}
        />
      )}

      {showTaskPanel ? (
        <TaskProgressPanel
          title="PDF 转图片"
          uploadPercent={uploading ? task.uploadPercent : null}
          snapshot={task.snapshot}
          pendingFiles={task.files}
          error={
            batchFailed
              ? { code: task.errorCode ?? 'PROCESSING_FAILED', message: task.error ?? '' }
              : null
          }
        />
      ) : (
        // 已上传、等用户设置参数时的状态条（§12 的「等待处理」）
        task.stage === 'ready' && (
          <PdfProgressPanel
            stage={task.stage}
            progress={progress}
            hasFiles={inputs.length > 0}
          />
        )
      )}

      {task.stage === 'done' && result && (
        <PdfResultPanel
          result={result}
          stats={stats}
          showImageGrid
          expired={task.resultExpired}
          onDownload={task.download}
          onReset={task.backToSettings}
          resetLabel="换个设置再导出"
          onRestart={task.reset}
          downloading={task.downloading}
          downloadError={task.downloadError}
        />
      )}

      {task.stage === 'ready' && inputs.length > 0 && (
        <div className="card animate-fade-in-up p-5 sm:p-6">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="text-sm font-medium text-slate-900">
              已上传 {inputs.length} 份 PDF
            </h2>
            <span className="text-xs text-slate-500">
              合计 {task.totalPages} 页
            </span>
          </div>

          <ul className="mt-3 space-y-2">
            {inputs.map((item, index) => (
              <li key={item.input_id}>
                <PdfFileCard
                  filename={item.filename}
                  size={item.size}
                  pageCount={item.page_count}
                  onRemove={() => task.removeEntry(index)}
                  removeLabel="移除"
                  disabled={task.busy}
                />
              </li>
            ))}
          </ul>

          {inputs.length < config.max_batch_files && (
            <div className="mt-4">
              <Dropzone
                compact
                multiple
                onSelect={task.selectFiles}
                onError={task.reportError}
                rules={rules}
                kind="文件"
                hint="可以继续添加 PDF，也可以直接把文件拖到这里"
              />
            </div>
          )}

          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">转换格式</legend>
            <div className="mt-3">
              <RadioGroup
                name="target-format"
                value={targetFormat}
                options={FORMAT_OPTIONS}
                onChange={setTargetFormat}
                disabled={task.busy}
              />
            </div>
          </fieldset>

          {/* §4：PNG 不显示 JPG 的质量选项 */}
          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">图片质量</legend>
            <div className="mt-3">
              {targetFormat === 'png' ? (
                <Alert tone="info">
                  PNG 是无损格式，不做有损压缩，因此不需要设置 JPG 那样的质量。
                  如果希望明显减小体积，建议选择 JPG 或 WEBP。
                </Alert>
              ) : (
                <QualityField
                  value={qualityChoice}
                  customValue={customQuality}
                  defaultValue={config.default_quality_value}
                  onChange={setQualityChoice}
                  onCustomChange={setCustomQuality}
                  disabled={task.busy}
                />
              )}
            </div>
          </fieldset>

          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">清晰度</legend>
            <div className="mt-3">
              <RadioGroup
                name="resolution"
                value={resolution}
                options={RESOLUTION_OPTIONS}
                onChange={setResolution}
                disabled={task.busy}
              />
            </div>
          </fieldset>

          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">页面范围</legend>
            <p className="mt-1 text-xs text-slate-500">
              留空表示全部页面；也可以填 1-3、1,3,5、2-6 这样的范围。
              {inputs.length > 1 && ' 范围对每一份 PDF 分别生效。'}
            </p>
            <div className="mt-3">
              <input
                type="text"
                value={rangeText}
                disabled={task.busy}
                onChange={(event) => setRangeText(event.target.value)}
                placeholder={`例如 1-${Math.min(firstPages || 3, 10)}，留空导出全部`}
                aria-label="页面范围"
                className="field w-full sm:max-w-sm"
              />
              {presets.length > 1 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {presets.map((preset) => (
                    <button
                      key={preset.value}
                      type="button"
                      disabled={task.busy}
                      onClick={() => setRangeText(preset.value)}
                      className={[
                        'rounded-lg border px-2.5 py-1 text-xs transition',
                        rangeText === preset.value
                          ? 'border-brand-500 bg-brand-50 text-brand-700'
                          : 'border-slate-200 text-slate-600 hover:border-slate-300 hover:bg-slate-50',
                      ].join(' ')}
                    >
                      {preset.label}
                    </button>
                  ))}
                </div>
              )}

              {selectionError && (
                <div className="mt-3">
                  <Alert tone="error">{selectionError}</Alert>
                </div>
              )}

              {!selectionError && totalSelectedPages > 0 && (
                <p className="mt-3 text-xs text-slate-500">
                  {inputs.length > 1
                    ? `将从 ${inputs.length} 份 PDF 共导出 ${totalSelectedPages} 张图片（每份最多 ${maxSelectedPages} 页），`
                    : `将导出 ${totalSelectedPages} 页，`}
                  {totalSelectedPages > 1
                    ? '自动打包成 pdf_pages.zip 下载。'
                    : '直接下载图片文件。'}
                </p>
              )}

              {overExportLimit && (
                <div className="mt-3">
                  <Alert tone="error">
                    一份 PDF 一次最多导出 {config.pdf_export_max_pages} 页，当前最多的一份选了{' '}
                    {maxSelectedPages} 页。请缩小范围分几次导出。
                  </Alert>
                </div>
              )}
            </div>
          </fieldset>

          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="order-2 text-xs text-slate-500 sm:order-1">
              {inputs.length} 份 PDF · 共 {task.totalPages} 页 · 导出为{' '}
              {FORMAT_LABEL[targetFormat]}
            </p>
            <Button
              size="lg"
              onClick={run}
              disabled={!canStart}
              icon={<IconArrowRight className="h-4 w-4" />}
              className="order-1 sm:order-2"
            >
              开始导出
            </Button>
          </div>
        </div>
      )}
    </ToolPage>
  )
}
