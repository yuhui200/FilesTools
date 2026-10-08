import { useCallback, useMemo, useState } from 'react'

import { Alert } from '@/components/Alert'
import { Button } from '@/components/Button'
import { Dropzone } from '@/components/Dropzone'
import { IconArrowRight } from '@/components/Icons'
import { PdfFileCard } from '@/components/PdfFileCard'
import { PdfPageGrid } from '@/components/PdfPageGrid'
import { PdfProgressPanel } from '@/components/PdfProgressPanel'
import { PdfResultPanel, type ResultStat } from '@/components/PdfResultPanel'
import { RadioGroup, type RadioOption } from '@/components/RadioGroup'
import { SortablePageList } from '@/components/SortablePageList'
import { ToolPage } from '@/components/ToolPage'
import { usePageOrder } from '@/hooks/usePageOrder'
import { usePdfInput } from '@/hooks/usePdfInput'
import { usePdfProgress } from '@/hooks/usePdfProgress'
import { useServerConfig } from '@/hooks/useServerConfig'
import { deletePdfPages, extractPdfPages } from '@/services/api'
import { formatBytes } from '@/utils/format'
import {
  describeSelection,
  formatPageList,
  formatPageSequence,
  parsePageSelection,
} from '@/utils/pdf'

type EditMode = 'delete' | 'extract'

const MODE_OPTIONS: RadioOption<EditMode>[] = [
  {
    value: 'delete',
    label: '删除页面',
    hint: '点选要删掉的页面，其余页面按原顺序生成新的 PDF',
  },
  {
    value: 'extract',
    label: '提取页面',
    hint: '点选要保留的页面，可以拖动调整先后顺序，再合成一份新的 PDF',
  },
]

/**
 * PDF 页面删除（§8）与页面提取（§9）。
 *
 * 两个功能的交互完全一样 —— 都是「看着缩略图挑页面」，差别只在
 * 挑中的页面是「要删掉」还是「要留下」，所以放在同一个页面里用模式切换，
 * 后端也只实现了一份「重建 PDF」的逻辑。
 */
