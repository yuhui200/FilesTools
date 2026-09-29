import { formatBytes, formatLabel, fileExtension, truncateFilename } from '@/utils/format'

import { IconFile, IconTrash } from './Icons'

interface FileCardProps {
  file: File
  previewUrl?: string | null
  onRemove?: () => void
  disabled?: boolean
}

export function FileCard({ file, previewUrl, onRemove, disabled = false }: FileCardProps) {
  const extension = fileExtension(file.name)

  return (
    <div className="flex items-center gap-3.5 rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
      <div className="grid h-14 w-14 shrink-0 place-items-center overflow-hidden rounded-lg bg-slate-100">
        {previewUrl ? (
          <img src={previewUrl} alt="" className="h-full w-full object-cover" />
        ) : (
          <IconFile className="h-6 w-6 text-slate-400" />
        )}
      </div>

      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-slate-900" title={file.name}>
          {truncateFilename(file.name)}
        </p>
        <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-500">
          <span>{formatBytes(file.size)}</span>
          <span aria-hidden="true" className="text-slate-300">
            ·
          </span>
          <span className="rounded-md bg-slate-100 px-1.5 py-0.5 font-medium text-slate-600">
            {formatLabel(extension.replace('.', ''))}
          </span>
        </p>
      </div>

      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          disabled={disabled}
          aria-label={`移除 ${file.name}`}
          className="grid h-9 w-9 shrink-0 place-items-center rounded-lg text-slate-400 transition hover:bg-red-50 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <IconTrash className="h-4 w-4" />
        </button>
      )}
    </div>
  )
}
