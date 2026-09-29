import type { SizeOption } from '@/hooks/useBatchTask'
import { formatBytes } from '@/utils/format'

import { RadioGroup, type RadioOption } from './RadioGroup'

const OPTIONS: RadioOption<SizeOption>[] = [
  { value: 'none', label: '不限制', hint: '只按质量设置压缩' },
  { value: '1mb', label: '≤ 1 MB' },
  { value: '2mb', label: '≤ 2 MB' },
  { value: '5mb', label: '≤ 5 MB' },
  { value: 'custom', label: '自定义大小' },
]

interface TargetSizeFieldProps {
  value: SizeOption
  customValue: string
  customUnit: 'KB' | 'MB'
  maxBytes: number
  error: string | null
  onChange: (option: SizeOption) => void
  onCustomChange: (value: string, unit: 'KB' | 'MB') => void
  disabled?: boolean
  /**
   * 单选按钮的分组名。
   *
   * 同一张页面上出现多份目标大小设置时必须各给一个（统一转换中心里每组一个），
   * 否则 name 相同的单选按钮会互相清空对方的选择。
   */
  name?: string
}

/**
 * 目标最大文件大小。
 *
 * 指定后，程序会先按用户的尺寸设置调整图片，再自动优化压缩质量，
 * 使结果尽可能接近目标且不超过目标。
 */
export function TargetSizeField({
  value,
  customValue,
  customUnit,
  maxBytes,
  error,
  onChange,
  onCustomChange,
  disabled = false,
  name = 'target-size',
}: TargetSizeFieldProps) {
  return (
    <div>
      <RadioGroup
        name={name}
        value={value}
        options={OPTIONS}
        onChange={onChange}
        columns={3}
        disabled={disabled}
      />

      {value === 'custom' && (
        <div className="mt-3 flex flex-wrap items-center gap-2.5">
          <input
            type="number"
            min={1}
            step={1}
            inputMode="numeric"
            value={customValue}
            disabled={disabled}
            onChange={(event) => onCustomChange(event.target.value, customUnit)}
            className="field w-32"
            aria-label="自定义目标大小"
          />
          <div className="inline-flex overflow-hidden rounded-xl border border-slate-300">
            {(['KB', 'MB'] as const).map((unit) => (
              <button
                key={unit}
                type="button"
                disabled={disabled}
                onClick={() => onCustomChange(customValue, unit)}
                className={[
                  'h-[42px] px-4 text-sm font-medium transition disabled:opacity-50',
                  customUnit === unit
                    ? 'bg-brand-600 text-white'
                    : 'bg-white text-slate-600 hover:bg-slate-50',
                ].join(' ')}
                aria-pressed={customUnit === unit}
              >
                {unit}
              </button>
            ))}
          </div>
          {error && <p className="text-xs text-red-600">{error}</p>}
        </div>
      )}

      <p className="mt-3 text-xs text-slate-500">
        指定后会自动优化压缩质量使结果不超过目标；单个文件上限 {formatBytes(maxBytes)}。
      </p>
    </div>
  )
}
