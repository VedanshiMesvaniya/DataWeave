import { useEffect, useRef } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import TopBar from './TopBar.jsx'
import LeftPanel from './LeftPanel.jsx'
import RightPanel from './RightPanel.jsx'
import { useAppStore } from '../store/store.js'
import { useResolvedTheme } from '../utils/theme.js'

export function Layout() {
  const initApp = useAppStore((state) => state.initApp)
  const theme = useAppStore((state) => state.settings?.theme)
  const appliedTheme = useResolvedTheme(theme)
  const initialized = useRef(false)
  const location = useLocation()
  const isChatRoute = location.pathname === '/' || location.pathname === '/chat'

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
    <div className="dashboard-shell">
      <TopBar />
      <div className={`dashboard-body ${isChatRoute ? 'dashboard-body--chat' : 'dashboard-body--page'}`}>
        {isChatRoute ? <LeftPanel /> : null}
        <div className="glass-card center-card">
          <Outlet />
        </div>
        {isChatRoute ? <RightPanel /> : null}
      </div>
    </div>
  )
}
