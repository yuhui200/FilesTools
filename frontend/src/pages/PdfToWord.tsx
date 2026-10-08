import { useCallback, useMemo, useState } from 'react'

import { Alert } from '@/components/Alert'
import { Button } from '@/components/Button'
import { Dropzone } from '@/components/Dropzone'
import { IconArrowRight } from '@/components/Icons'
import { PdfFileCard } from '@/components/PdfFileCard'
import { PdfProgressPanel } from '@/components/PdfProgressPanel'
import { PdfResultPanel, type ResultStat } from '@/components/PdfResultPanel'
import { ToolPage } from '@/components/ToolPage'
import type { PdfSubmit } from '@/hooks/usePdfMultiTask'
import { usePdfMultiTask } from '@/hooks/usePdfMultiTask'
import {
  PDF_TO_WORD_PROGRESS_LABELS,
  usePdfToWordProgress,
} from '@/hooks/usePdfToWordProgress'
import { useServerConfig } from '@/hooks/useServerConfig'
import { newProgressId, pdfToWord } from '@/services/api'
import type { DocumentResultResponse } from '@/types'
import { explainError } from '@/utils/errorMessages'
import { formatBytes } from '@/utils/format'
import { STALE_BACKEND_MESSAGE, featureAvailability } from '@/utils/serverFeature'

/**
 * 转换质量的两条声明（第六阶段 A 规格原话）。
 *
 * 常驻在页面顶部，而不是只在结果页出现：用户是在**决定要不要用这个工具**、
 * 以及**拿到结果判断它转得对不对**的时候需要知道它做不到什么。
 * 只放在上传前，转完看到排版变了就没有解释了。
 */
const QUALITY_DISCLAIMERS = [
  'PDF → Word 无法保证 100% 还原原始排版。',
  '复杂表格、特殊字体、图片和多栏布局可能出现排版差异。',
]

