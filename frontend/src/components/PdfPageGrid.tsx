import { useState } from 'react'

import { padPage } from '@/utils/pdf'

import { Button } from './Button'
import { IconCheck, IconFile } from './Icons'

interface PdfPageGridProps {
  /** 缩略图地址前缀，取第 N 页（从 0 开始）时拼上 /N */
  thumbnailBase: string
  pageCount: number
  /** 最多渲染多少张缩略图（来自服务端配置） */
  thumbnailLimit: number
  /** 已选页码（1 开始，升序） */
  selected: number[]
  onToggle: (page: number) => void
  onSelectAll: () => void
  onClear: () => void
  /** 选中态的语气：删除用警示色，提取用主题色 */
  tone: 'danger' | 'brand'
  disabled?: boolean
}

/**
 * 页面缩略图选择器（§8 删除页面 / §9 提取页面共用）。
 *
 * 缩略图由后端逐页渲染（``GET /api/pdf/input/{id}/thumb/{page}``，从 0 开始），
 * 这里直接用 <img> 拉取并懒加载，不占用下载令牌，可以反复看。
 *
 * 只渲染前 ``thumbnailLimit`` 页：一份 300 页的 PDF 全部渲染要几十秒，
 * 而用户翻到第 200 页的概率很低。超出的部分提示用户直接填页码，
 * 页码输入框和缩略图操作的是同一份选择。
 */
export function PdfPageGrid({
  thumbnailBase,
  pageCount,
  thumbnailLimit,
  selected,
  onToggle,
  onSelectAll,
  onClear,
  tone,
  disabled = false,
}: PdfPageGridProps) {
  const [failed, setFailed] = useState<Set<number>>(new Set())

  const shown = Math.min(pageCount, Math.max(1, thumbnailLimit))
  const pages = Array.from({ length: shown }, (_, index) => index + 1)
  const selectedSet = new Set(selected)

  const accent =
    tone === 'danger'
      ? { ring: 'border-red-500 ring-2 ring-red-500/30', badge: 'bg-red-600', caption: '将删除' }
      : { ring: 'border-brand-500 ring-2 ring-brand-500/30', badge: 'bg-brand-600', caption: '已选中' }

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <p className="text-xs text-slate-500">
          共 {pageCount} 页 · 点击缩略图选择页面，已选{' '}
          <span className="font-medium text-slate-900">{selected.length}</span> 页
        </p>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="secondary" onClick={onSelectAll} disabled={disabled}>
            全选
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={onClear}
            disabled={disabled || selected.length === 0}
          >
            清空选择
          </Button>
        </div>
      </div>

      <ul className="mt-3 grid max-h-[30rem] grid-cols-2 gap-3 overflow-y-auto pr-1 sm:grid-cols-3 lg:grid-cols-4">
        {pages.map((page) => {
          const isSelected = selectedSet.has(page)
          const broken = failed.has(page)

          return (
            <li key={page}>
              <button
                type="button"
                onClick={() => onToggle(page)}
                disabled={disabled}
                aria-pressed={isSelected}
                aria-label={`第 ${page} 页${isSelected ? '（已选中）' : ''}`}
                className={[
                  'group relative w-full overflow-hidden rounded-xl border bg-white text-left transition',
                  'disabled:cursor-not-allowed disabled:opacity-60',
                  isSelected ? accent.ring : 'border-slate-200 hover:border-slate-300',
                ].join(' ')}
              >
                <span className="block aspect-[3/4] bg-slate-50">
                  {broken ? (
                    <span className="grid h-full w-full place-items-center text-slate-300">
                      <IconFile className="h-6 w-6" />
                    </span>
                  ) : (
                    <img
                      src={`${thumbnailBase}/${page - 1}`}
                      alt={`第 ${page} 页预览`}
                      loading="lazy"
                      onError={() =>
                        setFailed((current) => new Set(current).add(page))
                      }
                      className="h-full w-full object-contain"
                    />
                  )}
                </span>

                <span className="flex items-center justify-between gap-2 border-t border-slate-100 px-2.5 py-1.5">
                  <span className="text-xs font-medium tabular-nums text-slate-600">
                    Page {padPage(page)}
                  </span>
                  {isSelected && (
                    <span
                      className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium text-white ${accent.badge}`}
                    >
                      <IconCheck className="h-2.5 w-2.5" />
                      {accent.caption}
                    </span>
                  )}
                </span>
              </button>
            </li>
          )
        })}
      </ul>

      {pageCount > shown && (
        <p className="mt-3 text-xs text-slate-500">
          为保证响应速度，这里只显示前 {shown} 页的缩略图。第 {shown + 1} 页及以后的页面，
          可以直接在下面的页码框里填写（例如
          <code className="mx-1 rounded bg-slate-100 px-1 py-0.5 text-slate-700">
            {shown + 1}-{Math.min(pageCount, shown + 3)}
          </code>
          ），效果与点击缩略图完全一样。
        </p>
      )}
    </div>
  )
}
