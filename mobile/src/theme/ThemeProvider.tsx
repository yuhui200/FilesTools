/**
 * 主题（规格 §二十二）：Light / Dark / System，默认 **System**。
 *
 * 「System」不是一个颜色，是一个**跟随**关系 —— 所以这里存的是 `mode`，
 * 而真正生效的 `scheme` 由 `useColorScheme()` 现算。把 mode 直接当成
 * scheme 用，系统在用户没进设置的情况下切成深色时界面不会跟着变。
 *
 * 偏好写进本地存储；写失败只是这次不记住，界面照常切换。
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'
import { useColorScheme } from 'react-native'

import { readJson, writeJson } from '@/storage/kv'
import { DARK, LIGHT, type Palette } from './tokens'

export type ThemeMode = 'light' | 'dark' | 'system'
export type ColorScheme = 'light' | 'dark'

const STORAGE_KEY = 'theme'

/** 三档都摆出来，界面上按这个顺序渲染，不在页面里另写一遍 */
export const THEME_MODES: ReadonlyArray<{ value: ThemeMode; label: string }> = [
  { value: 'system', label: '跟随系统' },
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
]

interface ThemeValue {
  /** 用户选的档位 */
  mode: ThemeMode
  /** 切换档位；写本地失败不影响这次切换 */
  setMode: (mode: ThemeMode) => void
  /** 实际生效的明暗 */
  scheme: ColorScheme
  colors: Palette
}

const ThemeContext = createContext<ThemeValue | null>(null)

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const systemScheme = useColorScheme()
  const [mode, setModeState] = useState<ThemeMode>('system')

  // 读回上次的选择。读到之前先按 System 显示 —— 这一小段闪动换来的是
  // 启动时不阻塞（异步读盘几十毫秒，挡在首屏前面会看出白屏）。
  useEffect(() => {
    let alive = true
    void readJson<ThemeMode>(STORAGE_KEY).then((saved) => {
      if (alive && (saved === 'light' || saved === 'dark' || saved === 'system')) {
        setModeState(saved)
      }
    })
    return () => {
      alive = false
    }
  }, [])

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next)
    void writeJson(STORAGE_KEY, next)
  }, [])

  const scheme: ColorScheme = mode === 'system' ? (systemScheme === 'dark' ? 'dark' : 'light') : mode

  const value = useMemo<ThemeValue>(
    () => ({ mode, setMode, scheme, colors: scheme === 'dark' ? DARK : LIGHT }),
    [mode, setMode, scheme],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeValue {
  const value = useContext(ThemeContext)
  if (value === null) {
    throw new Error('useTheme 必须在 ThemeProvider 里使用')
  }
  return value
}

/** 只要颜色时的快捷方式 */
export function useColors(): Palette {
  return useTheme().colors
}
