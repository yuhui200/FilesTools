import { IconShield } from './Icons'

export function Footer() {
  return (
    <footer className="mt-20 border-t border-slate-200 bg-white">
      <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
        <div className="flex flex-col gap-6 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="flex items-center gap-2.5">
              <span className="grid h-7 w-7 place-items-center rounded-lg bg-brand-600 text-xs font-bold text-white">
                F
              </span>
              <span className="font-semibold text-slate-900">FileTools</span>
            </div>
            <p className="mt-2 max-w-md text-sm text-slate-500">
              简单、快速的在线文件工具。转换、压缩、调整图片和处理 PDF。
            </p>
          </div>

          <div className="flex items-start gap-2.5 rounded-xl bg-slate-50 px-4 py-3 text-sm text-slate-600 sm:max-w-xs">
            <IconShield className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
            <p>
              文件仅用于本次处理，处理完成后自动从服务器删除，不做任何留存。
            </p>
          </div>
        </div>

        <p className="mt-8 text-xs text-slate-400">
          © {new Date().getFullYear()} FileTools · 本工具在本地部署运行
        </p>
      </div>
    </footer>
  )
}
