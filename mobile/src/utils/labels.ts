/**
 * 界面上反复出现的几段「人话」：状态名、文件大小、相对时间。
 *
 * 放在一处是因为它们都必须**前后一致**：同一个 `processing` 在一处写
 * 「处理中」、另一处写「转换中」，用户会以为是两种状态。
 */

import type { ConversionStatus } from '@/types'

/**
 * 状态的中文名。
 *
 * `cancelling` 单独一档是**必须的**：服务端的取消是协作式的，
 * 点了取消之后那个正在跑的文件停不下来，状态会如实停在 `cancelling`
 * 直到跑完。把它并进 `cancelled` 就是对用户撒谎。
 */
export const STATUS_LABELS: Record<ConversionStatus, string> = {
  queued: '等待中',
  processing: '处理中',
  cancelling: '正在取消',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

export type StatusTone = 'muted' | 'primary' | 'success' | 'danger' | 'warning'

export function statusTone(status: ConversionStatus): StatusTone {
  switch (status) {
    case 'completed':
      return 'success'
    case 'failed':
      return 'danger'
    case 'processing':
    case 'cancelling':
      return 'primary'
    default:
      return 'muted'
  }
}

/** 终态。到了这几个状态就不用再问了 */
export function isSettled(status: ConversionStatus | string): boolean {
  return status === 'completed' || status === 'failed' || status === 'cancelled'
}

/** 字节数 → 「1.2 MB」。拿不到大小（null / 0）时返回空串，**不显示 0 B** */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes <= 0) return ''
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  // B 不带小数；其余保留一位，但整数就不显示 `.0`
  const text = unit === 0 ? String(Math.round(value)) : value.toFixed(1).replace(/\.0$/, '')
  return `${text} ${units[unit]}`
}

/** 时间戳 → 「刚刚 / 12 分钟前 / 昨天 15:04 / 3月5日」 */
export function formatRelative(timestamp: number, now: number = Date.now()): string {
  const diff = now - timestamp
  if (!Number.isFinite(diff)) return ''
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`

  const date = new Date(timestamp)
  const today = new Date(now)
  const sameDay =
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate()
  const clock = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  if (sameDay) return `今天 ${clock}`

  const yesterday = new Date(now - 86_400_000)
  const isYesterday =
    date.getFullYear() === yesterday.getFullYear() &&
    date.getMonth() === yesterday.getMonth() &&
    date.getDate() === yesterday.getDate()
  if (isYesterday) return `昨天 ${clock}`

  return `${date.getMonth() + 1}月${date.getDate()}日`
}

/** 秒数 → 「30 分钟」。只在「我的」页说明结果保留多久时用 */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return ''
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`
  return `${Math.round(seconds / 3600)} 小时`
}

/** 「报告.docx」+ 3 → 「报告.docx 等 3 个文件」 */
export function batchTitle(filename: string, total: number): string {
  return total > 1 ? `${filename} 等 ${total} 个文件` : filename
}
