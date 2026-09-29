import { useCallback, useMemo, useState } from 'react'

import { Alert } from '@/components/Alert'
import { Button } from '@/components/Button'
import { Dropzone } from '@/components/Dropzone'
import { IconArrowRight, IconPlus, IconTrash } from '@/components/Icons'
import { PdfFileCard } from '@/components/PdfFileCard'
import { PdfProgressPanel } from '@/components/PdfProgressPanel'
import { PdfResultPanel, type ResultStat } from '@/components/PdfResultPanel'
import { RadioGroup, type RadioOption } from '@/components/RadioGroup'
import { SortablePageList } from '@/components/SortablePageList'
import { ToolPage } from '@/components/ToolPage'
import { usePageOrder } from '@/hooks/usePageOrder'
import { usePdfInput } from '@/hooks/usePdfInput'
import { usePdfProgress } from '@/hooks/usePdfProgress'
import { useServerConfig } from '@/hooks/useServerConfig'
import { splitPdf } from '@/services/api'
import type { PdfSplitMode } from '@/types'
import { formatBytes } from '@/utils/format'
import { formatPageList, formatPageSequence, parsePageSelection } from '@/utils/pdf'

const MODE_OPTIONS: RadioOption<PdfSplitMode>[] = [
  { value: 'every', label: '每页一个 PDF', hint: '每页拆成单独文件，自动打包 ZIP' },
  { value: 'ranges', label: '按范围拆分', hint: '一行一个范围，一行一个文件' },
  {
    value: 'selected',
    label: '自定义页面',
    hint: '只挑出选中的页面合成一份 PDF，可拖动调整先后顺序',
  },
]

