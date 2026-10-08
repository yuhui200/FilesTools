import type { ConversionBatchStatus } from '@/types'

import { ConversionTaskRow } from './ConversionTaskRow'

interface ConversionTaskListProps {
  batch: ConversionBatchStatus
  takenItems: ReadonlySet<string>
  downloading: boolean
  /** 正在重试的任务号 */
  retrying: string | null
  /** 桌面端各单项落盘后的绝对路径，键是任务号；Web 上恒为空对象 */
  savedItemPaths: Readonly<Record<string, string>>
  onRetry: (taskId: string) => void
  onDownload: (taskId: string, url: string, filename: string) => void
}

/**
 * 一组里每个文件的实时状态。
 *
 * 计数直接来自服务端快照（queued / processing / completed / failed / cancelled），
 * 页面上不放任何自己算的进度。超过 8 个文件时列表内部滚动。
 */
export function ConversionTaskList({
  batch,
  takenItems,
  downloading,
  retrying,
  savedItemPaths,
  onRetry,
  onDownload,
}: ConversionTaskListProps) {
  // 取消中的项在服务端仍算 processing，这里单列出来，计数才不会骗人
  const cancelling = batch.tasks.filter((task) => task.status === 'cancelling').length
  const processing = batch.processing - cancelling

  return (
    <div className="mt-4">
      <dl className="grid grid-cols-3 gap-2 sm:grid-cols-5">
        <Counter label="总任务" value={batch.total} />
        <Counter label="已完成" value={batch.completed} tone="emerald" />
        <Counter label="处理中" value={processing} tone="brand" />
        <Counter label="等待" value={batch.queued} />
        <Counter label="失败" value={batch.failed} tone={batch.failed > 0 ? 'red' : 'muted'} />
      </dl>
      {cancelling > 0 && (
        <p className="mt-2 text-xs text-amber-700">
          其中 {cancelling} 个已请求取消，正在等待当前文件处理完。
        </p>
      )}

      <ul
        className={[
          'mt-3 divide-y divide-slate-100 border-t border-slate-100',
          batch.tasks.length > 8 ? 'max-h-[26rem] overflow-y-auto' : '',
        ].join(' ')}
      >
        {batch.tasks.map((task) => (
          <ConversionTaskRow
            key={task.task_id}
            task={task}
            taken={takenItems.has(task.task_id)}
            downloading={downloading}
            retrying={retrying === task.task_id}
            savedPath={savedItemPaths[task.task_id] ?? null}
            onRetry={onRetry}
            onDownload={onDownload}
          />
        ))}
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
