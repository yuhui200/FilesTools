import { useState } from 'react'

import type { FilePreview } from '@/hooks/useFilePreviews'
import type { BatchResponse, ItemResult } from '@/types'
import { formatBytes, formatDimensions, formatLabel, formatPercent } from '@/utils/format'

import { Alert } from './Alert'
import { Button } from './Button'
import { DesktopSavedFile } from './DesktopSavedFile'
import { IconCheckCircle, IconDownload, IconFile } from './Icons'
import { ImageCompare, type CompareSide } from './ImageCompare'

interface BatchResultPanelProps {
  result: BatchResponse
  /** 与上传顺序一一对应的本地预览，用于展示「原图」 */
  previews: FilePreview[]
  onDownload: () => void
  onReset: () => void
  downloading: boolean
  downloadError: string | null
  /** 结果已被下载（或已过期），服务器上的文件已经删除 */
  expired?: boolean
  /**
   * 桌面端下载时落盘的绝对路径，用来渲染「已保存到 … / 打开 / 在文件夹中显示」。
   *
   * 不传或传 `null` 就什么都不渲染 —— Web 上恒为 `null`，因为下载交给了
   * 浏览器，我们既不知道也不该管它存到哪。
   */
  savedPath?: string | null
}

function toCompareSides(
  item: ItemResult,
  preview: FilePreview | undefined,
): { original: CompareSide; result: CompareSide } {
  return {
    original: {
      title: '原图',
      url: preview?.url ?? null,
      filename: item.original.filename,
      size: item.original.size,
      width: item.original.width ?? preview?.width ?? null,
      height: item.original.height ?? preview?.height ?? null,
      format: item.original.format,
      tone: 'muted',
    },
    result: {
      title: '处理后',
      url: item.preview_url,
      filename: item.result.filename,
      size: item.result.size,
      width: item.result.width,
      height: item.result.height,
      format: item.result.format,
      tone: 'brand',
    },
  }
}

function DetailRow({ item }: { item: ItemResult }) {
  const grew = item.result.size > item.original.size

  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-sm">
      <IconFile className="h-4 w-4 shrink-0 text-slate-400" />

      <span className="min-w-0 flex-1 truncate text-slate-700" title={item.original.filename}>
        {item.result.filename}
      </span>

      <span className="text-xs text-slate-500">
        {formatDimensions(item.original.width, item.original.height)}
        {item.result.width !== item.original.width && (
          <> → {formatDimensions(item.result.width, item.result.height)}</>
        )}
      </span>

      <span className="text-xs text-slate-400">{formatLabel(item.result.format)}</span>

      <span className="tabular-nums text-xs text-slate-600">
        {formatBytes(item.original.size)} → {formatBytes(item.result.size)}
      </span>

      {!grew && item.saved_bytes > 0 && (
        <span className="rounded-md bg-emerald-50 px-1.5 py-0.5 text-xs font-medium tabular-nums text-emerald-700">
          -{formatPercent(item.saved_percent)}
        </span>
      )}

      {!item.target_met && (
        <span className="rounded-md bg-amber-50 px-1.5 py-0.5 text-xs font-medium text-amber-700">
          未达目标大小
        </span>
      )}
    </li>
  )
}

/**
 * 格式转换 / 尺寸调整的结果面板。
 *
 * 单个文件时直接展示前后对比；多个文件时结果已打包成 ZIP，
 * 这里提供缩略图切换，逐个查看处理效果。
 */
