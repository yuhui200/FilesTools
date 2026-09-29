import type { ReactNode } from 'react'

import type { TaskSnapshot, TaskState, TaskFile } from '@/types'
import { formatBytes } from '@/utils/format'
import { explainError } from '@/utils/errorMessages'

/**
 * 批量任务的进度面板（第四阶段 §3 / §4）。
 *
 * 显示三样东西，全部来自服务端的真实状态，没有任何模拟进度：
 *
 *   1. 整批进度条 + 「已完成 / 总数」（§3 要求的「18 / 50」）
 *   2. 总任务 / 已完成 / 处理中 / 等待 / 失败 的实时计数
 *   3. 每个文件的名称、大小、状态与失败原因（§4）
 *
 * 上传阶段服务端还没有任务记录，这时用本地文件列表先占位（全部是「等待中」），
 * 上传完成拿到第一份快照后，展示的就完全是服务端的状态了。
 */

interface TaskProgressPanelProps {
  /** 面板标题，例如「图片压缩」 */
  title: string
  /** 上传进度 0-100；null 表示上传已完成，看 snapshot 即可 */
  uploadPercent: number | null
  /** 服务端任务快照；上传完成前为 null */
  snapshot: TaskSnapshot | null
  /** 上传阶段先占位的本地文件 */
  pendingFiles?: File[]
  /** 整批失败时的原因（错误码 + 服务端原文案） */
  error?: { code: string; message: string } | null
  /** 整批失败时可以做的事，例如「返回修改设置」 */
  action?: ReactNode
}

/** 每个状态的标记与配色（§4：✓ / ⟳ / ○ / ✕） */
const STATE_STYLE: Record<TaskState, { mark: string; badge: string; text: string; spin?: boolean }> =
  {
    done: {
      mark: '✓',
      badge: 'bg-emerald-100 text-emerald-700',
      text: 'text-slate-700',
    },
    processing: {
      mark: '⟳',
      badge: 'bg-brand-100 text-brand-700',
      text: 'text-slate-900 font-medium',
      spin: true,
    },
    waiting: {
      mark: '○',
      badge: 'bg-slate-100 text-slate-400',
      text: 'text-slate-500',
    },
    failed: {
      mark: '✕',
      badge: 'bg-red-100 text-red-700',
      text: 'text-red-700',
    },
  }

interface Row {
  key: string
  filename: string
  size: number
  state: TaskState
  label: string
  /** 失败原因 */
  message: string | null
}

function rowsOf(snapshot: TaskSnapshot | null, pendingFiles: File[]): Row[] {
  if (snapshot) {
    return snapshot.tasks.map((task: TaskFile) => ({
      key: `task-${task.index}`,
      filename: task.filename,
      size: task.size,
      state: task.state,
      label: task.state_label,
      message: task.error_message,
    }))
  }
  return pendingFiles.map((file, index) => ({
    key: `local-${index}-${file.name}`,
    filename: file.name,
    size: file.size,
    state: 'waiting' as TaskState,
    label: '等待中',
    message: null,
  }))
}

