import { IconAlert } from '@/components/Icons'
import type { UnsupportedFile } from '@/utils/conversion'
import { formatBytes } from '@/utils/format'

interface ConversionUnsupportedProps {
  files: UnsupportedFile[]
  /** 服务器当前缺哪些能力（capabilities 里的 notes） */
  notes: string[]
  onClear: () => void
}

/**
 * 「这些文件转不了」。
 *
 * 单独成块而不是混在可转的组里（§二十一 的等价形态）：用户一眼能看出
 * 哪些文件被落下了、每个是为什么。原因有两条来源 ——
 * 格式本身不在矩阵里，或者服务器没装处理这类文件需要的组件，
 * 后者用 capabilities 的 notes 说清楚，否则用户只会反复重试同一个文件。
 */
export function ConversionUnsupported({ files, notes, onClear }: ConversionUnsupportedProps) {
  if (files.length === 0) return null

  return (
    <section className="card border-amber-200 bg-amber-50/60 p-5 sm:p-6">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-2.5">
          <IconAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <div>
            <h2 className="text-sm font-medium text-amber-900">
              {files.length} 个文件不支持转换
            </h2>
            <p className="mt-0.5 text-xs text-amber-800">
              这些文件不会提交，可以移除后继续处理其余文件。
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={onClear}
          className="shrink-0 rounded-lg px-2.5 py-1.5 text-xs font-medium text-amber-900 transition hover:bg-amber-100"
        >
          清空
        </button>
      </div>

      <ul className="mt-3 space-y-1.5">
        {files.map((item, index) => (
          <li
            key={`${item.file.name}-${item.file.size}-${index}`}
            className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 text-sm"
          >
            <span className="min-w-0 break-all text-amber-900" title={item.file.name}>
              {item.file.name}
            </span>
            <span className="shrink-0 text-xs text-amber-700">
              {item.reason}
              {item.file.size > 0 && ` · ${formatBytes(item.file.size)}`}
            </span>
          </li>
        ))}
      </ul>

      {notes.length > 0 && (
        <ul className="mt-3 space-y-1 border-t border-amber-200 pt-3">
          {notes.map((note) => (
            <li key={note} className="text-xs text-amber-800">
              {note}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
