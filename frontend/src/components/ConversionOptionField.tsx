import type { ConversionOptionSpec } from '@/types'
import { prefersSelect, prefersSlider, stepOf, type OptionValue } from '@/utils/conversionOptions'

import { RadioGroup, type RadioOption } from './RadioGroup'

interface OptionFieldProps {
  /** 所属分组/卡片的 id，用来给单选按钮分组（同页多份时不能同名） */
  groupId: string
  item: ConversionOptionSpec
  value: OptionValue | undefined
  disabled: boolean
  onChange: (value: OptionValue) => void
}

/**
 * 一项参数。
 *
 * 控件由 ``type`` 与取值范围决定，**与键名无关**：
 *
 * * ``boolean`` → 复选框；
 * * ``enum`` → 取值少且标签短时用单选框，否则下拉框；
 * * ``integer`` / ``number`` → 范围窄的给滑杆 + 输入框，跨度大的只给输入框，
 *   带 ``presets`` 的（质量、目标大小）在输入框上方多一行快捷档；
 * * ``string`` → 文本框。
 *
 * 转换参数面板与 PDF 操作卡片**共用这一个渲染器** —— 两边各写一份的话，
 * 「必填项怎么标」「读不到可选值时怎么办」这些决定就会各走各的。
 */
export function OptionField({ groupId, item, value, disabled, onChange }: OptionFieldProps) {
  const help = item.help && <p className="mt-1 text-xs text-slate-500">{item.help}</p>
  // 星号对读屏软件是噪音，而且会改变 <legend> 的无障碍名字
  // （`get_by_role("group", name="尺寸", exact=True)` 会当场失配）。
  const required = item.required && (
    <span aria-hidden="true" className="ml-1 text-red-600">
      *
    </span>
  )

  if (item.type === 'boolean') {
    return (
      <fieldset>
        <label className="flex cursor-pointer items-center gap-2.5">
          <input
            type="checkbox"
            name={`conversion-${item.key}-${groupId}`}
            checked={value === true || value === 'true'}
            disabled={disabled}
            onChange={(event) => onChange(event.target.checked)}
            className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
          />
          <span className="text-sm font-medium text-slate-900">{item.label}</span>
        </label>
        {help}
      </fieldset>
    )
  }

  if (item.type === 'enum') {
    const options: RadioOption<string>[] = (item.enum ?? []).map((entry) => ({
      value: entry.value,
      label: entry.label,
    }))
    const current = typeof value === 'string' ? value : ''

    // 值域由服务端在运行期填（字体就是这一类）。填不出来时**不能**画一个
    // 空的单选组：用户会以为页面坏了，而且提交上去的值必然非法。
    // 如实说明读不到，并把整项禁用。
    if (options.length === 0) {
      return (
        <fieldset>
          <legend className="text-sm font-medium text-slate-900">
            {item.label}
            {required}
          </legend>
          {help}
          <p className="mt-1 text-xs text-slate-500">暂时读不到可选值，这一项已停用。</p>
        </fieldset>
      )
    }

    return (
      <fieldset>
        <legend className="text-sm font-medium text-slate-900">
          {item.label}
          {required}
        </legend>
        {help}
        <div className="mt-3">
          {prefersSelect(item) ? (
            <select
              name={`conversion-${item.key}-${groupId}`}
              value={current}
              disabled={disabled}
              onChange={(event) => onChange(event.target.value)}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:bg-slate-50 disabled:text-slate-500"
            >
              {options.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          ) : (
            <RadioGroup
              name={`conversion-${item.key}-${groupId}`}
              value={current}
              options={options}
              onChange={onChange}
              disabled={disabled}
            />
          )}
        </div>
      </fieldset>
    )
  }

  if (item.type === 'integer' || item.type === 'number') {
    const text = typeof value === 'string' ? value : value === undefined ? '' : String(value)
    const step = stepOf(item)
    const slider = prefersSlider(item)
    const presets = item.presets ?? []

    return (
      <fieldset>
        <legend className="text-sm font-medium text-slate-900">
          {item.label}
          {required}
        </legend>
        {help}
        {presets.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {presets.map((preset) => {
              // 「选中」按**值**比，不按索引：预设是服务端给的一组落点，
              // 用户完全可以在输入框里填一个不在表里的数，那时不该有任何
              // 一个按钮亮着 —— 亮了就是在说「你选的是这一档」，而其实不是。
              const active = text === preset.value
              return (
                <button
                  key={preset.value}
                  type="button"
                  aria-pressed={active}
                  disabled={disabled}
                  onClick={() => onChange(preset.value)}
                  className={[
                    'rounded-full border px-3 py-1.5 text-xs font-medium transition',
                    'disabled:cursor-not-allowed disabled:opacity-50',
                    active
                      ? 'border-brand-600 bg-brand-50 text-brand-700'
                      : 'border-slate-300 bg-white text-slate-700 hover:border-brand-400 hover:text-brand-700',
                  ].join(' ')}
                >
                  {preset.label}
                </button>
              )
            })}
          </div>
        )}
        <div className="mt-3 flex items-center gap-3">
          {slider && (
            <input
              type="range"
              aria-label={item.label}
              min={item.min}
              max={item.max}
              step={step}
              value={text === '' ? String(item.min ?? 0) : text}
              disabled={disabled}
              onChange={(event) => onChange(event.target.value)}
              className="h-2 flex-1 cursor-pointer appearance-none rounded-full bg-slate-200 accent-brand-600"
            />
          )}
          <span className="flex items-center gap-1.5">
            <input
              type="number"
              name={`conversion-${item.key}-${groupId}`}
              inputMode={item.type === 'integer' ? 'numeric' : 'decimal'}
              min={item.min}
              max={item.max}
              step={step}
              value={text}
              placeholder={
                item.default !== null && item.default !== undefined ? String(item.default) : ''
              }
              disabled={disabled}
              onChange={(event) => onChange(event.target.value)}
              className={[
                'rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900',
                'focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500',
                'disabled:bg-slate-50 disabled:text-slate-500',
                slider ? 'w-24' : 'w-32',
              ].join(' ')}
            />
            {item.unit && <span className="text-xs text-slate-500">{item.unit}</span>}
          </span>
        </div>
      </fieldset>
    )
  }

  // string：自由文本（值域无法穷举的项）。非法取值由服务端明确拒绝。
  return (
    <fieldset>
      <label className="block">
        <span className="text-sm font-medium text-slate-900">
          {item.label}
          {required}
        </span>
        {help}
        <input
          type="text"
          name={`conversion-${item.key}-${groupId}`}
          value={typeof value === 'string' ? value : ''}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
          className="mt-3 w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:bg-slate-50 disabled:text-slate-500"
        />
      </label>
    </fieldset>
  )
}