export function TaskProgressPanel({
  title,
  uploadPercent,
  snapshot,
  pendingFiles = [],
  error = null,
  action = null,
}: TaskProgressPanelProps) {
  const uploading = uploadPercent !== null && !snapshot

  const total = snapshot?.total ?? pendingFiles.length
  const completed = snapshot?.completed ?? 0
  const processing = snapshot?.processing ?? 0
  const waiting = snapshot?.waiting ?? pendingFiles.length
  const failed = snapshot?.failed ?? 0
  const finished = completed + failed

  const percent = snapshot ? snapshot.percent : (uploadPercent ?? 0)
  const rows = rowsOf(snapshot, pendingFiles)

  const statusText = uploading
    ? `正在上传 ${total} 个文件…`
    : snapshot?.state === 'failed'
      ? '处理结束，但有文件未能完成'
      : failed > 0
        ? `${title}进行中，已跳过 ${failed} 个失败文件`
        : `${title}进行中…`

  const explanation = error ? explainError(error.code, error.message) : null

  return (
    <div className="card overflow-hidden" role="status" aria-live="polite">
      <div className="border-b border-slate-200 bg-slate-50/80 px-5 py-4 sm:px-6">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <p className="text-sm font-medium text-slate-900">{statusText}</p>
          <p className="text-sm tabular-nums text-slate-500">
            <span className="text-xl font-semibold text-slate-900">{finished}</span>
            <span className="mx-1">/</span>
            {total}
          </p>
        </div>

        <div
          className="mt-3 h-2.5 w-full overflow-hidden rounded-full bg-slate-200"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(percent)}
        >
          <div
            className={[
              'h-full rounded-full transition-[width] duration-300 ease-out',
              failed > 0 && finished === total ? 'bg-amber-500' : 'bg-brand-500',
            ].join(' ')}
            style={{ width: `${Math.max(Math.min(percent, 100), uploading ? 2 : 0)}%` }}
          />
        </div>

        {/* §3：总任务 / 已完成 / 处理中 / 等待 / 失败 */}
        <dl className="mt-4 grid grid-cols-3 gap-2 sm:grid-cols-5">
          <Counter label="总任务" value={total} />
          <Counter label="已完成" value={completed} tone="emerald" />
          <Counter label="处理中" value={processing} tone="brand" />
          <Counter label="等待" value={waiting} />
          <Counter label="失败" value={failed} tone={failed > 0 ? 'red' : 'muted'} />
        </dl>
      </div>

      {explanation && (
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-red-50/70 px-5 py-3 sm:px-6">
          <div className="min-w-0">
            <p className="text-sm font-medium text-red-800">{explanation.title}</p>
            {explanation.hint && <p className="mt-0.5 text-xs text-red-700">{explanation.hint}</p>}
          </div>
          {action}
        </div>
      )}

      {/* §4：逐文件状态 */}
      <ul
        className={[
          'divide-y divide-slate-100',
          rows.length > 8 ? 'max-h-[22rem] overflow-y-auto' : '',
        ].join(' ')}
      >
        {rows.map((row) => {
          const style = STATE_STYLE[row.state]
          return (
            <li key={row.key} className="flex items-start gap-3 px-5 py-2.5 sm:px-6">
              <span
                className={[
                  'mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full text-[11px] font-bold leading-none',
                  style.badge,
                ].join(' ')}
                aria-hidden="true"
              >
                <span className={style.spin ? 'inline-block animate-spin' : undefined}>
                  {style.mark}
                </span>
              </span>

              <span className="min-w-0 flex-1">
                <span className={`block truncate text-sm ${style.text}`} title={row.filename}>
                  {row.filename}
                </span>
                {row.message && (
                  <span className="mt-0.5 block text-xs text-red-600">{row.message}</span>
                )}
              </span>

              <span className="shrink-0 text-right">
                <span className="block text-xs tabular-nums text-slate-500">
                  {row.size > 0 ? formatBytes(row.size) : '—'}
                </span>
                <span className="block text-xs text-slate-400">{row.label}</span>
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function Counter({
  label,
  value,
  tone = 'muted',
}: {
  label: string
  value: number
  tone?: 'muted' | 'brand' | 'emerald' | 'red'
}) {
  const color =
    value === 0
      ? 'text-slate-400'
      : tone === 'brand'
        ? 'text-brand-700'
        : tone === 'emerald'
          ? 'text-emerald-700'
          : tone === 'red'
            ? 'text-red-700'
            : 'text-slate-900'

  return (
    <div className="rounded-lg bg-white px-3 py-2 ring-1 ring-slate-200">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className={`mt-0.5 text-base font-semibold tabular-nums ${color}`}>{value}</dd>
    </div>
  )
}
