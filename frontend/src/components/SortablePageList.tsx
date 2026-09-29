import { useState } from 'react'

import { IconArrowDown, IconArrowUp, IconGrip, IconTrash } from './Icons'

interface SortablePageListProps {
  /** 缩略图地址前缀，取第 N 页（从 0 开始）时拼上 /N */
  thumbnailBase: string
  /** 按输出顺序排列的页码，1 开始 */
  pages: number[]
  onMove: (from: number, to: number) => void
  onRemove: (page: number) => void
  disabled?: boolean
}

/**
 * 按输出顺序排列的已选页面（第四阶段 §11）。
 *
 * 「PDF 页面处理」要求支持拖拽排序，且**最终输出必须按照新顺序** ——
 * 提取页面、自定义页面拆分出来的那份 PDF，第 1 页就是列表里的第 1 项。
 *
 * 与 SortableFileList 一样用浏览器原生 HTML5 拖放，不引入拖拽库；
 * 同时保留上移 / 下移按钮：触屏拖不动，键盘用户也拖不动，
 * 但用按钮一样能调整顺序（§11 明确要求这两个按钮）。
 */
export function SortablePageList({
  thumbnailBase,
  pages,
  onMove,
  onRemove,
  disabled = false,
}: SortablePageListProps) {
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [overIndex, setOverIndex] = useState<number | null>(null)

  const resetDrag = () => {
    setDragIndex(null)
    setOverIndex(null)
  }

  if (pages.length === 0) return null

  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h4 className="text-sm font-medium text-slate-900">输出顺序（共 {pages.length} 页）</h4>
        <span className="text-xs text-slate-500">拖动或用箭头调整，结果按这个顺序生成</span>
      </div>

      <ul
        className={[
          'mt-3 space-y-2',
          pages.length > 6 ? 'max-h-[26rem] overflow-y-auto pr-1' : '',
        ].join(' ')}
      >
        {pages.map((page, index) => {
          const dragging = dragIndex === index
          const targeted = overIndex === index && dragIndex !== null && dragIndex !== index

          return (
            <li
              key={`${page}-${index}`}
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
                targeted ? 'border-brand-500 ring-2 ring-brand-500/20' : 'border-slate-200',
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

              <span className="w-9 shrink-0 text-center text-xs font-medium tabular-nums text-slate-500 sm:w-12">
                第 {index + 1} 位
              </span>

              <span className="grid h-10 w-10 shrink-0 place-items-center overflow-hidden rounded-lg border border-slate-200 bg-slate-50 sm:h-12 sm:w-12">
                <img
                  src={`${thumbnailBase}/${page - 1}`}
                  alt={`原第 ${page} 页`}
                  loading="lazy"
                  className="h-full w-full object-cover"
                />
              </span>

              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-slate-900">
                  原第 {page} 页
                </span>
                <span className="mt-0.5 block text-xs text-slate-500">
                  输出时排在第 {index + 1} 页
                </span>
              </span>

              <span className="flex shrink-0 items-center gap-0.5">
                <IconButton
                  label={`把原第 ${page} 页上移`}
                  disabled={disabled || index === 0}
                  onClick={() => onMove(index, index - 1)}
                >
                  <IconArrowUp className="h-4 w-4" />
                </IconButton>
                <IconButton
                  label={`把原第 ${page} 页下移`}
                  disabled={disabled || index === pages.length - 1}
                  onClick={() => onMove(index, index + 1)}
                >
                  <IconArrowDown className="h-4 w-4" />
                </IconButton>
                <IconButton
                  label={`移除原第 ${page} 页`}
                  disabled={disabled}
                  danger
                  onClick={() => onRemove(page)}
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
        'grid h-11 w-11 place-items-center rounded-lg text-slate-400 transition sm:h-9 sm:w-9',
        'disabled:cursor-not-allowed disabled:opacity-30',
        danger ? 'hover:bg-red-50 hover:text-red-600' : 'hover:bg-slate-100 hover:text-slate-700',
      ].join(' ')}
    >
      {children}
    </button>
  )
}
