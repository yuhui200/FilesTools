import { useState } from 'react'
import { Link, NavLink } from 'react-router-dom'

import { NAV_LINKS } from '@/config/tools'

import { IconMenu, IconX } from './Icons'

export function Navbar() {
  const [menuOpen, setMenuOpen] = useState(false)

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    [
      'rounded-lg px-3 py-2 text-sm font-medium transition',
      isActive ? 'text-brand-700' : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900',
    ].join(' ')

  return (
    <header className="sticky top-0 z-40 border-b border-slate-200/80 bg-white/85 backdrop-blur">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-4 px-4 sm:px-6">
        {/* 品牌锁排（Brand Logo）= App Icon + 字标，来自 branding/ 的唯一真源。
            这里**不放**自己写死的文字：字标换过一次就是一次漂移，
            scripts/verify_branding.py 会比对全仓库有没有第三份图标。
            h-8 配上 viewBox 的 132:32，出图正好 132×32。 */}
        <Link to="/" className="flex items-center" aria-label="FileTools 首页">
          <img src="/filetools-logo.svg" alt="FileTools" className="h-8 w-auto" />
        </Link>

        <nav className="hidden items-center gap-1 md:flex" aria-label="主导航">
          {NAV_LINKS.map((link) => (
            <NavLink key={link.to} to={link.to} end={link.to === '/'} className={linkClass}>
              {link.label}
            </NavLink>
          ))}
        </nav>

        <div className="flex items-center gap-2">
          <Link
            to="/image/compress"
            className="hidden h-10 items-center rounded-xl bg-brand-600 px-4 text-sm font-medium text-white shadow-sm transition hover:bg-brand-700 sm:inline-flex"
          >
            开始使用
          </Link>

          {/* 汉堡按钮取 h-11 w-11 = 44px。它只在窄屏出现，而窄屏基本都是
              手指 —— 原先的 h-10（40px）比 §五十六 的下界少 4px，是第十
              阶段 C 的移动端检查（scripts/verify_phase10.py I 段）挑出来的。
              它带 md:hidden，放大到 44 不影响桌面端的头部高度。 */}
          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            className="grid h-11 w-11 place-items-center rounded-xl text-slate-600 transition hover:bg-slate-100 md:hidden"
            aria-expanded={menuOpen}
            aria-label={menuOpen ? '关闭菜单' : '打开菜单'}
          >
            {menuOpen ? <IconX className="h-5 w-5" /> : <IconMenu className="h-5 w-5" />}
          </button>
        </div>
      </div>

      {menuOpen && (
        <nav
          className="border-t border-slate-200 bg-white px-4 pb-4 pt-2 md:hidden"
          aria-label="移动端导航"
        >
          {NAV_LINKS.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              end={link.to === '/'}
              onClick={() => setMenuOpen(false)}
              className={({ isActive }) =>
                [
                  'block rounded-lg px-3 py-2.5 text-sm font-medium transition',
                  isActive
                    ? 'bg-brand-50 text-brand-700'
                    : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900',
                ].join(' ')
              }
            >
              {link.label}
            </NavLink>
          ))}
          <Link
            to="/image/compress"
            onClick={() => setMenuOpen(false)}
            className="mt-2 flex h-11 items-center justify-center rounded-xl bg-brand-600 text-sm font-medium text-white"
          >
            开始使用
          </Link>
        </nav>
      )}
    </header>
  )
}
