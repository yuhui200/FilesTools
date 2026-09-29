import { Button } from '@/components/Button'
import { IconDownload } from '@/components/Icons'
import type { ConversionStatus, ConversionTaskStatus } from '@/types'
import { conversionStageLabel, conversionStatusLabel } from '@/utils/conversion'
import { explainError } from '@/utils/errorMessages'
import { formatBytes } from '@/utils/format'

interface ConversionTaskRowProps {
  task: ConversionTaskStatus
  /** 结果已经被取走（一次性令牌，取过就没了） */
  taken: boolean
  /** 正在下载 */
  downloading: boolean
  /** 正在重试这一项 */
  retrying: boolean
  onRetry: (taskId: string) => void
  onDownload: (taskId: string, url: string, filename: string) => void
}

/**
 * 省下了百分之多少；**没法算就是 ``null``**（不返回 0）。
 *
 * 原始大小是 0 时不能除；变大了的时候这个数会是负的，那是有意义的
 * 信息（换格式后反而更大），照实显示比藏起来好。取整后为 0% 也返回
 * ``null`` —— 显示一个「省了 0%」只是噪音。
 */
function savedPercent(before: number, after: number): string | null {
  if (before <= 0) return null
  const percent = Math.round((1 - after / before) * 100)
  return percent === 0 ? null : `${percent > 0 ? '-' : '+'}${Math.abs(percent)}%`
}

/** 状态标记与配色，与队列面板同一套符号（✓ / ⟳ / ○ / ✕） */
const STATUS_STYLE: Record<
  ConversionStatus,
  { mark: string; badge: string; text: string; spin?: boolean }
> = {
  queued: { mark: '○', badge: 'bg-slate-100 text-slate-400', text: 'text-slate-500' },
  processing: {
    mark: '⟳',
    badge: 'bg-brand-100 text-brand-700',
    text: 'text-slate-900 font-medium',
    spin: true,
  },
  // 取消请求已经发出，但活还停不下来 —— 用等待的配色，不用「已停止」的灰色
  cancelling: {
    mark: '⟳',
    badge: 'bg-amber-100 text-amber-700',
    text: 'text-amber-800',
    spin: true,
  },
  completed: { mark: '✓', badge: 'bg-emerald-100 text-emerald-700', text: 'text-slate-700' },
  failed: { mark: '✕', badge: 'bg-red-100 text-red-700', text: 'text-red-700' },
  cancelled: { mark: '—', badge: 'bg-slate-100 text-slate-400', text: 'text-slate-400' },
}

/**
 * 批次里的一个文件。
 *
 * 进度条只在**服务端给出真实百分比**时才按比例走；拿不到百分比的转换
 * （Office → PDF、图片 → 图片这些不写进度表的）走不确定态动画，
 * 绝不编一个 50% / 70% 出来（§十二）。有真实页码的（PDF → Word 走 OCR）
 * 就把页码一并显示出来。
 */
