import { Link } from 'react-router-dom'

export function NotFound() {
  return (
    <div className="mx-auto flex max-w-xl flex-col items-center px-4 py-24 text-center sm:px-6">
      <p className="text-5xl font-bold tracking-tight text-brand-600">404</p>
      <h1 className="mt-5 text-2xl font-semibold tracking-tight text-slate-900">页面不存在</h1>
      <p className="mt-3 text-slate-600">
        你访问的地址可能已经变更或被移除。回到首页看看有哪些可用工具。
      </p>
      <Link
        to="/"
        className="mt-8 inline-flex h-11 items-center rounded-xl bg-brand-600 px-5 text-sm font-medium text-white shadow-sm transition hover:bg-brand-700"
      >
        返回首页
      </Link>
    </div>
  )
}
