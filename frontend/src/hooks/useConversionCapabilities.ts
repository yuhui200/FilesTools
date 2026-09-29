import { useEffect, useState } from 'react'

import { fetchConversionCapabilities } from '@/services/api'
import type { ConversionCapabilities } from '@/types'

/** 读不到能力目录时的提示。与转换中心那句一致 —— 同一件事不该有两种说法。 */
export const CAPABILITIES_ERROR =
  '读取服务器支持的转换类型失败，请刷新页面重试；问题持续出现时可以稍后再来。'

export interface ConversionCapabilitiesState {
  /** 还没到手时是 ``null`` —— 调用方必须处理这个状态，不能当成「什么都支持」 */
  capabilities: ConversionCapabilities | null
  loading: boolean
  /** 读不到的原因（中文）；为 null 表示一切正常 */
  error: string | null
}

/**
 * 读一次能力目录（``GET /api/conversion/capabilities``）。
 *
 * 图片工具页与首页的「支持哪些格式」都靠它 —— **不自己维护一份格式清单**。
 * 这不是洁癖：服务器缺 HEIC 组件时那个格式会从矩阵里消失，前端另抄一份
 * 清单就会继续宣称支持，用户传上去才发现不行（§五十六）。
 *
 * 与 ``useServerConfig`` 同一套路：读不到就如实说读不到，不猜。
 * 猜「应该都支持」正是上面那种假话的来源。
 */
export function useConversionCapabilities(): ConversionCapabilitiesState {
  const [state, setState] = useState<ConversionCapabilitiesState>({
    capabilities: null,
    loading: true,
    error: null,
  })

  useEffect(() => {
    const controller = new AbortController()

    fetchConversionCapabilities(controller.signal)
      .then((capabilities) => {
        setState({ capabilities, loading: false, error: null })
      })
      .catch(() => {
        if (controller.signal.aborted) return
        setState({ capabilities: null, loading: false, error: CAPABILITIES_ERROR })
      })

    return () => controller.abort()
  }, [])

  return state
}
