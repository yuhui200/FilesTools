import { useEffect } from 'react'
import { Outlet, useLocation } from 'react-router-dom'

import { Footer } from './Footer'
import { Navbar } from './Navbar'

/** 路由切换时回到页面顶部 */
function ScrollToTop() {
  const { pathname } = useLocation()

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: 'auto' })
  }, [pathname])

  return null
}

export function Layout() {
  return (
    <div className="flex min-h-screen flex-col">
      <ScrollToTop />
      <Navbar />
      <main className="flex-1">
        <Outlet />
      </main>
      <Footer />
    </div>
  )
}
