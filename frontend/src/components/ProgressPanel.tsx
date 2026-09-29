import { IconCheck } from './Icons'

export interface ProgressStep {
  key: string
  label: string
}

interface ProgressPanelProps {
  /** 当前状态文案，例如「正在压缩…」 */
  statusText: string
  /** 0-100；传 null 表示进度未知，显示为滚动条 */
  percent: number | null
  steps: ProgressStep[]
  /** 已完成 / 正在进行的步骤下标 */
  activeStep: number
  /**
   * 等待状态：文件已经就绪，还没开始处理（§12 的「等待处理」）。
   *
   * 这时既没有进度也不该有动画 —— 一个跳动的进度条会让人以为已经在跑了，
   * 所以进度条是静止的灰色，步骤里也没有「进行中」的那一项。
   */
  waiting?: boolean
}

export function ProgressPanel({
  statusText,
  percent,
  steps,
  activeStep,
  waiting = false,
}: ProgressPanelProps) {
  const clamped = percent === null ? null : Math.max(0, Math.min(100, Math.round(percent)))

  return (
    <div className="card p-5 sm:p-6" role="status" aria-live="polite">
      <div className="flex items-center justify-between gap-4">
        <p
          className={[
            'text-sm font-medium',
            waiting ? 'text-slate-600' : 'text-slate-900',
          ].join(' ')}
        >
          {statusText}
        </p>
        {clamped !== null && !waiting && (
          <span className="text-sm tabular-nums text-slate-500">{clamped}%</span>
        )}
      </div>

      <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-slate-100">
        {waiting ? (
          <div className="h-full w-full rounded-full bg-slate-200" />
        ) : clamped === null ? (
          <div className="h-full w-1/4 animate-indeterminate rounded-full bg-brand-500" />
        ) : (
          <div
            className="h-full rounded-full bg-brand-500 transition-[width] duration-300 ease-out"
            style={{ width: `${Math.max(clamped, 3)}%` }}
          />
        )}
      </div>

      <ol className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-2">
        {steps.map((step, index) => {
          const done = index < activeStep
          const active = !waiting && index === activeStep
          return (
            <li key={step.key} className="flex items-center gap-2 text-xs">
              <span
                className={[
                  'grid h-5 w-5 place-items-center rounded-full text-[10px] font-semibold transition',
                  done
                    ? 'bg-emerald-100 text-emerald-700'
                    : active
                      ? 'bg-brand-600 text-white'
                      : 'bg-slate-100 text-slate-400',
                ].join(' ')}
              >
                {done ? <IconCheck className="h-3 w-3" /> : index + 1}
              </span>
              <span
                className={
                  done ? 'text-slate-500' : active ? 'font-medium text-slate-900' : 'text-slate-400'
                }
              >
                {step.label}
              </span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
