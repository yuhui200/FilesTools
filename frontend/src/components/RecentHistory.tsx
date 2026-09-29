import { IconAlert, IconCheckCircle, IconClock, IconTrash } from '@/components/Icons'
import { useHistory } from '@/hooks/useHistory'
import { formatBytes, formatDateTime, truncateFilename } from '@/utils/format'

/**
 * 「最近处理」列表（第四阶段 §12）。
 *
 * 只展示元信息：文件名称、操作类型、处理时间、处理状态、文件大小。
 * 记录存在浏览器的 sessionStorage 里（见 :mod:`@/utils/history`），
 * 关掉标签页就没了 —— 与「不长期保存用户文件」的承诺一致。
 *
 * 没有任何记录时整块不渲染：首页不需要一个空表格占位置。
 */
export function RecentHistory() {
  const { entries, clear } = useHistory()

  if (entries.length === 0) return null

  return (
    <section className="card overflow-hidden">
      <div className="flex items-center justify-between gap-3 border-b border-slate-100 px-5 py-4">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
          <IconClock className="h-4 w-4 text-slate-400" />
          最近处理
        </h3>
        {/* 窄屏 h-11 = 44px（§五十六 的手指下界），桌面仍是 h-8 ——
            这个按钮在窄屏是手指点，在桌面是鼠标点，两边本来就该不一样。
            原先窄屏是 h-10（40px），比下界少 4px，第十阶段 C 的移动端检查
            （scripts/verify_phase10.py I 段）把它挑了出来。 */}
        <button
          type="button"
          onClick={clear}
          className="inline-flex h-11 items-center gap-1.5 rounded-lg px-3 text-xs font-medium text-slate-500 transition hover:bg-slate-50 hover:text-red-600 sm:h-8"
        >
          <IconTrash className="h-3.5 w-3.5" />
          清空记录
        </button>
      </div>

      {/* 桌面端是五列表格；窄屏下文件名独占一行，其余信息跟在后面换行显示（§8：不能横向滚动） */}
      <div className="hidden border-b border-slate-100 bg-slate-50/70 px-5 py-2 text-xs font-medium text-slate-500 sm:grid sm:grid-cols-[minmax(0,1fr)_7rem_9.5rem_5.5rem_6rem] sm:gap-3">
        <span>文件名称</span>
        <span>操作类型</span>
        <span>处理时间</span>
        <span>处理状态</span>
        <span className="sm:text-right">文件大小</span>
      </div>

      <ul className="divide-y divide-slate-100">
        {entries.map((entry) => (
          <li
            key={entry.id}
            className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3 sm:grid sm:grid-cols-[minmax(0,1fr)_7rem_9.5rem_5.5rem_6rem] sm:gap-3"
          >
            <span
              className="w-full truncate text-sm font-medium text-slate-800 sm:w-auto"
              title={entry.filename}
            >
              {truncateFilename(entry.filename, 48)}
            </span>
            <span className="text-xs text-slate-500 sm:text-sm">{entry.tool}</span>
            <span className="text-xs tabular-nums text-slate-500 sm:text-sm">
              {formatDateTime(entry.at)}
            </span>
            <span>
              {entry.ok ? (
                <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">
                  <IconCheckCircle className="h-3.5 w-3.5" />
                  已完成
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 rounded-full bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">
                  <IconAlert className="h-3.5 w-3.5" />
                  失败
                </span>
              )}
            </span>
            <span className="text-xs tabular-nums text-slate-500 sm:text-right sm:text-sm">
              {formatBytes(entry.size)}
            </span>
          </li>
        ))}
      </ul>

      <p className="border-t border-slate-100 px-5 py-3 text-xs text-slate-500">
        这里只记录文件名、操作类型、时间、状态和大小，不保存文件内容，也不会跨设备同步。
        关闭浏览器标签页后记录即消失。
      </p>
    </section>
  )
}
