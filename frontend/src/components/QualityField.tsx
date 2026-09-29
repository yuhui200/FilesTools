import { RadioGroup, type RadioOption } from './RadioGroup'

import type { QualityChoice } from '@/hooks/useBatchTask'

const OPTIONS: RadioOption<QualityChoice>[] = [
  { value: 'high', label: '高质量', hint: '画质优先，文件较大' },
  { value: 'balanced', label: '平衡', hint: '画质与体积兼顾（推荐）' },
  { value: 'strong', label: '高压缩', hint: '体积优先，画质损失较明显' },
  { value: 'custom', label: '自定义质量' },
]

/** 图片压缩页按档位名传参，不提供自定义数值 */
const PRESET_ONLY = OPTIONS.slice(0, 3)

interface QualityFieldProps {
  value: QualityChoice
  customValue: number
  /** 未指定质量时服务端使用的默认值，用于界面说明 */
  defaultValue: number
  onChange: (choice: QualityChoice) => void
  onCustomChange: (value: number) => void
  disabled?: boolean
  /** 目标格式为 PNG 时不显示 —— PNG 是无损格式，质量的含义与 JPG 不同 */
  hint?: string
  /** 是否提供「自定义质量」档（默认提供） */
  allowCustom?: boolean
  /**
   * 单选按钮的分组名。
   *
   * 同一张页面上出现多份质量设置时必须各给一个（统一转换中心里每组一个），
   * 否则 name 相同的单选按钮会互相清空对方的选择。
   */
  name?: string
}

/**
 * 图片质量设置（1-100）。
 *
 * PNG 是无损格式，调用方会通过 hint 说明「PNG 不做有损压缩」，
 * 因此这里的质量选项只在 JPG / WEBP 下展示数值输入。
 */
export function QualityField({
  value,
  customValue,
  defaultValue,
  onChange,
  onCustomChange,
  disabled = false,
  hint,
  allowCustom = true,
  name = 'quality-choice',
}: QualityFieldProps) {
  return (
    <div>
      <RadioGroup
        name={name}
        value={value}
        options={allowCustom ? OPTIONS : PRESET_ONLY}
        onChange={onChange}
        columns={2}
        disabled={disabled}
      />

      {value === 'custom' && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <input
            type="range"
            min={1}
            max={100}
            step={1}
            value={customValue}
            disabled={disabled}
            onChange={(event) => onCustomChange(Number(event.target.value))}
            aria-label="自定义图片质量"
            className="h-1.5 w-full max-w-xs cursor-pointer appearance-none rounded-full bg-slate-200 accent-brand-600 disabled:opacity-50"
          />
          <input
            type="number"
            min={1}
            max={100}
            step={1}
            inputMode="numeric"
            value={customValue}
            disabled={disabled}
            onChange={(event) => onCustomChange(Number(event.target.value))}
            aria-label="自定义图片质量数值"
            className="field w-24"
          />
          <span className="text-xs text-slate-500">1 - 100，数值越大画质越好</span>
        </div>
      )}

      <p className="mt-3 text-xs text-slate-500">
        {hint ?? `未指定时使用默认质量 ${defaultValue}。`}
      </p>
    </div>
  )
}
