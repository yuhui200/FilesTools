/**
 * 本地历史（规格 §十九）。
 *
 * ## 三件事必须说清楚
 *
 * 1. **只存元数据，不存文件本体。** 转换结果在服务器的临时目录里、下载一次
 *    就作废，存一份副本既占手机空间又与「一次性令牌」的设计打架。所以历史
 *    列表里的「重新下载」在结果过期后是真的下不动 —— 界面据 `resultExpired`
 *    如实显示，**不假装还能取**。
 * 2. **没有数据库。** 就是一个 JSON 数组，放 AsyncStorage 里，最多
 *    :data:`MAX_HISTORY` 条。超了从头截断。
 * 3. **写失败要如实返回 false。** 存储写不进去（空间满、企业策略禁存储）时
 *    不能静默吞掉 —— 否则用户以为存了，下次打开却没有。返回值由界面决定
 *    要不要提示。
 */

import { readJson, remove, writeJson } from '@/storage/kv'
import type { HistoryEntry } from '@/types'

/** AsyncStorage 里的键名（`storage/kv.ts` 会再加统一前缀） */
const HISTORY_KEY = 'history'

/**
 * 最多留多少条。
 *
 * 50 条的 JSON 大约几十 KB，读进来和排序都是瞬时的；再多也不会有人往下翻，
 * 只是白白拖慢每次启动。
 */
export const MAX_HISTORY = 50

/** 按提交时间倒序读回全部记录。读不到 / 数据损坏时返回空数组，不抛错。 */
export async function loadHistory(): Promise<HistoryEntry[]> {
  const stored = await readJson<unknown>(HISTORY_KEY)
  if (!Array.isArray(stored)) return []
  return stored
    .filter(isHistoryEntry)
    .sort((left, right) => right.createdAt - left.createdAt)
}

/**
 * 记下一条新提交，返回是否写成功。
 *
 * 同一个 `batchId` 已存在时**替换**而不是追加：重试、重转都会命中同一批，
 * 追加会在列表里留下两条看不出区别的记录。
 */
export async function recordSubmission(entry: HistoryEntry): Promise<boolean> {
  const entries = await loadHistory()
  const without = entries.filter((item) => item.batchId !== entry.batchId)
  return writeJson(HISTORY_KEY, [entry, ...without].slice(0, MAX_HISTORY))
}

/**
 * 更新某一批的状态（轮询到新状态、结果过期时用）。
 *
 * 找不到这条记录时**什么都不做**并返回 false —— 历史被清了、或这条是
 * 从别处发起的批次，都不该凭空造一条记录出来。
 */
export async function updateHistory(
  batchId: string,
  patch: Partial<Omit<HistoryEntry, 'batchId'>>,
): Promise<boolean> {
  const entries = await loadHistory()
  const index = entries.findIndex((item) => item.batchId === batchId)
  if (index === -1) return false
  const next = entries.slice()
  next[index] = { ...next[index], ...patch, batchId }
  return writeJson(HISTORY_KEY, next)
}

/** 删掉一条记录 */
export async function removeHistoryEntry(batchId: string): Promise<boolean> {
  const entries = await loadHistory()
  const next = entries.filter((item) => item.batchId !== batchId)
  if (next.length === entries.length) return true
  return writeJson(HISTORY_KEY, next)
}

/** 清空历史。删键而不是写空数组 —— 省得下次读出一个空数组再判断一遍 */
export async function clearHistory(): Promise<void> {
  await remove(HISTORY_KEY)
}

/**
 * 一条记录是不是能用的形状。
 *
 * 本地数据是**上一个版本的 App 写的**，字段可能对不上（我们以后一定会加字段）。
 * 与其让界面在某次启动后崩在 `entry.createdAt.toFixed` 上，不如在这里把
 * 认不出来的行丢掉。
 */
function isHistoryEntry(value: unknown): value is HistoryEntry {
  if (typeof value !== 'object' || value === null) return false
  const entry = value as Partial<HistoryEntry>
  return (
    typeof entry.batchId === 'string' &&
    entry.batchId !== '' &&
    typeof entry.filename === 'string' &&
    typeof entry.sourceType === 'string' &&
    typeof entry.targetType === 'string' &&
    typeof entry.capabilityId === 'string' &&
    typeof entry.total === 'number' &&
    typeof entry.status === 'string' &&
    typeof entry.createdAt === 'number' &&
    Number.isFinite(entry.createdAt)
  )
}
