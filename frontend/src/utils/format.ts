/** 格式化与解析工具。 */

const UNITS = ['B', 'KB', 'MB', 'GB'] as const

/** 把字节数格式化成人类可读的字符串，例如 1.92 MB */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${Math.round(bytes)} B`

  let value = bytes
  let unitIndex = 0
  while (value >= 1024 && unitIndex < UNITS.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value.toFixed(2)} ${UNITS[unitIndex]}`
}

/** 保留一位小数的百分比 */
export function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return '—'
  return `${value.toFixed(1)}%`
}

/** 格式化图片尺寸，例如 1600 × 1200 */
export function formatDimensions(width: number | null, height: number | null): string {
  if (!width || !height) return '—'
  return `${width} × ${height}`
}

/** 格式化成大写短名，例如 JPEG */
export function formatLabel(format: string | null): string {
  if (!format) return '—'
  return format.toUpperCase()
}

/**
 * 把时间戳格式化成「2026-09-27 14:32」。
 *
 * 自己补零而不用 toLocaleString：不同浏览器 / 系统的本地化格式差别很大，
 * 「最近处理」的表里需要一个宽度稳定的写法。
 */
export function formatDateTime(at: number): string {
  if (!Number.isFinite(at)) return '—'
  const date = new Date(at)
  const pad = (value: number) => String(value).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

/**
 * 把「数值 + 单位」换算成字节数。
 * 校验失败时返回 null，由调用方决定如何提示。
 */
export function toBytes(value: number, unit: 'KB' | 'MB'): number | null {
  if (!Number.isFinite(value) || value <= 0) return null
  const factor = unit === 'MB' ? 1024 * 1024 : 1024
  return Math.round(value * factor)
}

/** 从文件名里取出扩展名（小写，含点）。 */
export function fileExtension(filename: string): string {
  const index = filename.lastIndexOf('.')
  if (index < 0) return ''
  return filename.slice(index).toLowerCase()
}

/** 把文件大小限制的字节数转成「不超过 N」的可读文案。 */
export function describeTarget(bytes: number): string {
  return `不超过 ${formatBytes(bytes)}`
}

/** 截断过长的文件名，保留扩展名。 */
export function truncateFilename(name: string, max = 42): string {
  if (name.length <= max) return name
  const dot = name.lastIndexOf('.')
  if (dot <= 0) return `${name.slice(0, max - 1)}…`
  const ext = name.slice(dot)
  const stem = name.slice(0, dot)
  const keep = Math.max(6, max - ext.length - 1)
  return `${stem.slice(0, keep)}…${ext}`
}
