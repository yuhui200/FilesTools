import type { ReactNode } from 'react'

import type { PdfResultResponse } from '@/types'
import { formatBytes, formatPercent } from '@/utils/format'

import { Alert } from './Alert'
import { Button } from './Button'
import { DesktopSavedFile } from './DesktopSavedFile'
import { IconCheckCircle, IconDownload, IconFile } from './Icons'

export interface ResultStat {
  label: string
  value: string
  tone?: 'muted' | 'brand' | 'emerald'
}

interface PdfResultPanelProps {
  result: PdfResultResponse
  /** 这次的看点，由各页面按功能给出（页数、压缩前后、节省比例…） */
  stats: ResultStat[]
  /** 完成文案，例如压缩页用「✓ 压缩完成」 */
  doneTitle?: string
  onDownload: () => void
  /** 保留已上传的文件，换参数再来一次 */
  onReset: () => void
  resetLabel?: string
  /** 换一份文件重新开始 */
  onRestart?: () => void
  restartLabel?: string
  downloading: boolean
  downloadError: string | null
  /**
   * 下载按钮的文案。默认按「打包成了 ZIP 没有」自动取，
   * 认得出具体格式的页面可以传得更具体（例如「下载 Word」）。
   */
  downloadLabel?: string
  /** 结果本身就是图片时（PDF 转图片）展示缩略图 */
  showImageGrid?: boolean
  /** 结果已被下载（或已过期），服务器上的文件已经删除 */
  expired?: boolean
  /**
   * 桌面端下载时落盘的绝对路径，用来渲染「已保存到 … / 打开 / 在文件夹中显示」。
   *
   * 不传或传 `null` 就什么都不渲染 —— Web 上恒为 `null`，因为下载交给了
   * 浏览器，我们既不知道也不该管它存到哪。
   */
  savedPath?: string | null
  children?: ReactNode
}

/**
 * PDF 工具的统一结果面板。
 *
 * 六个功能的产物差别很大（一个 PDF、一堆图片、一个 ZIP），
 * 但用户想知道的都是同一件事：生成了什么、多少页、多大、省了多少。
 * 因此这里只负责把「结果」讲清楚，具体看哪些指标由各页面通过 stats 决定。
 */
export function PdfResultPanel({
  result,
  stats,
  doneTitle = '✓ 处理完成',
  onDownload,
  onReset,
  resetLabel = '重新处理',
  onRestart,
  restartLabel = '换一份文件',
  downloading,
  downloadError,
  downloadLabel,
  showImageGrid = false,
  expired = false,
  savedPath = null,
  children,
}: PdfResultPanelProps) {
  const images = showImageGrid ? result.files.filter((file) => file.preview_url) : []

  return (
    <div className="card animate-fade-in-up overflow-hidden">
      <div className="flex items-center gap-3 border-b border-slate-200 bg-emerald-50/70 px-5 py-4 sm:px-6">
        <IconCheckCircle className="h-6 w-6 shrink-0 text-emerald-600" />
        <div className="min-w-0">
          <p className="font-semibold text-emerald-900">{doneTitle}</p>
          <p className="text-xs text-emerald-700">
            {result.archived
              ? `已打包成 ${result.archive_filename ?? 'ZIP'}，下载后临时文件会自动从服务器删除`
              : '临时文件将在下载后自动从服务器删除'}
          </p>
        </div>
      </div>

      <div className="p-5 sm:p-6">
        {/* 生成的文件 */}
        <div className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-white text-slate-400">
            <IconFile className="h-5 w-5" />
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium text-slate-900" title={result.filename}>
              {result.filename}
            </p>
            <p className="mt-0.5 text-xs text-slate-500">
              {result.archived && result.files.length > 0
                ? `共 ${result.files.length} 个文件 · 合计 ${formatBytes(result.size)}`
                : formatBytes(result.size)}
            </p>
          </div>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {stats.map((stat) => (
            <Stat key={stat.label} {...stat} />
          ))}
        </div>

        {/* 说明：服务端给出的提示原样展示，不加工也不隐瞒 */}
        {result.notes.length > 0 && (
          <div className="mt-4 space-y-2">
            {result.notes.map((note) => (
              <Alert key={note} tone="info">
                {note}
              </Alert>
            ))}
          </div>
        )}

        {/* PDF 转图片的结果缩略图 */}
        {images.length > 0 && (
          <div className="mt-5">
            <p className="text-xs font-medium text-slate-500">
              生成 {images.length} 张图片
              {images.length > 12 ? '（仅展示前 12 张）' : ''}
            </p>
            <div className="mt-2 grid grid-cols-3 gap-2 sm:grid-cols-6">
              {images.slice(0, 12).map((file) => (
                <figure key={file.filename} className="min-w-0">
                  <div className="aspect-[3/4] overflow-hidden rounded-lg border border-slate-200 bg-slate-50">
                    <img
                      src={file.preview_url ?? undefined}
                      alt={file.filename}
                      loading="lazy"
                      className="h-full w-full object-cover"
                    />
                  </div>
                  <figcaption
                    className="mt-1 truncate text-[11px] text-slate-500"
                    title={file.filename}
                  >
                    {file.filename}
                  </figcaption>
                </figure>
              ))}
            </div>
          </div>
        )}

        {children}

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
              {downloadLabel ?? (result.archived ? '下载 ZIP' : '下载文件')}
            </Button>
          )}
          <Button
            size="lg"
            variant={expired ? 'primary' : 'secondary'}
            onClick={onReset}
            disabled={downloading}
            className={expired ? 'sm:flex-1' : undefined}
          >
            {resetLabel}
          </Button>
          {onRestart && (
            <Button size="lg" variant="ghost" onClick={onRestart} disabled={downloading}>
              {restartLabel}
            </Button>
          )}
        </div>

        {/* 桌面端落盘之后的位置与后续动作（Web 上这个组件不渲染） */}
        <DesktopSavedFile path={savedPath} />
      </div>
    </div>
  )
}

function Stat({ label, value, tone = 'muted' }: ResultStat) {
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

/** 压缩这类「前后对比」的三块指标，几个页面共用。 */
export function compareStats(
  originalSize: number,
  resultSize: number,
  savedPercent: number | null,
  savedBytes: number | null,
): ResultStat[] {
  const saved = (savedBytes ?? 0) > 0
  return [
    { label: '原文件', value: formatBytes(originalSize) },
    { label: '处理后', value: formatBytes(resultSize), tone: 'brand' },
    {
      label: '共节省',
      value: saved && savedPercent !== null ? formatPercent(savedPercent) : '—',
      tone: saved ? 'emerald' : 'muted',
    },
  ]
}
