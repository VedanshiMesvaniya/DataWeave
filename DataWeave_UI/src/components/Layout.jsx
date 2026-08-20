import { useEffect, useRef } from 'react'
import { Outlet } from 'react-router-dom'
import Header from './Header.jsx'
import Sidebar from './Sidebar.jsx'
import { useAppStore } from '../store/store.js'
import { useResolvedTheme } from '../utils/theme.js'

export function Layout() {
  const initApp = useAppStore((state) => state.initApp)
  const theme = useAppStore((state) => state.settings?.theme)
  const sidebarCollapsed = useAppStore((state) => state.sidebarCollapsed)
  const appliedTheme = useResolvedTheme(theme)
  const initialized = useRef(false)

  useEffect(() => {
    if (initialized.current) return
    initialized.current = true
    initApp()
  }, [initApp])

  useEffect(() => {
    if (typeof document === 'undefined') return
    document.documentElement.dataset.theme = appliedTheme.value
    document.documentElement.dataset.themeMode = appliedTheme.mode
    document.documentElement.style.colorScheme = appliedTheme.mode
  }, [appliedTheme])

  useEffect(() => {
    if (typeof window === 'undefined') return undefined

    const root = document.documentElement
    let timerId = null

    const markScrolling = () => {
      root.dataset.scrolling = 'true'
      window.clearTimeout(timerId)
      timerId = window.setTimeout(() => {
        delete root.dataset.scrolling
      }, 700)
    }

    window.addEventListener('scroll', markScrolling, true)
    return () => {
      window.removeEventListener('scroll', markScrolling, true)
      window.clearTimeout(timerId)
      delete root.dataset.scrolling
    }
  }, [])

  return (
    <div className="app-shell" data-sidebar-collapsed={sidebarCollapsed ? 'true' : 'false'}>
      <Sidebar />
      <main className="content">
        <Header />
        <div className="main-scroll">
          <div className="surface">
            <Outlet />
          </div>
        </div>
      </main>
    </div>
  )
}
