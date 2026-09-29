import { Link } from 'react-router-dom'

import type { ToolDefinition } from '@/config/tools'

import { IconArrowRight } from './Icons'

interface ToolCardProps {
  tool: ToolDefinition
}

export function ToolCard({ tool }: ToolCardProps) {
  const Icon = tool.icon

  const inner = (
    <>
      <span
        className={[
          'grid h-11 w-11 place-items-center rounded-xl transition',
          tool.available
            ? 'bg-brand-50 text-brand-600 group-hover:bg-brand-600 group-hover:text-white'
            : 'bg-slate-100 text-slate-400',
        ].join(' ')}
      >
        <Icon className="h-6 w-6" />
      </span>

      <div className="mt-4 flex items-center gap-2">
        <h3 className="font-semibold text-slate-900">{tool.name}</h3>
        {!tool.available && (
          <span className="rounded-md bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-700">
            即将上线
          </span>
        )}
      </div>

      <p className="mt-1.5 text-sm leading-relaxed text-slate-500">{tool.description}</p>

      {tool.available && (
        <span className="mt-4 inline-flex items-center gap-1 text-sm font-medium text-brand-600">
          开始使用
          <IconArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
        </span>
      )}
    </>
  )

  const base =
    'group flex flex-col rounded-2xl border border-slate-200/80 bg-white p-5 shadow-card transition'

  if (!tool.available || !tool.path) {
    return (
      <div className={`${base} cursor-default opacity-75`} aria-disabled="true">
        {inner}
      </div>
    )
  }

  return (
    <Link to={tool.path} className={`${base} hover:-translate-y-0.5 hover:shadow-card-hover`}>
      {inner}
    </Link>
  )
}
