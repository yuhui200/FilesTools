/**
 * 统一转换中心的词汇与分组（第七阶段）。
 *
 * 只依赖类型，不 import 任何 hook / 组件 —— 状态名与分组规则是**数据**，
 * 放在这里既能让页面与 hook 共用一份，也不会绕出模块环。
 */

import type {
  ConversionCapabilities,
  ConversionCapability,
  ConversionFormatOption,
  ConversionStatus,
} from '@/types'

import { fileExtension } from './format'

/** 单项状态的中文名。整批状态复用同一张表 */
const STATUS_LABELS: Record<ConversionStatus, string> = {
  queued: '排队中',
  processing: '转换中',
  // 不是「已取消」：活还没停。LibreOffice / OCR 跑在线程里，取消不了（§十五）
  cancelling: '正在取消',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

export function conversionStatusLabel(status: string): string {
  return STATUS_LABELS[status as ConversionStatus] ?? status
}

/**
 * 服务端阶段 → 中文。
 *
 * 键与 ``backend/services/progress.py`` 的 ``STAGE_*`` 一一对应。
 * 认不出的阶段返回 null —— 界面就不显示阶段，而不是把服务端的生词
 * 摆给用户看。
 */
const STAGE_LABELS: Record<string, string> = {
  analyzing: '正在分析文件…',
  detecting: '正在检测文字层…',
  extracting: '正在提取内容…',
  ocr: '正在识别文字…',
  writing: '正在生成结果…',
  verifying: '正在校验结果…',
}

export function conversionStageLabel(stage: string | null): string | null {
  if (!stage) return null
  return STAGE_LABELS[stage] ?? null
}

/** 整批是否已经定下来。与 services/taskRunner.ts 里轮询的终止条件一致 */
export function isConversionSettled(status: string): boolean {
  return status === 'completed' || status === 'failed' || status === 'cancelled'
}

/** 前端按扩展名分出来的一组：一种源格式的文件 + 它能选的那些能力 */
export interface LocalGroup {
  sourceType: string
  sourceLabel: string
  /** 这一种源格式的全部转换能力，推荐目标排在前面（服务端给的次序） */
  entries: ConversionCapability[]
  files: File[]
}

/** 进不了转换中心的文件，以及原因 */
export interface UnsupportedFile {
  file: File
  reason: string
}

/** 扩展名 -> 格式。与后端 registry 一样，.jpg 与 .jpeg 归为同一类 */
function formatIndex(capabilities: ConversionCapabilities): Map<string, ConversionFormatOption> {
  const index = new Map<string, ConversionFormatOption>()
  for (const format of capabilities.formats) {
    for (const extension of format.extensions) index.set(extension, format)
  }
  return index
}

/**
 * 一种源格式能选的全部能力，**推荐目标排在最前面**。
 *
 * 次序完全由服务端给的 ``conversions[]`` 决定（推荐项由 registry 打标签），
 * 前端不重新排序、也不自己算「哪些算推荐」—— 那就是 §十六 禁止的
 * 第二份能力矩阵。
 */
export function conversionsForSource(
  capabilities: ConversionCapabilities,
  sourceType: string,
): ConversionCapability[] {
  return capabilities.conversions.filter(
    (entry) => entry.source_type === sourceType && entry.available,
  )
}

/** 这一对 (源, 目标) 对应的能力；没有就是 null（同格式不是能力） */
export function capabilityFor(
  capabilities: ConversionCapabilities,
  sourceType: string,
  targetType: string,
): ConversionCapability | null {
  return (
    capabilities.conversions.find(
      (entry) => entry.source_type === sourceType && entry.target_type === targetType,
    ) ?? null
  )
}

/** 推荐的与其余的。分栏只是摆放，两栏都能选（§二十二） */
export function splitTargets(entries: ConversionCapability[]): {
  recommended: ConversionCapability[]
  others: ConversionCapability[]
} {
  const recommended: ConversionCapability[] = []
  const others: ConversionCapability[] = []
  for (const entry of entries) {
    ;(entry.tags.includes('recommended') ? recommended : others).push(entry)
  }
  return { recommended, others }
}

/** 一组默认选中哪一格：第一个推荐目标，没有推荐就第一个 */
export function defaultTargetOf(entries: ConversionCapability[]): string {
  const { recommended, others } = splitTargets(entries)
  return (recommended[0] ?? others[0])?.target_type ?? ''
}

/**
 * 按**扩展名**把文件分组。
 *
 * 这一步只是界面排版：提交之后服务端会按真实内容再判一次，声明与内容
 * 不符的文件会作为失败项回来（§五）。但用户在上传的当下就得看到
 * 「这几个能转、那个不行」，所以本地这一遍不能省。
 *
 * 前端不自己维护一份源格式清单 —— 能转什么完全由 capabilities 决定，
 * 服务器缺组件时这里自然也就分不出组了（§十九）。用 ``formats`` 认扩展名、
 * 用 ``conversions`` 认目标，两者都是服务端发的。
 */
export function groupFiles(
  files: File[],
  capabilities: ConversionCapabilities,
): { groups: LocalGroup[]; unsupported: UnsupportedFile[] } {
  const index = formatIndex(capabilities)
  const groups = new Map<string, LocalGroup>()
  const unsupported: UnsupportedFile[] = []

  for (const file of files) {
    const extension = fileExtension(file.name)
    const format = index.get(extension)
    const entries = format ? conversionsForSource(capabilities, format.value) : []
    if (!format || entries.length === 0) {
      unsupported.push({
        file,
        reason: extension
          ? `暂不支持 ${extension.replace('.', '').toUpperCase()} 格式`
          : '这个文件没有扩展名，无法判断格式',
      })
      continue
    }

    const existing = groups.get(format.value)
    if (existing) {
      existing.files.push(file)
      continue
    }
    groups.set(format.value, {
      sourceType: format.value,
      sourceLabel: format.label,
      entries,
      files: [file],
    })
  }

  return { groups: [...groups.values()], unsupported }
}

/** 格式的显示名。capabilities 里查不到就退回大写短名 */
export function conversionFormatLabel(
  capabilities: ConversionCapabilities,
  value: string,
): string {
  return capabilities.formats.find((item) => item.value === value)?.label ?? value.toUpperCase()
}

/** 目标格式的显示名（同 :func:`conversionFormatLabel`，这个名字用得更久） */
export function conversionTargetLabel(
  capabilities: ConversionCapabilities,
  target: string,
): string {
  return conversionFormatLabel(capabilities, target)
}

/**
 * 源格式属于哪一组（image / office / text / pdf）。
 *
 * 缩略图那一处靠它决定「要不要读图片尺寸」。组名也是服务器给的 ——
 * 前端不自己判断「哪些扩展名算图片」。用的是 ``groups`` 这份**投影**：
 * 它本来就是为界面分栏发布的，与 ``conversions[]`` 是同一份数据的两个视图。
 */
export function sourceGroupKey(
  capabilities: ConversionCapabilities,
  sourceType: string,
): string | null {
  for (const group of capabilities.groups) {
    if (group.sources.some((source) => source.value === sourceType)) return group.key
  }
  return null
}

/** 一种格式的分类（image / document / pdf），也来自服务端 */
export function categoryOf(
  capabilities: ConversionCapabilities,
  sourceType: string,
): string | null {
  return capabilities.formats.find((format) => format.value === sourceType)?.category ?? null
}
