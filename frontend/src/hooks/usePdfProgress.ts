import { useEffect, useState } from 'react'

import type { ProgressStep } from '@/components/ProgressPanel'

/**
 * PDF 处理的统一状态机。
 *
 * 第三阶段 §12 要求展示：等待处理 → 正在上传 → 正在分析文件 → 正在处理
 * → 正在生成文件 → 处理完成 / 处理失败。这里把它们落到一个枚举上，
 * 六个 PDF 页面共用，避免每个页面各写一套文案。
 *
 *   idle       刚进页面，什么文件都还没有          → 等待处理
 *   uploading  正在把文件传给服务器（真实百分比）  → 正在上传
 *   ready      上传完成，服务器已认出这份文件      → 等待处理
 *   working    正在处理（不确定态，无百分比）      → 正在分析文件 / 正在处理 / 正在生成文件
 *   done       处理完成                            → 处理完成
 *
 * 「正在分析文件」是真实的：上传结束时服务器已经打开并校验过文件，
 * 前端这时才知道页数。**不编造百分比** —— 只有上传阶段有真实进度，
 * 处理阶段显示的是不确定态进度条，中间几条文案按 §12 的顺序轮播。
 */
export type PdfStage = 'idle' | 'uploading' | 'ready' | 'working' | 'done'

export const PDF_PROGRESS_STEPS: ProgressStep[] = [
  { key: 'upload', label: '上传文件' },
  { key: 'analyze', label: '分析文件' },
  { key: 'process', label: '处理文件' },
  { key: 'result', label: '生成文件' },
]

/** 处理阶段的文案，顺序即 §12 的排列 */
const WORKING_HINTS = ['正在分析文件…', '正在处理…', '正在生成文件…']

/**
 * 一组状态条文案。
 *
 * 默认是 PDF 工具用的那套；文档转换传自己的那一组（「转换文件」而不是
 * 「处理文件」）—— 用户看到的是自己正在做的事。两组都是四步，
 * 索引关系由 ``steps.length`` 推出，不写死 4。
 */
export interface PdfProgressLabels {
  steps: ProgressStep[]
  /** 处理阶段轮播的文案，顺序与 §12 一致 */
  workingHints: string[]
}

export const PDF_PROGRESS_LABELS: PdfProgressLabels = {
  steps: PDF_PROGRESS_STEPS,
  workingHints: WORKING_HINTS,
}

const HINT_INTERVAL_MS = 1800

export interface PdfProgress {
  statusText: string
  /** 0-100；null 表示进度未知 */
  percent: number | null
  activeStep: number
}

export function usePdfProgress(
  stage: PdfStage,
  uploadPercent: number,
  uploadingText = '正在上传文件…',
  labels: PdfProgressLabels = PDF_PROGRESS_LABELS,
): PdfProgress {
  const [hintIndex, setHintIndex] = useState(0)
  const { steps, workingHints } = labels

  useEffect(() => {
    if (stage !== 'working') {
      setHintIndex(0)
      return
    }
    const timer = window.setInterval(() => {
      setHintIndex((index) => Math.min(index + 1, workingHints.length - 1))
    }, HINT_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [stage, workingHints.length])

  // ProgressPanel 的规则是「下标小于 activeStep 的算已完成，等于的算进行中」，
  // 上传与分析在服务端返回结果时就已经真实完成，所以从这里开始往后数。
  // 用长度推出下标，换一组文案不会指错格子（默认那组算出来仍是原来的 2 和 3）。
  const afterUploadStep = Math.min(2, steps.length - 1)
  const lastWorkingStep = Math.max(afterUploadStep, steps.length - 1)

  switch (stage) {
    case 'uploading':
      return { statusText: uploadingText, percent: uploadPercent, activeStep: 0 }
    case 'ready':
    case 'idle':
      // 上传与分析都已经真实完成，接下来等用户点按钮（此时不显示「进行中」）
      return { statusText: '等待处理', percent: null, activeStep: afterUploadStep }
    case 'working':
      return {
        statusText: workingHints[hintIndex] ?? '正在处理…',
        percent: null,
        activeStep:
          hintIndex >= workingHints.length - 1 ? lastWorkingStep : afterUploadStep,
      }
    case 'done':
      return { statusText: '处理完成', percent: 100, activeStep: steps.length }
  }
}
