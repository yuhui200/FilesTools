import { useSyncExternalStore } from 'react'

import { clearHistory, getHistory, subscribeHistory, type HistoryEntry } from '@/utils/history'

export interface History {
  entries: HistoryEntry[]
  clear: () => void
}

/**
 * 订阅「最近处理」（第四阶段 §12）。
 *
 * sessionStorage 是浏览器里的外部数据源，用 useSyncExternalStore 读它，
 * 首页和工具页拿到的永远是同一份、且不会有快照不一致的问题
 * （记录发生在处理完成的回调里，不在渲染期间）。
 */
export function useHistory(): History {
  const entries = useSyncExternalStore(subscribeHistory, getHistory, getHistory)
  return { entries, clear: clearHistory }
}
