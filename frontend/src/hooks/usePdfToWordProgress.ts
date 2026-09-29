import { useEffect, useState } from 'react'

import { getPdfToWordProgress } from '@/services/api'
import type { PdfToWordProgress } from '@/types'

import {
  usePdfProgress,
  type PdfProgress,
  type PdfProgressLabels,
  type PdfStage,
} from './usePdfProgress'

/**
 * PDF 转 Word 的状态条（第六阶段 A 规格 §进度）。
 *
 * 规格要求显示的是**真实阶段**：「正在分析 PDF... / 正在检测文字层... /
 * 正在提取内容... / 正在进行 OCR... / 正在生成 Word... / 正在验证 Word...」，
 * 并且明确「不要使用假的固定进度。如果无法得到准确百分比：使用阶段状态」。
 *
 * 所以这里的进度有两个来源，服务端的优先：
 * 1. 服务端 ``GET /api/office/pdf-to-word/progress/{id}`` 报回来的真实阶段
 *    （后台线程每推进一步就写一次）；
 * 2. 拿不到时退回本地按 §12 顺序轮播的阶段文案 —— 它仍然是阶段状态，
 *    不是编出来的百分比。
 */
export const PDF_TO_WORD_PROGRESS_LABELS: PdfProgressLabels = {
  steps: [
    { key: 'upload', label: '上传文件' },
    { key: 'analyze', label: '分析 PDF' },
    { key: 'extract', label: '提取内容' },
    { key: 'write', label: '生成 Word' },
  ],
  // 拿不到服务端阶段时的兜底轮播，顺序就是实际发生的顺序
  workingHints: ['正在分析 PDF…', '正在提取内容…', '正在生成 Word…'],
}

/**
 * 服务端阶段 → 状态条上的文案与步骤下标。
 *
 * 步骤下标是「正在进行的那一格」：0 上传文件、1 分析 PDF、2 提取内容、
 * 3 生成 Word。检测文字层属于「分析 PDF」，验证属于「生成 Word」的收尾。
 *
 * 键与 ``backend/services/progress.py`` 的 STAGE_* 一一对应；
 * 出现前端不认识的阶段时退回本地轮播，不会显示成空白。
 */
const SERVER_STAGES: Record<string, { text: string; step: number }> = {
  analyzing: { text: '正在分析 PDF…', step: 1 },
  detecting: { text: '正在检测文字层…', step: 1 },
  extracting: { text: '正在提取内容…', step: 2 },
  ocr: { text: '正在进行 OCR…', step: 2 },
  writing: { text: '正在生成 Word…', step: 3 },
  verifying: { text: '正在验证 Word…', step: 3 },
}

/** 轮询间隔。OCR 一页约两秒，一秒一次足够跟上，也不会给服务端添负担 */
const POLL_INTERVAL_MS = 1000

export function usePdfToWordProgress(
  stage: PdfStage,
  uploadPercent: number,
  progressId: string | null,
): PdfProgress {
  const local = usePdfProgress(
    stage,
    uploadPercent,
    '正在上传 PDF…',
    PDF_TO_WORD_PROGRESS_LABELS,
  )
  const [server, setServer] = useState<PdfToWordProgress | null>(null)

  // 转换没在跑（或没拿到 id）时不轮询
  const polling = stage === 'working' && progressId !== null

  useEffect(() => {
    setServer(null)
    if (!polling || !progressId) return

    const controller = new AbortController()
    let stopped = false

    const tick = async () => {
      while (!stopped) {
        try {
          const snapshot = await getPdfToWordProgress(progressId, controller.signal)
          if (stopped) return
          setServer(snapshot)
        } catch {
          // 进度只是锦上添花：查不到（转换已结束、条目已过期、id 不合法）
          // 就退回本地的阶段轮播，绝不让它影响正在跑的这次转换。
          return
        }
        await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS))
      }
    }

    void tick()

    return () => {
      stopped = true
      controller.abort()
    }
  }, [polling, progressId])

  if (stage !== 'working' || !server) return local

  const mapped = SERVER_STAGES[server.stage]
  if (!mapped) return local

  const { page, page_count: pageCount } = server
  // OCR 期间能诚实算出一个百分比：页面是**按顺序**处理的，
  // 「已处理页数 / 总页数」就是这次转换真实完成了多少，不是估的。
  const percent =
    server.stage === 'ocr' && page !== null && pageCount !== null && pageCount > 0
      ? (page / pageCount) * 100
      : null

  return {
    statusText:
      server.stage === 'ocr' && page !== null && pageCount !== null
        ? `${mapped.text}（第 ${page} / 共 ${pageCount} 页）`
        : mapped.text,
    percent,
    activeStep: mapped.step,
  }
}
