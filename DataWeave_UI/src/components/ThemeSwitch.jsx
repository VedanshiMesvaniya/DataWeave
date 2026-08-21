import { Moon, Sun } from 'lucide-react'
import { useAppStore } from '../store/store.js'

export default function ThemeSwitch() {
  const settings = useAppStore((state) => state.settings)
  const updateSettings = useAppStore((state) => state.updateSettings)
  const current = settings?.theme || 'academic-dark'
  const isLight = current === 'academic-light'

  const toggle = () => {
    updateSettings({ theme: isLight ? 'academic-dark' : 'academic-light' })
  }

  return (
    <button
      type="button"
      className="theme-switch"
      data-mode={isLight ? 'light' : 'dark'}
      onClick={toggle}
      role="switch"
      aria-checked={isLight}
      aria-label={isLight ? 'Switch to Midnight theme' : 'Switch to Day Light theme'}
      title={isLight ? 'Day Light' : 'Midnight'}
    >
      <span className="theme-switch__icon theme-switch__icon--moon">
        <Moon size={12} strokeWidth={2.4} />
      </span>
      <span className="theme-switch__icon theme-switch__icon--sun">
        <Sun size={12} strokeWidth={2.4} />
      </span>
      <span className="theme-switch__thumb" />
    </button>
  )
}
