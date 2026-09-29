export interface RadioOption<T extends string> {
  value: T
  label: string
  hint?: string
}

interface RadioGroupProps<T extends string> {
  name: string
  value: T
  options: RadioOption<T>[]
  onChange: (value: T) => void
  disabled?: boolean
  /** 每行显示的选项数（大屏） */
  columns?: 2 | 3
}

export function RadioGroup<T extends string>({
  name,
  value,
  options,
  onChange,
  disabled = false,
  columns = 3,
}: RadioGroupProps<T>) {
  const gridClass = columns === 2 ? 'sm:grid-cols-2' : 'sm:grid-cols-3'

  return (
    <div role="radiogroup" className={`grid grid-cols-1 gap-2.5 ${gridClass}`}>
      {options.map((option) => {
        const checked = option.value === value
        return (
          <label
            key={option.value}
            className={[
              'flex cursor-pointer items-start gap-2.5 rounded-xl border p-3.5 transition',
              disabled ? 'cursor-not-allowed opacity-60' : '',
              checked
                ? 'border-brand-500 bg-brand-50/70 ring-1 ring-brand-500'
                : 'border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50',
            ].join(' ')}
          >
            <input
              type="radio"
              name={name}
              value={option.value}
              checked={checked}
              disabled={disabled}
              onChange={() => onChange(option.value)}
              className="sr-only"
            />
            <span
              aria-hidden="true"
              className={[
                'mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full border-2 transition',
                checked ? 'border-brand-600' : 'border-slate-300',
              ].join(' ')}
            >
              {checked && <span className="h-2 w-2 rounded-full bg-brand-600" />}
            </span>

            <span className="min-w-0">
              <span
                className={`block text-sm font-medium ${checked ? 'text-brand-900' : 'text-slate-700'}`}
              >
                {option.label}
              </span>
              {option.hint && (
                <span className="mt-0.5 block text-xs leading-snug text-slate-500">
                  {option.hint}
                </span>
              )}
            </span>
          </label>
        )
      })}
    </div>
  )
}