export function BatchResultPanel({
  result,
  previews,
  onDownload,
  onReset,
  downloading,
  downloadError,
  expired = false,
  savedPath = null,
}: BatchResultPanelProps) {
  const [activeIndex, setActiveIndex] = useState(0)

  const items = result.items
  const active = items[Math.min(activeIndex, items.length - 1)]
  const saved = result.saved_bytes > 0
  const notes = items.filter((item) => item.note)

  if (!active) {
    return (
      <div className="card p-5 sm:p-6">
        <Alert tone="error">没有可展示的结果，请重新处理。</Alert>
      </div>
    )
  }

  const sides = toCompareSides(active, previews[active.index])

  return (
    <div className="card animate-fade-in-up overflow-hidden">
      <div className="flex items-center gap-3 border-b border-slate-200 bg-emerald-50/70 px-5 py-4 sm:px-6">
        <IconCheckCircle className="h-6 w-6 shrink-0 text-emerald-600" />
        <div>
          <p className="font-semibold text-emerald-900">
            {items.length > 1 ? `${items.length} 个文件处理完成` : '文件处理完成'}
          </p>
          <p className="text-xs text-emerald-700">
            {result.archived
              ? '结果已打包为 ZIP，下载后临时文件会自动从服务器删除'
              : '临时文件将在下载后自动从服务器删除'}
          </p>
        </div>
      </div>

      <div className="p-5 sm:p-6">
        <ImageCompare original={sides.original} result={sides.result} />

        {/* 多个文件时提供缩略图切换 */}
        {items.length > 1 && (
          <div className="mt-5">
            <p className="text-xs font-medium text-slate-500">选择要查看的文件</p>
            <div className="mt-2 flex gap-2 overflow-x-auto pb-1">
              {items.map((item, position) => (
                <button
                  key={item.index}
                  type="button"
                  onClick={() => setActiveIndex(position)}
                  aria-label={`查看 ${item.result.filename}`}
                  aria-pressed={position === activeIndex}
                  className={[
                    'h-14 w-14 shrink-0 overflow-hidden rounded-lg border-2 transition',
                    position === activeIndex
                      ? 'border-brand-500 ring-2 ring-brand-500/20'
                      : 'border-slate-200 hover:border-slate-300',
                  ].join(' ')}
                >
                  {previews[item.index]?.url ? (
                    <img
                      src={previews[item.index]?.url ?? undefined}
                      alt=""
                      className="h-full w-full object-cover"
                    />
                  ) : (
                    <span className="grid h-full w-full place-items-center bg-slate-100 text-slate-400">
                      <IconFile className="h-5 w-5" />
                    </span>
                  )}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* 整批统计 */}
        <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="文件数" value={`${items.length} 个`} />
          <Stat label="原始总大小" value={formatBytes(result.original_total)} />
          <Stat label="处理后总大小" value={formatBytes(result.result_total)} tone="brand" />
          <Stat
            label="共节省"
            value={saved ? formatPercent(result.saved_percent) : '—'}
            tone={saved ? 'emerald' : 'muted'}
          />
        </div>

        {!saved && (
          <div className="mt-4">
            <Alert tone="warning">
              这次处理没有减小文件体积。格式转换（例如 JPG 转 PNG）本身就可能让文件变大，
              这是正常现象。
            </Alert>
          </div>
        )}

        {notes.length > 0 && (
          <div className="mt-4 space-y-2">
            {notes.slice(0, 3).map((item) => (
              <Alert key={item.index} tone={item.target_met ? 'info' : 'warning'}>
                {item.original.filename}：{item.note}
              </Alert>
            ))}
          </div>
        )}

        {result.failures.length > 0 && (
          <div className="mt-4">
            <Alert tone="error">
              <p className="font-medium">
                有 {result.failures.length} 个文件未能处理：
              </p>
              <ul className="mt-1.5 space-y-0.5">
                {result.failures.map((failure) => (
                  <li key={failure.index}>
                    {failure.filename} —— {failure.message}
                  </li>
                ))}
              </ul>
            </Alert>
          </div>
        )}

        {/* 明细 */}
        <ul className="mt-5 divide-y divide-slate-100 rounded-xl border border-slate-200">
          {items.map((item) => (
            <DetailRow key={item.index} item={item} />
          ))}
        </ul>

        {downloadError && (
          <div className="mt-4">
            <Alert tone="error">{downloadError}</Alert>
          </div>
        )}

        {expired && (
          <div className="mt-4">
            <Alert tone="info">
              结果文件已经下载并从服务器删除。下载链接只能使用一次，
              需要再次下载请重新处理一次。
            </Alert>
          </div>
        )}

        <div className="mt-6 flex flex-col gap-3 sm:flex-row">
          {!expired && (
            <Button
              size="lg"
              onClick={onDownload}
              loading={downloading}
              icon={<IconDownload className="h-4 w-4" />}
              className="sm:flex-1"
            >
              {result.archived ? `下载全部（${items.length} 个文件）` : '下载文件'}
            </Button>
          )}
          <Button
            size="lg"
            variant={expired ? 'primary' : 'secondary'}
            onClick={onReset}
            disabled={downloading}
            className={expired ? 'sm:flex-1' : undefined}
          >
            重新处理
          </Button>
        </div>

        {/* 桌面端落盘之后的位置与后续动作（Web 上这个组件不渲染） */}
        <DesktopSavedFile path={savedPath} />
      </div>
    </div>
  )
}

function Stat({
  label,
  value,
  tone = 'muted',
}: {
  label: string
  value: string
  tone?: 'muted' | 'brand' | 'emerald'
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p
        className={[
          'mt-1 text-base font-semibold tabular-nums',
          tone === 'brand'
            ? 'text-brand-700'
            : tone === 'emerald'
              ? 'text-emerald-700'
              : 'text-slate-900',
        ].join(' ')}
      >
        {value}
      </p>
    </div>
  )
}
