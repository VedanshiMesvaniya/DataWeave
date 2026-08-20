import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { Toaster } from 'sonner'
import { Layout } from './components/Layout.jsx'
import About from './pages/About.jsx'
import Documents from './pages/Documents.jsx'
import Home from './pages/Home.jsx'
import Settings from './pages/Settings.jsx'
import { useAppStore } from './store/store.js'
import { useResolvedTheme } from './utils/theme.js'
import './styles/markdown.css'
import './styles/globals.css'

function App() {
  const theme = useAppStore((state) => state.settings?.theme)
  const appliedTheme = useResolvedTheme(theme)
  const themeMode = appliedTheme.mode
  const toasterTheme = themeMode === 'light' ? 'light' : 'dark'

  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Home />} />
          <Route path="chat" element={<Home />} />
          <Route path="documents" element={<Documents />} />
          <Route path="settings" element={<Settings />} />
          <Route path="about" element={<About />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <Toaster
        theme={toasterTheme}
        richColors
        position="top-right"
        closeButton
        toastOptions={{
          style: {
            background:
              themeMode === 'light'
                ? 'rgba(255, 253, 251, 0.95)'
                : 'rgba(18, 18, 18, 0.95)',
            color: 'var(--text-primary)',
            border:
              themeMode === 'light'
                ? '1px solid rgba(35, 35, 35, 0.12)'
                : '1px solid rgba(255, 255, 255, 0.08)',
          },
        }}
      />
    </BrowserRouter>
  )
}

export default App