export function PdfSplit() {
  const { config, offlineMessage } = useServerConfig()

  const [mode, setMode] = useState<PdfSplitMode>('every')
  const [rows, setRows] = useState<string[]>([''])
  const [selectedText, setSelectedText] = useState('')

  const rules = useMemo(
    () => ({
      allowedExtensions: config.allowed_pdf_extensions,
      maxBytes: config.max_upload_bytes,
    }),
    [config.allowed_pdf_extensions, config.max_upload_bytes],
  )

  const task = usePdfInput(rules, { label: 'PDF 拆分' })
  const progress = usePdfProgress(task.stage, task.uploadPercent, '正在上传 PDF…')

  const pageCount = task.input?.page_count ?? 0

  /** 每个范围单独校验：报错文案与服务端一致 */
  const parsedRows = useMemo(
    () => rows.map((row) => ({ row, ...parsePageSelection(row, pageCount) })),
    [rows, pageCount],
  )

  const selected = useMemo(
    () => parsePageSelection(selectedText, pageCount),
    [selectedText, pageCount],
  )

  /** 自定义页面模式下用户排定的输出顺序（§11） */
  const order = usePageOrder(mode === 'selected' ? selected.pages : [])

  const filledRows = parsedRows.filter((item) => item.row.trim() !== '')
  const rowsError =
    parsedRows.find((item) => item.row.trim() !== '' && item.error)?.error ?? null
  const rowsEmpty = filledRows.length === 0

  /** 拆分依据（发给服务端的文本） */
  const splitValue = useMemo(() => {
    if (mode === 'every') return null
    // 自定义页面的页序由用户决定，按列表顺序发送（§11）
    if (mode === 'selected') return formatPageSequence(order.ordered, pageCount)
    return filledRows.map((item) => item.row.trim()).join('\n')
  }, [mode, order.ordered, pageCount, filledRows])

  const modeError = useMemo(() => {
    if (mode === 'every') return null
    if (mode === 'selected') {
      if (selectedText.trim() === '') return '请填写要提取的页码，例如：1,3,5,8。'
      return selected.error
    }
    if (rowsEmpty) return '请至少填写一个页面范围，例如：1-5。'
    return rowsError
  }, [mode, selectedText, selected.error, rowsEmpty, rowsError])

  /** 拆成几份：每页一个就是页数，按范围就是填写了几行 */
  const partCount = mode === 'every' ? pageCount : mode === 'selected' ? 1 : filledRows.length

  const run = useCallback(
    () => task.run((inputId) => splitPdf({ inputId, mode, value: splitValue })),
    [task, mode, splitValue],
  )

  const canStart = task.stage === 'ready' && !modeError && partCount > 0

  const addRow = () => setRows((current) => [...current, ''])
  const updateRow = (index: number, value: string) =>
    setRows((current) => current.map((row, position) => (position === index ? value : row)))
  const removeRow = (index: number) =>
    setRows((current) =>
      current.length <= 1 ? [''] : current.filter((_, position) => position !== index),
    )

  const result = task.result
  const stats: ResultStat[] = result
    ? [
        { label: '拆出文件', value: `${result.files.length} 个`, tone: 'brand' },
        {
          label: '原文件页数',
          value: result.original_pages === null ? '—' : `${result.original_pages} 页`,
        },
        { label: '下载形式', value: result.archived ? 'ZIP 打包' : '单个文件' },
        { label: '合计大小', value: formatBytes(result.size) },
      ]
    : []

  return (
    <ToolPage
      category="pdf"
      title="PDF 拆分"
      description="把一个 PDF 拆成多份：每页一个文件、按范围分几份，或者只挑出需要的页面。"
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
          resetLabel="换个方式再拆"
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
            <legend className="text-sm font-medium text-slate-900">拆分方式</legend>
            <div className="mt-3">
              <RadioGroup
                name="split-mode"
                value={mode}
                options={MODE_OPTIONS}
                onChange={setMode}
                disabled={task.busy}
              />
            </div>
          </fieldset>

          {mode === 'every' && (
            <div className="mt-5">
              <Alert tone="info">
                这份 PDF 共 {pageCount} 页，将拆成 {pageCount} 个 PDF（part-01.pdf、part-02.pdf …），
                自动打包成 {task.input.filename.replace(/\.pdf$/i, '')}_parts.zip 下载。
              </Alert>
            </div>
          )}

          {mode === 'ranges' && (
            <fieldset className="mt-7">
              <legend className="text-sm font-medium text-slate-900">每份的页面范围</legend>
              <p className="mt-1 text-xs text-slate-500">
                一行一个范围，一行生成一个文件。例如填 1-5、6-10、11-20，会得到
                part-01.pdf、part-02.pdf、part-03.pdf。同一行里也可以用逗号把不连续的页面
                放在一起（1-3,7 表示这一份包含第 1、2、3、7 页）。
              </p>

              <div className="mt-3 space-y-2">
                {rows.map((row, index) => {
                  const parsed = parsedRows[index]
                  const empty = row.trim() === ''
                  const pages = parsed?.pages ?? []
                  const rowError = parsed?.error ?? null
                  return (
                    <div key={index}>
                      <div className="flex items-center gap-2">
                        <span className="w-16 shrink-0 text-xs font-medium tabular-nums text-slate-500">
                          第 {index + 1} 份
                        </span>
                        <input
                          type="text"
                          value={row}
                          disabled={task.busy}
                          onChange={(event) => updateRow(index, event.target.value)}
                          placeholder={`例如 1-${Math.min(5, Math.max(1, pageCount))}`}
                          aria-label={`第 ${index + 1} 份的页面范围`}
                          className="field flex-1"
                        />
                        <button
                          type="button"
                          onClick={() => removeRow(index)}
                          disabled={task.busy}
                          aria-label={`删除第 ${index + 1} 份`}
                          className="grid h-9 w-9 shrink-0 place-items-center rounded-lg text-slate-400 transition hover:bg-red-50 hover:text-red-600 disabled:opacity-40"
                        >
                          <IconTrash className="h-4 w-4" />
                        </button>
                      </div>
                      {!empty && !rowError && (
                        <p className="ml-[4.5rem] mt-1 text-xs text-slate-500">
                          含第 {formatPageList(pages)} 页，共 {pages.length} 页
                        </p>
                      )}
                    </div>
                  )
                })}
              </div>

              <div className="mt-3">
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={addRow}
                  disabled={task.busy || rows.length >= 50}
                  icon={<IconPlus className="h-3.5 w-3.5" />}
                >
                  再加一份
                </Button>
              </div>
            </fieldset>
          )}

          {mode === 'selected' && (
            <fieldset className="mt-7">
              <legend className="text-sm font-medium text-slate-900">要保留的页面</legend>
              <p className="mt-1 text-xs text-slate-500">
                只保留这里填写的页面，合成一份 selected-pages.pdf。
              </p>
              <div className="mt-3">
                <input
                  type="text"
                  value={selectedText}
                  disabled={task.busy}
                  onChange={(event) => setSelectedText(event.target.value)}
                  placeholder="例如 1,3,5,8"
                  aria-label="要保留的页面"
                  className="field w-full sm:max-w-sm"
                />
                {!selected.error && selected.pages.length > 0 && (
                  <p className="mt-2 text-xs text-slate-500">
                    将保留第 {formatPageList(selected.pages)} 页，共 {selected.pages.length} 页
                  </p>
                )}
              </div>

              {selected.pages.length > 0 && (
                <div className="mt-5">
                  <SortablePageList
                    thumbnailBase={task.input.thumbnail_base}
                    pages={order.ordered}
                    onMove={order.move}
                    onRemove={(page) =>
                      setSelectedText(
                        formatPageList(selected.pages.filter((item) => item !== page)),
                      )
                    }
                    disabled={task.busy}
                  />
                  {order.reordered && (
                    <p className="mt-2 text-xs text-brand-700">
                      已按你调整的顺序输出：结果第 1 页是原第 {order.ordered[0]} 页。
                    </p>
                  )}
                </div>
              )}
            </fieldset>
          )}

          {modeError && (
            <div className="mt-4">
              <Alert tone="error">{modeError}</Alert>
            </div>
          )}

          {!modeError && partCount > 1 && (
            <p className="mt-4 text-xs text-slate-500">
              将拆成 {partCount} 个文件，结果会自动打包成 ZIP 下载。
            </p>
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
              开始拆分
            </Button>
          </div>
        </div>
      )}
    </ToolPage>
  )
}
