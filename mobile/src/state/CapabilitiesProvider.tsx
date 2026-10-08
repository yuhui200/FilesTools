/**
 * 能力表（规格 §八 / §三十四）。
 *
 * **整个 App 的工具列表只有这一个来源。** Mobile 本地没有能力矩阵 ——
 * 后端加一种格式，App 不重新发版就能看到它；反过来，服务器没装
 * LibreOffice 时 Office 那几条会带 `available: false` 出现在列表里，
 * 点进去前就知道不能用，而不是传完 50 MB 才报错。
 *
 * 只在 App 启动时拉一次，之后按分类过滤在内存里做（几十条数据，
 * 每次切分类都回服务器只会更慢、更容易失败）。下拉刷新可以手动重拉。
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import { ApiError } from '@/services/api/client'
import { fetchCapabilities } from '@/services/api/capabilities'
import { fetchConfig, type ServerConfig } from '@/services/api/config'
import type { ConversionCapability, ConversionCapabilities, ConversionCategoryOption } from '@/types'

/** 一个分类，以及归入它的工具 */
export interface ToolGroup {
  category: ConversionCategoryOption
  items: ConversionCapability[]
}

export interface CapabilitiesValue {
  /** 服务器当前真的能做的转换（含 `available: false` 的条目） */
  tools: ConversionCapability[]
  /** 按分类分好组的工具，给「工具」页直接用 */
  groups: ToolGroup[]
  /** 原样的响应，少数地方（格式表、提示语）需要 */
  capabilities: ConversionCapabilities | null
  /** 服务器配置（字体、错误码清单） */
  config: ServerConfig | null
  loading: boolean
  /** 拉取失败。**与「有能力但都不可用」是两回事** */
  error: ApiError | null
  reload: () => Promise<void>
  /** 按 ID 找一条能力。找不到返回 null（老的历史记录会指向已下线的能力） */
  findTool: (capabilityId: string) => ConversionCapability | null
  /** `dynamic: "fonts"` 那一项要的选项 */
  fontOptions: { value: string; label: string }[]
}

const CapabilitiesContext = createContext<CapabilitiesValue | null>(null)

/** 分类的显示顺序。取不到分类时用它兜底 */
const CATEGORY_ORDER = ['image', 'document', 'pdf']

export function CapabilitiesProvider({ children }: { children: React.ReactNode }) {
  const [capabilities, setCapabilities] = useState<ConversionCapabilities | null>(null)
  const [config, setConfig] = useState<ServerConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<ApiError | null>(null)

  const load = useCallback(async (signal?: AbortSignal): Promise<void> => {
    setLoading(true)
    setError(null)
    try {
      // 两个请求并发：它们互不依赖，串行只会让启动慢一倍。
      // 配置拉失败**不阻断**能力表 —— 字体项拿不到只是那一项不显示，
      // 其它工具照常用；反过来才会让整个 App 打不开。
      const [caps, cfg] = await Promise.all([
        fetchCapabilities({}, signal ? { signal } : {}),
        fetchConfig(signal ? { signal } : {}).catch(() => null),
      ])
      if (signal?.aborted) return
      setCapabilities(caps)
      setConfig(cfg)
    } catch (caught) {
      if (signal?.aborted) return
      setError(caught instanceof ApiError ? caught : new ApiError('无法加载工具列表', 'network_error'))
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  const reload = useCallback(async (): Promise<void> => {
    await load()
  }, [load])

  const tools = useMemo<ConversionCapability[]>(
    () => (capabilities?.conversions ?? []).filter((item) => item.operation_type === 'conversion'),
    [capabilities],
  )

  const groups = useMemo<ToolGroup[]>(() => {
    const known = capabilities?.categories ?? []
    const order = known.length > 0 ? known.map((item) => item.value) : CATEGORY_ORDER

    const byCategory = new Map<string, ConversionCapability[]>()
    for (const tool of tools) {
      const bucket = byCategory.get(tool.category)
      if (bucket) bucket.push(tool)
      else byCategory.set(tool.category, [tool])
    }

    const labelOf = (value: string): string =>
      known.find((item) => item.value === value)?.label ?? value

    return order
      .filter((value) => byCategory.has(value))
      .map((value) => ({
        category: { value, label: labelOf(value) },
        items: (byCategory.get(value) ?? []).slice().sort(compareTools),
      }))
  }, [capabilities, tools])

  const findTool = useCallback(
    (capabilityId: string): ConversionCapability | null =>
      tools.find((item) => item.id === capabilityId) ?? null,
    [tools],
  )

  const value = useMemo<CapabilitiesValue>(
    () => ({
      tools,
      groups,
      capabilities,
      config,
      loading,
      error,
      reload,
      findTool,
      fontOptions: config?.txt_fonts ?? [],
    }),
    [tools, groups, capabilities, config, loading, error, reload, findTool],
  )

  return <CapabilitiesContext.Provider value={value}>{children}</CapabilitiesContext.Provider>
}

/**
 * 排序：可用的在前，其次按显示名。
 *
 * 不可用的沉底而不是藏起来 —— 用户搜「PDF 转 Word」时应该看得到它、
 * 并且一眼看到它为什么不能用，而不是搜不到、以为 App 没这个功能。
 */
function compareTools(left: ConversionCapability, right: ConversionCapability): number {
  if (left.available !== right.available) return left.available ? -1 : 1
  return left.display_name.localeCompare(right.display_name, 'zh-Hans-CN')
}

export function useCapabilities(): CapabilitiesValue {
  const value = useContext(CapabilitiesContext)
  if (value === null) {
    throw new Error('useCapabilities 必须在 CapabilitiesProvider 里使用')
  }
  return value
}
