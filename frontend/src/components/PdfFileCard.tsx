import { formatBytes } from '@/utils/format'

import { Button } from './Button'
import { IconFile, IconX } from './Icons'

interface PdfFileCardProps {
  filename: string
  size: number
  /** 服务端识别出的真实页数；尚未上传完成时为 null */
  pageCount?: number | null
  /** 换一份文件 */
  onRemove?: () => void
  removeLabel?: string
  disabled?: boolean
}

/**
 * 已上传 PDF 的信息条。
 *
 * §4 要求「上传后显示文件名、大小、页数」—— 页数是服务端真正打开文件后
 * 数出来的，不是前端猜的，所以它在页面选择器里可以放心使用。
 */
export function PdfFileCard({
  filename,
  size,
  pageCount = null,
  onRemove,
  removeLabel = '换一份文件',
  disabled = false,
}: PdfFileCardProps) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
      <span className="grid h-11 w-11 shrink-0 place-items-center rounded-lg bg-brand-50 text-brand-600">
        <IconFile className="h-5 w-5" />
      </span>

      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-slate-900" title={filename}>
          {filename}
        </p>
        <p className="mt-0.5 text-xs text-slate-500">
          {formatBytes(size)}
          {pageCount !== null && (
            <>
              <span aria-hidden="true" className="mx-1.5 text-slate-300">
                ·
              </span>
              共 {pageCount} 页
            </>
          )}
        </p>
      </div>

      {onRemove && (
        <Button
          size="sm"
          variant="secondary"
          onClick={onRemove}
          disabled={disabled}
          icon={<IconX className="h-3.5 w-3.5" />}
        >
          {removeLabel}
        </Button>
      )}
    </div>
  )
}
