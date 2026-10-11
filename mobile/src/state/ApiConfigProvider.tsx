/**
 * 服务器地址（规格 §十二 的运行期那一半）。
 *
 * ## 为什么需要它
 *
 * 0.1.0 那份 APK 的地址是**构建时**烤死的，装到手机上就改不了 ——
 * 而「手机上填的 127.0.0.1 指的是手机自己」这件事，决定了发出去的包
 * 必须在**设备上**能改地址，否则它连不上任何一台真实的服务器。
 * （0.1.0 的发行说明反过来写了「首次启动时填」，而当时根本没有这个入口。）
 *
 * ## 分工
 *
 * - 纯内存的覆盖值住在 `services/api/client.ts` —— 那里不能 import 本地存储
 *   （验收脚本要把 `client.ts` 编成 CJS 交给 node 跑，见那边的注释）。
 * - 读盘、写盘、什么时候生效，住在这里。
 *
 * ## 顺序
 *
 * 启动时**先把本地存的值读出来**（几十毫秒），再让 `CapabilitiesProvider`
 * 去拉能力表 —— 反过来的话，第一次启动会拿着构建时的旧地址发一轮请求，
 * 用户看到的就是「明明改过地址了还是连不上」。
 * 所以 `ready` 是给下游用的闸门，不是装饰。
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'

import {
  getApiBaseUrl,
  normalizeApiBaseUrl,
  setApiBaseUrlOverride,
} from '@/services/api/client'
import { readJson, remove, writeJson } from '@/storage/kv'

/** 本地存储里的键名（`kv.ts` 会加上 `filetools.` 前缀） */
const STORAGE_KEY = 'apiBaseUrl'

export interface ApiConfigValue {
  /** 本地存的值读完了没有。**没读完之前不要发请求** */
  ready: boolean
  /** 当前生效的地址 */
  baseUrl: string
  /**
   * 打包时烤进安装包里的那个地址（可能为空 —— 构建时没配就是没配）。
   * 界面要把它显示出来，用户才知道「恢复默认」会恢复到哪去。
   */
  defaultBaseUrl: string
  /**
   * 这个地址是用户在 App 里设的（true），还是构建时烤进包里的（false）。
   * 「我的」页要如实说清是哪一种 —— 用户有权知道改了会不会跟着走。
   */
  isOverridden: boolean
  /** 保存一个地址；写本地失败返回 false（界面据此提示「这次改的不会被记住」） */
  save: (value: string) => Promise<boolean>
  /** 清掉本地值，退回构建时的默认地址 */
  reset: () => Promise<void>
}

const ApiConfigContext = createContext<ApiConfigValue | null>(null)

/** 读回来的值可能是任何东西（老版本写的、手改的），只认非空字符串 */
function usable(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const trimmed = normalizeApiBaseUrl(value)
  return trimmed === '' ? null : trimmed
}

export function ApiConfigProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false)
  const [override, setOverride] = useState<string | null>(null)
  // 构建时的默认值只在首次渲染时取一次 —— 此刻还没人写过覆盖值，
  // 所以 `getApiBaseUrl()` 拿到的必然是打包时烤进去的那个。
  // 它此后不会变（除非热重载整包），不需要跟着 state 走。
  const [envBaseUrl] = useState(() => getApiBaseUrl())

  useEffect(() => {
    let alive = true
    void readJson<unknown>(STORAGE_KEY).then((saved) => {
      if (!alive) return
      const value = usable(saved)
      if (value) {
        setApiBaseUrlOverride(value)
        setOverride(value)
      }
      setReady(true)
    })
    return () => {
      alive = false
    }
  }, [])

  const save = useCallback(async (value: string): Promise<boolean> => {
    const normalized = normalizeApiBaseUrl(value)
    // **先让新地址生效再写盘**：写盘会失败（磁盘满、企业策略），
    // 但「这次能用」不该跟着一起失败。写不进去只影响「下次还记不记得」。
    setApiBaseUrlOverride(normalized)
    setOverride(normalized)
    return writeJson(STORAGE_KEY, normalized)
  }, [])

  const reset = useCallback(async (): Promise<void> => {
    setApiBaseUrlOverride(null)
    setOverride(null)
    await remove(STORAGE_KEY)
  }, [])

  const baseUrl = override ?? envBaseUrl

  const value = useMemo<ApiConfigValue>(
    () => ({
      ready,
      baseUrl,
      defaultBaseUrl: envBaseUrl,
      isOverridden: override !== null,
      save,
      reset,
    }),
    [ready, baseUrl, envBaseUrl, override, save, reset],
  )

  return <ApiConfigContext.Provider value={value}>{children}</ApiConfigContext.Provider>
}

export function useApiConfig(): ApiConfigValue {
  const value = useContext(ApiConfigContext)
  if (!value) throw new Error('useApiConfig 必须在 ApiConfigProvider 里用')
  return value
}
