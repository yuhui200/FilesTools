import { useCallback, useMemo } from 'react'

import { Alert } from '@/components/Alert'
import { Button } from '@/components/Button'
import { Dropzone } from '@/components/Dropzone'
import { IconArrowRight } from '@/components/Icons'
import { PdfProgressPanel } from '@/components/PdfProgressPanel'
import { PdfResultPanel, type ResultStat } from '@/components/PdfResultPanel'
import { SortableFileList } from '@/components/SortableFileList'
import { ToolPage } from '@/components/ToolPage'
import { usePdfMultiTask } from '@/hooks/usePdfMultiTask'
import { usePdfProgress } from '@/hooks/usePdfProgress'
import { useServerConfig } from '@/hooks/useServerConfig'
import { mergePdfs } from '@/services/api'
import { formatBytes } from '@/utils/format'

/**
 * PDF 合并（§6）。
 *
 * 合并的价值全在顺序上，所以这里把「拖动排序 + 上移 / 下移 / 删除」
 * 放在最显眼的位置，并且按用户排定的顺序原样提交，服务端不会重新排序。
 */
export function PdfMerge() {
  const { config, offlineMessage } = useServerConfig()

  const rules = useMemo(
    () => ({
      allowedExtensions: config.allowed_pdf_extensions,
      maxBytes: config.max_upload_bytes,
    }),
    [config.allowed_pdf_extensions, config.max_upload_bytes],
  )

  const submit = useCallback(
    (
      files: File[],
      params: { onUploadProgress: (ratio: number) => void; signal: AbortSignal },
    ) => mergePdfs({ files, ...params }),
    [],
  )

  const task = usePdfMultiTask({
    maxFiles: config.max_batch_files,
    maxTotalBytes: config.max_batch_total_bytes,
    rules,
    kind: '文件',
    label: 'PDF 合并',
    submit,
  })

  const progress = usePdfProgress(
    task.stage,
    task.uploadPercent,
    `正在上传 ${task.files.length} 个文件…`,
  )

  const entries = task.files.map((file) => ({ file, previewUrl: null }))

  const result = task.result
  const stats: ResultStat[] = result
    ? [
        { label: '来源文件', value: `${task.files.length} 个` },
        {
          label: '合并后页数',
          value: result.page_count === null ? '—' : `${result.page_count} 页`,
          tone: 'brand',
        },
        { label: '文件大小', value: formatBytes(result.size) },
        {
          label: '原文件合计',
          value: result.original_size === null ? '—' : formatBytes(result.original_size),
        },
      ]
    : []

  const canStart = task.files.length > 0 && !task.busy

  return (
    <ToolPage
      category="pdf"
      title="PDF 合并"
      description={`把多个 PDF 按你排定的顺序合并成一个 merged.pdf，一次最多 ${config.max_batch_files} 个文件。`}
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
          kind="文件"
          hint={`支持 PDF 文件，单个最大 ${formatBytes(config.max_upload_bytes)}，最多 ${config.max_batch_files} 个`}
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
          resetLabel="调整顺序"
          onRestart={task.clearFiles}
          downloading={task.downloading}
          downloadError={task.downloadError}
        />
      )}

      {task.files.length > 0 && task.stage === 'idle' && (
        <div className="card animate-fade-in-up p-5 sm:p-6">
          <SortableFileList
            entries={entries}
            onRemove={task.removeFile}
            onMove={task.moveFile}
            disabled={task.busy}
            unitLabel="个"
            hint="拖动左侧抓手可以调整顺序，合并后的页序就是这个顺序。"
          />

          {task.files.length < config.max_batch_files && (
            <div className="mt-4">
              <Dropzone
                compact
                multiple
                onSelect={task.addFiles}
                onError={task.reportError}
                rules={rules}
                kind="文件"
                hint="可以继续添加，也可以直接把 PDF 拖到这里"
              />
            </div>
          )}

          {task.files.length === 1 && (
            <div className="mt-4">
              <Alert tone="info">
                只选了一个文件，合并后就是它本身。再添加几个文件才有合并的意义。
              </Alert>
            </div>
          )}

          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="order-2 text-xs text-slate-500 sm:order-1">
              {task.files.length} 个文件 · 合计{' '}
              {formatBytes(task.files.reduce((sum, file) => sum + file.size, 0))}
            </p>
            <Button
              size="lg"
              onClick={task.start}
              disabled={!canStart}
              icon={<IconArrowRight className="h-4 w-4" />}
              className="order-1 sm:order-2"
            >
              合并 PDF
            </Button>
          </div>
        </div>
      )}
    </ToolPage>
  )
}