export function ConversionTaskRow({
  task,
  taken,
  downloading,
  retrying,
  onRetry,
  onDownload,
}: ConversionTaskRowProps) {
  const style = STATUS_STYLE[task.status]
  const stage = task.status === 'processing' ? conversionStageLabel(task.stage) : null
  const explanation = task.status === 'failed' ? explainError(task.error_code, task.error_message) : null

  const result = task.result
  const showDownload = task.status === 'completed' && result !== null

  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-2 px-5 py-3 sm:px-6">
      <span
        className={[
          'mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full text-[11px] font-bold leading-none',
          style.badge,
        ].join(' ')}
        aria-hidden="true"
      >
        <span className={style.spin ? 'inline-block animate-spin' : undefined}>{style.mark}</span>
      </span>

      <div className="min-w-0 flex-1">
        <p className={`truncate text-sm ${style.text}`} title={task.source_filename}>
          {task.source_filename}
        </p>

        {/* 具体一点的状态说明：阶段名 / 页码 / 取消说明 / 自动重排，都是服务端给的真实信息 */}
        <p className="mt-0.5 text-xs text-slate-500">
          {conversionStatusLabel(task.status)}
          {/* 服务器**自己**把这一项重新排进了队（第八阶段）。
              这不是装饰：用户没点任何按钮，这一项却回到了排队状态，
              不说一句的话界面看起来就像倒退了。 */}
          {task.status === 'queued' && task.auto_retry_count > 0 && ' · 重新排队…'}
          {task.status === 'cancelling' && '：已请求取消，正在等待当前文件处理完'}
          {stage && ` · ${stage}`}
          {task.page !== null &&
            task.page_count !== null &&
            ` · 第 ${task.page} / ${task.page_count} 页`}
        </p>

        {explanation && (
          <p className="mt-1 text-xs text-red-600">
            {explanation.title}
            {explanation.hint && <span className="text-red-500"> {explanation.hint}</span>}
          </p>
        )}

        {/* 真实百分比：只在这一项真的报了进度时才画 */}
        {task.status === 'processing' && task.progress !== null && (
          <div
            className="mt-2 h-1.5 w-full max-w-xs overflow-hidden rounded-full bg-slate-200"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(task.progress)}
          >
            <div
              className="h-full rounded-full bg-brand-500 transition-[width] duration-300 ease-out"
              style={{ width: `${Math.min(Math.max(task.progress, 0), 100)}%` }}
            />
          </div>
        )}
        {/* 没有真实百分比：走不确定态，不编数字 */}
        {task.status === 'processing' && task.progress === null && (
          <div className="mt-2 h-1.5 w-full max-w-xs overflow-hidden rounded-full bg-slate-200">
            <div className="h-full w-1/3 animate-pulse rounded-full bg-brand-400" />
          </div>
        )}
      </div>

      {/* 压缩前后（§三十一）：只在服务端真的报了原始大小时显示。
          没报就是这类转换不谈压缩，这里也不编一个「优化了 0%」出来。 */}
      {showDownload && result.original_size !== null && (
        <div className="w-full shrink-0 sm:w-auto">
          <p className="text-xs tabular-nums text-slate-600">
            {formatBytes(result.original_size)}
            <span className="mx-1 text-slate-400">→</span>
            <span className="font-medium text-slate-900">{formatBytes(result.size)}</span>
            {savedPercent(result.original_size, result.size) !== null && (
              <span className="ml-1.5 text-emerald-700">
                {savedPercent(result.original_size, result.size)}%
              </span>
            )}
          </p>
          {/* 要了目标大小但没做到：如实说，不装成功（§三十二）。
              ``null`` 是「没有目标可谈」，不在这里出现。 */}
          {result.target_reached === false && (
            <p className="mt-0.5 text-xs text-amber-700">
              未达到目标大小
              {result.target_size !== null && `（目标 ${formatBytes(result.target_size)}）`}
            </p>
          )}
        </div>
      )}

      {/* 内联预览（§三十九–§四十一）。地址由服务端给，给不出来就没有这一块。
          预览**不消耗**下载令牌，所以它可以和下载按钮并排存在。 */}
      {showDownload && result.preview_url !== null && (
        <a
          href={result.preview_url}
          target="_blank"
          rel="noreferrer"
          title={`预览 ${result.filename}`}
          className="shrink-0 self-center rounded-lg ring-1 ring-slate-200 transition hover:ring-brand-400"
        >
          <img
            src={result.preview_url}
            alt={`${result.filename} 的预览`}
            loading="lazy"
            className="h-12 w-12 rounded-lg object-cover"
          />
        </a>
      )}

      {/* 结果大小 + 单项下载 */}
      {showDownload && (
        <div className="flex shrink-0 items-center gap-3">
          {/* 上面那行已经报过「原来 → 现在」的就不重复报了 */}
          {result.original_size === null && (
            <span className="text-xs tabular-nums text-slate-500">
              {formatBytes(result.size)}
            </span>
          )}
          {taken ? (
            <span className="text-xs text-slate-400">已取走</span>
          ) : (
            <Button
              size="sm"
              variant="secondary"
              loading={downloading}
              icon={<IconDownload className="h-4 w-4" />}
              onClick={() => onDownload(task.task_id, result.download_url, result.filename)}
            >
              下载
            </Button>
          )}
        </div>
      )}

      {/* 重试：只对失败项开放，且服务端说能重试才给按钮（§十六） */}
      {task.status === 'failed' && task.can_retry && (
        <Button
          size="sm"
          variant="secondary"
          loading={retrying}
          onClick={() => onRetry(task.task_id)}
          className="shrink-0"
        >
          重试
        </Button>
      )}
      {task.status === 'failed' && !task.can_retry && task.retry_count > 0 && (
        <span className="shrink-0 text-xs text-slate-400">已重试过</span>
      )}
    </li>
  )
}