export function PdfEditPages() {
  const { config, offlineMessage } = useServerConfig()

  const [mode, setMode] = useState<EditMode>('delete')
  const [rangeText, setRangeText] = useState('')

  const rules = useMemo(
    () => ({
      allowedExtensions: config.allowed_pdf_extensions,
      maxBytes: config.max_upload_bytes,
    }),
    [config.allowed_pdf_extensions, config.max_upload_bytes],
  )

  const task = usePdfInput(rules, { label: 'PDF 页面删除 / 提取' })
  const progress = usePdfProgress(task.stage, task.uploadPercent, '正在上传 PDF…')

  const pageCount = task.input?.page_count ?? 0
  const selection = useMemo(
    () => parsePageSelection(rangeText, pageCount),
    [rangeText, pageCount],
  )
  const selected = selection.pages

  /**
   * 提取模式下用户排定的输出顺序（§11）。
   *
   * 删除模式不需要排序：保留的页按原顺序输出即可。
   */
  const order = usePageOrder(mode === 'extract' ? selected : [])

  /** 缩略图点击与页码输入框操作的是同一份选择 */
  const togglePage = (page: number) => {
    const next = selected.includes(page)
      ? selected.filter((item) => item !== page)
      : [...selected, page].sort((a, b) => a - b)
    setRangeText(formatPageList(next))
  }

  const selectAll = () => setRangeText(pageCount > 0 ? 'all' : '')
  const clearSelection = () => setRangeText('')

  /** 与后端一致的前置校验：这两条都是服务端也会拒绝的情况，提前说清楚 */
  const blockingError = useMemo(() => {
    if (selection.error) return selection.error
    if (selected.length === 0) {
      return mode === 'delete' ? '请先选择要删除的页面。' : '请先选择要提取的页面。'
    }
    if (mode === 'delete' && selected.length >= pageCount) {
      return '不能删除全部页面，请至少保留一页。'
    }
    return null
  }, [selection.error, selected.length, pageCount, mode])

  const run = useCallback(() => {
    // 提取按用户排定的顺序发送（§11），删除与页序无关，照旧用压缩后的升序列表
    const pages =
      mode === 'extract'
        ? formatPageSequence(order.ordered, pageCount)
        : formatPageList(selected)
    return task.run((inputId) =>
      mode === 'delete'
        ? deletePdfPages({ inputId, pages })
        : extractPdfPages({ inputId, pages }),
    )
  }, [task, mode, selected, order.ordered, pageCount])

  const canStart = task.stage === 'ready' && !blockingError

  const result = task.result
  const stats: ResultStat[] = result
    ? [
        {
          label: '新 PDF 页数',
          value: result.page_count === null ? '—' : `${result.page_count} 页`,
          tone: 'brand',
        },
        {
          label: '原文件页数',
          value: result.original_pages === null ? '—' : `${result.original_pages} 页`,
        },
        {
          label: mode === 'delete' ? '已删除' : '已提取',
          value:
            result.original_pages === null || result.page_count === null
              ? '—'
              : mode === 'delete'
                ? `${result.original_pages - result.page_count} 页`
                : `${result.page_count} 页`,
        },
        { label: '文件大小', value: formatBytes(result.size) },
      ]
    : []

  const outputName =
    mode === 'delete'
      ? `${(task.input?.filename ?? 'document.pdf').replace(/\.pdf$/i, '')}_edited.pdf`
      : 'selected_pages.pdf'

  return (
    <ToolPage
      category="pdf"
      title="PDF 页面删除 / 提取"
      description="看着页面缩略图点选：删掉不要的页面，或者只留下需要的页面。"
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
          onDownload={task.download}
          onReset={task.backToSettings}
          resetLabel="重新选页面"
          onRestart={task.reset}
          downloading={task.downloading}
          downloadError={task.downloadError}
          savedPath={task.savedPath}
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
            <legend className="text-sm font-medium text-slate-900">要做什么</legend>
            <div className="mt-3">
              <RadioGroup
                name="edit-mode"
                value={mode}
                options={MODE_OPTIONS}
                onChange={setMode}
                columns={2}
                disabled={task.busy}
              />
            </div>
          </fieldset>

          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">
              {mode === 'delete' ? '点选要删除的页面' : '点选要保留的页面'}
            </legend>
            <p className="mt-1 text-xs text-slate-500">
              {mode === 'delete'
                ? '点击缩略图选中（红色标记），这些页面不会出现在新的 PDF 里。'
                : '点击缩略图选中（蓝色标记），只有这些页面会出现在新的 PDF 里。'}
            </p>
            <div className="mt-3">
              <PdfPageGrid
                thumbnailBase={task.input.thumbnail_base}
                pageCount={pageCount}
                thumbnailLimit={config.pdf_thumbnail_max_pages}
                selected={selected}
                onToggle={togglePage}
                onSelectAll={selectAll}
                onClear={clearSelection}
                tone={mode === 'delete' ? 'danger' : 'brand'}
                disabled={task.busy}
              />
            </div>
          </fieldset>

          <fieldset className="mt-7">
            <legend className="text-sm font-medium text-slate-900">页码</legend>
            <p className="mt-1 text-xs text-slate-500">
              上面的缩略图和这里填写的页码是同一份选择，两边都可以改。
              页码可以写成 1-3、1,3,5 或 2-6。
            </p>
            <div className="mt-3">
              <input
                type="text"
                value={rangeText}
                disabled={task.busy}
                onChange={(event) => setRangeText(event.target.value)}
                placeholder={mode === 'delete' ? '例如 2,4,7-9' : '例如 1,3,5,8'}
                aria-label="页码"
                className="field w-full sm:max-w-md"
              />
              <p className="mt-2 text-xs text-slate-500">
                {describeSelection(selected, pageCount)}
              </p>
            </div>
          </fieldset>

          {mode === 'extract' && selected.length > 0 && (
            <fieldset className="mt-7">
              <legend className="sr-only">输出顺序</legend>
              <SortablePageList
                thumbnailBase={task.input.thumbnail_base}
                pages={order.ordered}
                onMove={order.move}
                onRemove={togglePage}
                disabled={task.busy}
              />
              {order.reordered && (
                <p className="mt-2 text-xs text-brand-700">
                  已按你调整的顺序输出：结果第 1 页是原第 {order.ordered[0]} 页。
                </p>
              )}
            </fieldset>
          )}

          {blockingError && (
            <div className="mt-4">
              <Alert tone={selection.error ? 'error' : 'warning'}>{blockingError}</Alert>
            </div>
          )}

          {!blockingError && (
            <div className="mt-4">
              <Alert tone="info">
                {mode === 'delete'
                  ? `将删除 ${selected.length} 页，保留 ${pageCount - selected.length} 页，`
                  : `将保留 ${selected.length} 页，`}
                生成 <span className="font-medium">{outputName}</span>。
              </Alert>
            </div>
          )}

          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="order-2 text-xs text-slate-500 sm:order-1">
              共 {pageCount} 页 · 原文件 {formatBytes(task.input.size)}
            </p>
            <Button
              size="lg"
              onClick={run}
              disabled={!canStart}
              icon={<IconArrowRight className="h-4 w-4" />}
              className="order-1 sm:order-2"
            >
              生成新的 PDF
            </Button>
          </div>
        </div>
      )}
    </ToolPage>
  )
}