export function PdfToWord() {
  const { config, offlineMessage } = useServerConfig()

  /**
   * 这次转换的进度 id。
   *
   * 由前端生成、随请求一起发出去，服务端只校验形状。存成 state 而不是 ref：
   * 它变了要触发重新轮询，而且必须是**这一次**转换的 id，不能跨次复用。
   */
  const [progressId, setProgressId] = useState<string | null>(null)

  const rules = useMemo(
    () => ({
      allowedExtensions: config.allowed_pdf_extensions,
      maxBytes: config.max_upload_bytes,
    }),
    [config.allowed_pdf_extensions, config.max_upload_bytes],
  )

  const submit: PdfSubmit<DocumentResultResponse> = useCallback((files, options) => {
    const [file] = files
    if (!file) throw new Error('没有可转换的文件，请重新选择')
    const id = newProgressId()
    setProgressId(id)
    return pdfToWord({ file, progressId: id, ...options })
  }, [])

  const task = usePdfMultiTask<DocumentResultResponse>({
    maxFiles: 1,
    maxTotalBytes: config.max_upload_bytes,
    rules,
    kind: 'PDF 文件',
    label: 'PDF 转 Word',
    submit,
  })

  const progress = usePdfToWordProgress(task.stage, task.uploadPercent, progressId)

  /**
   * 缺 DOCX 组件才是「这个功能整个用不了」，上传前就得说清楚。
   *
   * 但**「后端没给这个字段」不等于「服务器没装组件」**：前者是后端比前端旧，
   * 连 `/api/office/pdf-to-word` 这个接口都没有。两种情况都该挡住上传，
   * 但要说清楚是哪一种 —— 否则用户会去找一个并不存在的缺失组件。
   */
  const docxAvailability = featureAvailability(config.pdf_to_word_available)
  const unavailable = docxAvailability === 'unavailable'
  const staleBackend = docxAvailability === 'unknown'
  /** 两种情况都不能开始：接口真的不在，传上去也只会失败 */
  const docxBlocked = unavailable || staleBackend

  /** OCR 同样是三态。`unknown` 时不能声称「未安装 OCR 组件」 */
  const ocrAvailability = featureAvailability(config.ocr_available)

  const file = task.files[0] ?? null
  const result = task.result

  /**
   * 结果页要如实说清「内容是怎么来的」。
   *
   * 页数是源 PDF 的页数（不是产出 DOCX 的页数 —— DOCX 没有固定页数，
   * 分页由读者的 Word 决定）；识别方式直接用服务端给的中文，
   * 前端不自己拼，免得同一件事有两种说法。
   */
  const stats: ResultStat[] = result
    ? [
        {
          label: '类型',
          value: result.document_kind_label,
          tone: result.document_kind === 'text' ? 'muted' : 'brand',
        },
        {
          label: '页数',
          value: `${result.original_pages ?? result.page_count ?? '—'} 页`,
        },
        {
          label: '识别方式',
          value: result.extraction_label,
          tone: result.ocr_pages > 0 ? 'brand' : 'muted',
        },
        { label: '输出大小', value: formatBytes(result.size) },
      ]
    : []

  const explanation = task.error ? explainError(task.errorCode, task.error) : null
  const canStart = !docxBlocked && !task.busy && file !== null

  /** 用了 OCR 才提示页数上限，纯文字版 PDF 不受这个上限约束 */
  const ocrPageLimit = config.pdf_to_word_max_ocr_pages

  return (
    <ToolPage
      category="pdf"
      title="PDF 转 Word"
      description="把 PDF 转成可编辑的 .docx。文字版 PDF 直接提取文字层，扫描件自动逐页识别；原始 PDF 在转换完成后立即从服务器删除。"
      offlineMessage={offlineMessage}
    >
      {unavailable && (
        <Alert tone="warning">当前服务器缺少 Word 生成组件，请联系管理员。</Alert>
      )}

      {/* 后端比前端旧：接口根本不存在，和「没装组件」是两回事，别混着说 */}
      {staleBackend && <Alert tone="warning">{STALE_BACKEND_MESSAGE}</Alert>}

      {/* 缺 OCR 只挡扫描件，文字版 PDF 照常可用 —— 所以是提示，不是禁用。
          只在后端**明确说没有**时才这么讲；字段缺失时我们并不知道实情。 */}
      {!docxBlocked && ocrAvailability === 'unavailable' && (
        <Alert tone="warning">
          当前服务器未安装 OCR 组件，暂时无法处理扫描 PDF。文字版 PDF 仍可正常转换。
        </Alert>
      )}

      {/* 能力边界常驻页面上，不藏在结果页里 */}
      {!docxBlocked && (
        <Alert tone="info">
          {QUALITY_DISCLAIMERS.map((text) => (
            <p key={text}>{text}</p>
          ))}
        </Alert>
      )}

      {explanation && (
        <Alert tone="error" onDismiss={task.dismissError}>
          <p className="font-medium">{explanation.title}</p>
          {explanation.hint && <p className="mt-0.5 text-xs">{explanation.hint}</p>}
        </Alert>
      )}

      {!file && (
        <Dropzone
          onSelect={(files) => task.addFiles(files)}
          onError={task.reportError}
          rules={rules}
          kind="PDF 文件"
          // 接口不存在时也不让传：传上去只会拿一个 404/405 回来
          disabled={docxBlocked}
          hint={`支持 ${rules.allowedExtensions.join(' / ')}，单个最大 ${formatBytes(
            config.max_upload_bytes,
          )}`}
        />
      )}

      <PdfProgressPanel
        stage={task.stage}
        progress={progress}
        hasFiles={file !== null}
        labels={PDF_TO_WORD_PROGRESS_LABELS}
      />

      {file && !task.busy && !result && (
        <div className="card animate-fade-in-up p-5 sm:p-6">
          <PdfFileCard
            filename={file.name}
            size={file.size}
            onRemove={task.clearFiles}
            disabled={task.busy}
          />

          <div className="mt-6">
            <Alert tone="info">
              {file.name} · {formatBytes(file.size)}，将转换成 Word 文档（.docx）。
              {/* 只有后端**明确说 OCR 可用**时才敢承诺扫描件能转 */}
              {ocrAvailability === 'available'
                ? `文字版 PDF 直接提取，通常几秒；扫描件需要逐页识别，约两秒一页，一次最多识别 ${ocrPageLimit} 页。`
                : '文字版 PDF 直接提取文字层，通常几秒完成。'}
            </Alert>
          </div>

          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="order-2 text-xs text-slate-500 sm:order-1">
              转换完成后原始 PDF 会立即从服务器删除
            </p>
            <Button
              size="lg"
              onClick={task.start}
              disabled={!canStart}
              icon={<IconArrowRight className="h-4 w-4" />}
              className="order-1 sm:order-2"
            >
              开始转换
            </Button>
          </div>
        </div>
      )}

      {result && (
        <PdfResultPanel
          result={result}
          stats={stats}
          doneTitle="✓ 转换完成"
          downloadLabel="下载 Word"
          onDownload={task.download}
          onReset={task.reset}
          resetLabel="重新转换"
          onRestart={task.clearFiles}
          restartLabel="换一份 PDF"
          downloading={task.downloading}
          downloadError={task.downloadError}
          savedPath={task.savedPath}
          expired={task.resultExpired}
        >
          <p className="mt-4 text-xs text-slate-500">
            转换自 <span className="break-all font-medium text-slate-700">{file?.name}</span>
            {result.ocr_pages > 0 && (
              <>
                {' · '}
                {result.text_pages} 页直接提取、{result.ocr_pages} 页由 OCR 识别
                {result.ocr_languages.length > 0 &&
                  `（字符集 ${result.ocr_languages.join(' / ')}）`}
              </>
            )}
          </p>
        </PdfResultPanel>
      )}
    </ToolPage>
  )
}
