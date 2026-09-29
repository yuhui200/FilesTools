import type { ConversionCapabilities, ConversionCapability } from '@/types'
import { conversionTargetLabel, splitTargets } from '@/utils/conversion'

import { RadioGroup, type RadioOption } from './RadioGroup'

interface ConversionTargetSelectorProps {
  /**
   * 所属分组的 id。
   *
   * 单选按钮靠 ``name`` 分组，同一张页面上有多个分组时，
   * 名字相同会让「选 A 组的 WEBP」把 B 组的选择也清掉。
   */
  groupId: string
  /** 这一种源格式的全部能力，来自 capabilities */
  entries: ConversionCapability[]
  value: string
  capabilities: ConversionCapabilities
  disabled?: boolean
  onChange: (target: string) => void
}

/**
 * 目标格式选择：**推荐**与**其他格式**两栏（§二十二）。
 *
 * 两栏都是普通单选框，都能选 —— 分栏只是把常用的几个放在第一屏，
 * 不是把其余的藏起来，也不做任何「评分」。
 *
 * 推荐与否由服务端在条目上打的 ``recommended`` 标签决定（registry 里
 * 那条按族排的偏好表），前端**不自己算**「哪些算常用」：那会变成第二份
 * 能力矩阵，加一种格式就得改两处（§十六）。
 */
export function ConversionTargetSelector({
  groupId,
  entries,
  value,
  capabilities,
  disabled = false,
  onChange,
}: ConversionTargetSelectorProps) {
  const { recommended, others } = splitTargets(entries)

  const toOptions = (items: ConversionCapability[]): RadioOption<string>[] =>
    items.map((entry) => {
      const format = capabilities.formats.find((item) => item.value === entry.target_type)
      const extension = format?.extensions[0]
      return {
        value: entry.target_type,
        label: conversionTargetLabel(capabilities, entry.target_type),
        hint: extension ? `输出 ${extension}` : undefined,
      }
    })

  return (
    <div className="space-y-5">
      {recommended.length > 0 && (
        <div>
          <p className="text-xs font-medium text-slate-500">推荐</p>
          <div className="mt-2">
            <RadioGroup
              name={`conversion-target-${groupId}`}
              value={value}
              options={toOptions(recommended)}
              onChange={onChange}
              disabled={disabled}
            />
          </div>
        </div>
      )}

      {others.length > 0 && (
        <div>
          <p className="text-xs font-medium text-slate-500">其他格式</p>
          <div className="mt-2">
            <RadioGroup
              name={`conversion-target-${groupId}`}
              value={value}
              options={toOptions(others)}
              onChange={onChange}
              disabled={disabled}
            />
          </div>
        </div>
      )}
    </div>
  )
}
