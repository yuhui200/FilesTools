import type { ConversionGroupState } from '@/hooks/useConversionGroups'

interface ConversionSummaryProps {
  groups: ConversionGroupState[]
  /** 认得出格式但服务器转不了的文件数 */
  unsupportedCount: number
  /** 还没开始的组数（这些文件的活还没交出去） */
  pendingGroups: number
}

/**
 * 页首的一行汇总：整页一共多少个文件、成了几个、败了几个。
 *
 * 数字全部来自各组的服务端快照。还没开始的组只计入总数 ——
 * 它们既不算成功也不算失败，混进去只会让「3 / 8」看起来像个进度。
 */
export function ConversionSummary({
  groups,
  unsupportedCount,
  pendingGroups,
}: ConversionSummaryProps) {
  const total = groups.reduce((sum, group) => sum + group.files.length, 0)
  if (total === 0 && unsupportedCount === 0) return null

  const started = groups.filter((group) => group.batch !== null)
  const completed = started.reduce((sum, group) => sum + (group.batch?.completed ?? 0), 0)
  const failed = started.reduce((sum, group) => sum + (group.batch?.failed ?? 0), 0)
  const cancelled = started.reduce((sum, group) => sum + (group.batch?.cancelled ?? 0), 0)
  const settled = completed + failed + cancelled

  return (
    <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm text-slate-600">
      <span className="tabular-nums">
        <span className="text-lg font-semibold text-slate-900">{completed}</span>
        <span className="mx-0.5">/</span>
        {total} 个文件已完成
      </span>
      {settled < total && (
        <span className="text-xs text-slate-500">
          {pendingGroups > 0 ? `${pendingGroups} 组还没开始` : '其余处理中'}
        </span>
      )}
      {failed > 0 && <span className="text-xs text-red-600">{failed} 个失败</span>}
      {cancelled > 0 && <span className="text-xs text-slate-500">{cancelled} 个已取消</span>}
      {unsupportedCount > 0 && (
        <span className="text-xs text-amber-700">{unsupportedCount} 个不支持</span>
      )}
    </div>
  )
}
