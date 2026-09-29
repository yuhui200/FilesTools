import { useState } from 'react'

import { formatBytes } from '@/utils/format'

import { IconArrowDown, IconArrowUp, IconFile, IconGrip, IconTrash } from './Icons'

export interface SortableEntry {
  file: File
  /** 图片可以有缩略图；PDF 没有，显示文件图标 */
  previewUrl?: string | null
  /** 名称下方的补充信息，例如「1600 × 1200」「共 12 页」 */
  meta?: string | null
}

interface SortableFileListProps {
  entries: SortableEntry[]
  onRemove: (index: number) => void
  onMove: (from: number, to: number) => void
  disabled?: boolean
  /** 序号单位，例如「页」「个」，显示为「第 3 页」 */
  unitLabel?: string
  /** 顺序对结果的影响，显示在标题下方 */
  hint?: string
}

/**
 * 可调整顺序的文件列表。
 *
 * 第三阶段 §3（图片转 PDF）和 §6（PDF 合并）都要求「拖动调整顺序」。
 * 这里用浏览器原生的 HTML5 拖放实现，不引入拖拽库 ——
 * 只需要一个列表内部的排序，引一个几百 KB 的依赖不划算。
 * 同时保留上移 / 下移按钮：键盘用户和触屏用户拖不动，
 * 但用按钮一样能调整顺序（§6 明确要求这两个按钮）。
 */
export function SortableFileList({
  entries,
  onRemove,
  onMove,
  disabled = false,
  unitLabel = '个',
  hint,
}: SortableFileListProps) {
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [overIndex, setOverIndex] = useState<number | null>(null)

  const resetDrag = () => {
    setDragIndex(null)
    setOverIndex(null)
  }

  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 className="text-sm font-medium text-slate-900">已选 {entries.length} 个文件</h3>
        <span className="text-xs text-slate-500">
          合计 {formatBytes(entries.reduce((sum, entry) => sum + entry.file.size, 0))}
        </span>
      </div>

      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}

      <ul className={['mt-3 space-y-2', entries.length > 6 ? 'max-h-[26rem] overflow-y-auto pr-1' : ''].join(' ')}>
        {entries.map((entry, index) => {
          const dragging = dragIndex === index
          const targeted = overIndex === index && dragIndex !== null && dragIndex !== index

          return (
            <li
              key={`${entry.file.name}-${entry.file.size}-${entry.file.lastModified}`}
              draggable={!disabled}
              onDragStart={(event) => {
                if (disabled) return
                setDragIndex(index)
                event.dataTransfer.effectAllowed = 'move'
                // Firefox 必须设置数据，否则不会开始拖动
                event.dataTransfer.setData('text/plain', String(index))
              }}
              onDragOver={(event) => {
                if (disabled || dragIndex === null) return
                event.preventDefault()
                event.dataTransfer.dropEffect = 'move'
                setOverIndex(index)
              }}
              onDrop={(event) => {
                if (disabled) return
                event.preventDefault()
                if (dragIndex !== null && dragIndex !== index) onMove(dragIndex, index)
                resetDrag()
              }}
              onDragEnd={resetDrag}
              className={[
                'flex items-center gap-2.5 rounded-xl border bg-white p-2.5 shadow-sm transition',
                targeted
                  ? 'border-brand-500 ring-2 ring-brand-500/20'
                  : 'border-slate-200',
                dragging ? 'opacity-50' : '',
                disabled ? '' : 'cursor-grab active:cursor-grabbing',
              ]
                .filter(Boolean)
                .join(' ')}
            >
              <span
                aria-hidden="true"
                className={`shrink-0 ${disabled ? 'text-slate-200' : 'text-slate-300'}`}
                title="拖动可调整顺序"
              >
                <IconGrip className="h-4 w-4" />
              </span>

              <span className="w-11 shrink-0 text-center text-xs font-medium tabular-nums text-slate-500">
                第 {index + 1} {unitLabel}
              </span>

              <span className="grid h-12 w-12 shrink-0 place-items-center overflow-hidden rounded-lg bg-slate-100">
                {entry.previewUrl ? (
                  <img src={entry.previewUrl} alt="" className="h-full w-full object-cover" />
                ) : (
                  <IconFile className="h-5 w-5 text-slate-400" />
                )}
              </span>

              <span className="min-w-0 flex-1">
                <span
                  className="block truncate text-sm font-medium text-slate-900"
                  title={entry.file.name}
                >
                  {entry.file.name}
                </span>
                <span className="mt-0.5 block truncate text-xs text-slate-500">
                  {formatBytes(entry.file.size)}
                  {entry.meta ? ` · ${entry.meta}` : ''}
                </span>
              </span>

              <span className="flex shrink-0 items-center gap-0.5">
                <IconButton
                  label={`把 ${entry.file.name} 上移`}
                  disabled={disabled || index === 0}
                  onClick={() => onMove(index, index - 1)}
                >
                  <IconArrowUp className="h-4 w-4" />
                </IconButton>
                <IconButton
                  label={`把 ${entry.file.name} 下移`}
                  disabled={disabled || index === entries.length - 1}
                  onClick={() => onMove(index, index + 1)}
                >
                  <IconArrowDown className="h-4 w-4" />
                </IconButton>
                <IconButton
                  label={`移除 ${entry.file.name}`}
                  disabled={disabled}
                  danger
                  onClick={() => onRemove(index)}
                >
                  <IconTrash className="h-4 w-4" />
                </IconButton>
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function IconButton({
  label,
  onClick,
  disabled,
  danger = false,
  children,
}: {
  label: string
  onClick: () => void
  disabled: boolean
  danger?: boolean
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={[
        'grid h-11 w-11 place-items-center rounded-lg text-slate-400 transition sm:h-8 sm:w-8',
        'disabled:cursor-not-allowed disabled:opacity-30',
        danger
          ? 'hover:bg-red-50 hover:text-red-600'
          : 'hover:bg-slate-100 hover:text-slate-700',
      ].join(' ')}
    >
      {children}
    </button>
  )
}
