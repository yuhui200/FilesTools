import type { FilePreview } from '@/hooks/useFilePreviews'
import { formatBytes } from '@/utils/format'

import { FileCard } from './FileCard'

interface FileListProps {
  files: File[]
  previews: FilePreview[]
  onRemove: (index: number) => void
  disabled?: boolean
  maxFiles: number
}

/**
 * 已选文件列表。
 *
 * 每个文件展示缩略图、名称、大小、类型和删除按钮（第一阶段 FileCard 的直接复用）。
 * 超过 6 个时列表内部滚动，避免页面被撑得过长。
 */
export function FileList({ files, previews, onRemove, disabled = false, maxFiles }: FileListProps) {
  const total = files.reduce((sum, file) => sum + file.size, 0)

  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-medium text-slate-900">
          已选 {files.length} 个文件
        </h3>
        <span className="text-xs text-slate-500">
          合计 {formatBytes(total)} · 最多 {maxFiles} 个
        </span>
      </div>

      <ul
        className={[
          'mt-3 space-y-2',
          files.length > 6 ? 'max-h-[26rem] overflow-y-auto pr-1' : '',
        ].join(' ')}
      >
        {files.map((file, index) => (
          <li key={`${file.name}-${file.size}-${index}`}>
            <FileCard
              file={file}
              previewUrl={previews[index]?.url ?? null}
              onRemove={() => onRemove(index)}
              disabled={disabled}
            />
          </li>
        ))}
      </ul>
    </div>
  )
}
