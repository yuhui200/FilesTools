import type { ReactNode } from 'react'

import { IconAlert, IconCheckCircle, IconX } from './Icons'

type Tone = 'error' | 'warning' | 'success' | 'info'

const TONES: Record<Tone, { wrapper: string; icon: string }> = {
  error: { wrapper: 'border-red-200 bg-red-50 text-red-800', icon: 'text-red-500' },
  warning: { wrapper: 'border-amber-200 bg-amber-50 text-amber-900', icon: 'text-amber-500' },
  success: { wrapper: 'border-emerald-200 bg-emerald-50 text-emerald-800', icon: 'text-emerald-500' },
  info: { wrapper: 'border-brand-200 bg-brand-50 text-brand-900', icon: 'text-brand-500' },
}

interface AlertProps {
  tone?: Tone
  children: ReactNode
  onDismiss?: () => void
}

export function Alert({ tone = 'info', children, onDismiss }: AlertProps) {
  const styles = TONES[tone]
  const Icon = tone === 'success' ? IconCheckCircle : IconAlert

  return (
    <div
      role={tone === 'error' ? 'alert' : 'status'}
      className={`flex items-start gap-2.5 rounded-xl border px-4 py-3 text-sm ${styles.wrapper}`}
    >
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${styles.icon}`} />
      <div className="min-w-0 flex-1 break-words">{children}</div>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="关闭提示"
          className="-mr-1 grid h-6 w-6 shrink-0 place-items-center rounded-md opacity-60 transition hover:bg-black/5 hover:opacity-100"
        >
          <IconX className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  )
}
