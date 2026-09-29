/**
 * 「最近处理」的记录（第四阶段 §12）。
 *
 * 只存**元信息**：文件名称、操作类型、处理时间、处理状态、文件大小。
 * 不存文件内容、不存 blob、不存下载链接 —— 结果文件的下载链接是
 * 一次性的，存下来也点不动；原始文件更是从不落到浏览器里。
 *
 * 存在 ``sessionStorage``：关掉标签页就清空，换台设备也看不到，
 * 与「不长期保存用户文件」的承诺一致。写入失败（隐私模式、配额满）
 * 一律静默忽略 —— 记录历史失败不该影响用户拿到结果。
 */

import type { TaskSnapshot } from '@/types'

const KEY = 'filetools.history.v1'

/** 最多保留多少条，超出丢弃最旧的 */
export const HISTORY_LIMIT = 20

export interface HistoryEntry {
  /** 时间戳 + 序号，用于列表 key 与去重 */
  id: string
  /** 文件名称 */
  filename: string
  /** 操作类型，例如「图片压缩」 */
  tool: string
  /** 处理时间（epoch 毫秒） */
  at: number
  /** 处理状态：成功 / 失败 */
  ok: boolean
  /** 文件大小（字节）—— 输入文件的大小 */
  size: number
}

/** 一次处理完成后要记的一条 */
export type HistoryInput = Omit<HistoryEntry, 'id' | 'at'> & { at?: number }

/**
 * 从任务快照生成记录：批量任务的每个文件各记一条。
 *
 * 文件名、大小、成功与否都直接来自服务端快照，不重新拼一遍 ——
 * 这样「最近处理」里显示的失败文件和结果面板里的是同一份事实。
 */
export function historyFromTasks(snapshot: TaskSnapshot<unknown>): HistoryInput[] {
  return snapshot.tasks.map((task) => ({
    filename: task.filename,
    tool: snapshot.label,
    ok: task.state === 'done',
    size: task.size,
  }))
}

type Listener = () => void

const listeners = new Set<Listener>()

// useSyncExternalStore 要求 getSnapshot 返回稳定引用：
// 每次读 sessionStorage 都新建数组会让 React 认为数据一直在变，直接死循环。
let cache: HistoryEntry[] | null = null

function parse(raw: string | null): HistoryEntry[] {
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(isEntry).slice(0, HISTORY_LIMIT)
  } catch {
    // 数据被外部改坏时当作空历史，不要让首页整页崩掉
    return []
  }
}

function isEntry(value: unknown): value is HistoryEntry {
  if (typeof value !== 'object' || value === null) return false
  const entry = value as Record<string, unknown>
  return (
    typeof entry.id === 'string' &&
    typeof entry.filename === 'string' &&
    typeof entry.tool === 'string' &&
    typeof entry.at === 'number' &&
    typeof entry.ok === 'boolean' &&
    typeof entry.size === 'number'
  )
}

function read(): HistoryEntry[] {
  if (cache) return cache
  try {
    cache = parse(window.sessionStorage.getItem(KEY))
  } catch {
    cache = []
  }
  return cache
}

function write(next: HistoryEntry[]): void {
  cache = next
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify(next))
  } catch {
    // 配额满或被禁用：本次会话内仍然看得到（cache 已更新），只是刷新后丢失
  }
  listeners.forEach((listener) => listener())
}

/** 记录一次处理结果，最新的排在最前面 */
export function recordHistory(entries: HistoryInput[]): void {
  if (entries.length === 0) return

  const now = Date.now()
  const recorded: HistoryEntry[] = entries.map((entry, index) => ({
    ...entry,
    id: `${now}-${index}-${Math.random().toString(36).slice(2, 8)}`,
    at: entry.at ?? now,
  }))

  write([...recorded, ...read()].slice(0, HISTORY_LIMIT))
}

/** 清空最近处理 */
export function clearHistory(): void {
  write([])
}

export function subscribeHistory(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getHistory(): HistoryEntry[] {
  return read()
}
