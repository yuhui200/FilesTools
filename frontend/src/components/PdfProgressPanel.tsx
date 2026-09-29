import type { PdfProgress, PdfProgressLabels, PdfStage } from '@/hooks/usePdfProgress'

import { PDF_PROGRESS_LABELS } from '@/hooks/usePdfProgress'

import { ProgressPanel } from './ProgressPanel'

interface PdfProgressPanelProps {
  stage: PdfStage
  progress: PdfProgress
  /** 页面上是否已经有选好的文件 */
  hasFiles: boolean
  /** 状态条的四个步骤文案；默认是 PDF 工具那一套，文档转换传自己的一组 */
  labels?: PdfProgressLabels
}

/**
 * 六个 PDF 页面共用的状态条（§12）。
 *
 * 什么时候出现：
 *   - 「正在上传」：真的在传，显示真实百分比；
 *   - 「正在分析文件 / 正在处理 / 正在生成文件」：不确定态滚动条；
 *   - 「等待处理」：文件已经就绪、等用户点按钮 —— 这一条以前只算不显示，
 *     用户看不到自己处在哪一步，所以补上；
 *   - 处理完成由结果面板负责，这里不重复。
 */
export function PdfProgressPanel({
  stage,
  progress,
  hasFiles,
  labels = PDF_PROGRESS_LABELS,
}: PdfProgressPanelProps) {
  const busy = stage === 'uploading' || stage === 'working'
  const waiting = stage === 'ready' || (stage === 'idle' && hasFiles)

  if (!busy && !waiting) return null

  return (
    <ProgressPanel
      statusText={progress.statusText}
      percent={busy ? progress.percent : null}
      steps={labels.steps}
      activeStep={progress.activeStep}
      waiting={!busy}
    />
  )
}
