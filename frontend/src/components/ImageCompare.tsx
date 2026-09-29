import { useState } from 'react'

import { formatBytes, formatDimensions, formatLabel } from '@/utils/format'

import { IconImage } from './Icons'

export interface CompareSide {
  title: string
  /** 图片地址；原图用本地 object URL，处理后用后端预览地址 */
  url: string | null
  filename: string
  size: number
  width: number | null
  height: number | null
  format: string | null
  tone: 'muted' | 'brand'
}

function Side({ side }: { side: CompareSide }) {
  const [failed, setFailed] = useState(false)
  const showImage = side.url !== null && !failed

  return (
    <figure className="min-w-0 flex-1">
      <figcaption className="mb-2 flex items-baseline justify-between gap-2">
        <span
          className={[
            'text-xs font-medium uppercase tracking-wide',
            side.tone === 'brand' ? 'text-brand-700' : 'text-slate-500',
          ].join(' ')}
        >
          {side.title}
        </span>
        <span className="text-xs text-slate-500">{formatLabel(side.format)}</span>
      </figcaption>

      <div
        className={[
          'grid h-56 place-items-center overflow-hidden rounded-xl border sm:h-64',
          side.tone === 'brand'
            ? 'border-brand-200 bg-brand-50/50'
            : 'border-slate-200 bg-slate-50',
        ].join(' ')}
      >
        {showImage ? (
          <img
            src={side.url ?? undefined}
            alt={`${side.title}：${side.filename}`}
            onError={() => setFailed(true)}
            className="h-full w-full object-contain"
          />
        ) : (
          <span className="flex flex-col items-center gap-2 text-slate-400">
            <IconImage className="h-7 w-7" />
            <span className="text-xs">{failed ? '预览不可用' : '暂无预览'}</span>
          </span>
        )}
      </div>

      <div className="mt-2.5 space-y-1">
        <p className="truncate text-sm font-medium text-slate-900" title={side.filename}>
          {side.filename}
        </p>
        <p className="text-xs text-slate-500">
          {formatBytes(side.size)} · {formatDimensions(side.width, side.height)}
        </p>
      </div>
    </figure>
  )
}

/**
 * 处理前后对比预览：左原图、右处理后。
 *
 * 图片用 object-contain 完整显示，不做裁剪，方便肉眼判断画质变化。
 */
export function ImageCompare({ original, result }: { original: CompareSide; result: CompareSide }) {
  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:gap-5">
      <Side side={original} />
      <Side side={result} />
    </div>
  )
}
