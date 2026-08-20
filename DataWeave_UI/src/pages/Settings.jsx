import { Check, Sparkles } from 'lucide-react'
import { motion } from 'framer-motion'
import { useAppStore } from '../store/store.js'

const themeOptions = [
  {
    id: 'academic-dark',
    name: 'Midnight',
    label: 'Focus Dark',
    mode: 'academic-dark',
    previewClass: 'theme-preview theme-preview--academic-dark',
    previewType: 'bars',
  },
  {
    id: 'academic-light',
    name: 'Day Light',
    label: 'Clear Light',
    mode: 'academic-light',
    previewClass: 'theme-preview theme-preview--academic-light',
    previewType: 'bars',
  },
]

export default function Settings() {
  const settings = useAppStore((state) => state.settings)
  const updateSettings = useAppStore((state) => state.updateSettings)

  const current = settings || {
    endpoint: '/api',
    model: 'Mistral 7B Instruct',
    streamResponses: true,
    autoSync: true,
    theme: 'academic-dark',
    provider: 'openrouter',
  }

  const setSetting = (patch) => {
    updateSettings(patch)
  }

  return (
    <section className="page settings-page">
      <div className="section__header settings-page__header">
        <div>
          <h2 className="section__title">Settings</h2>
          <p className="section__subtitle">
            Model behavior and appearance. Changes are saved automatically.
          </p>
        </div>
      </div>

      <motion.section
        className="settings-panel"
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <div className="settings-panel__heading">
          <div className="settings-panel__title-wrap">
            <Sparkles size={16} />
            <h3 className="settings-panel__title">Appearance</h3>
          </div>
          <span className="settings-panel__rule" />
        </div>

        <div className="setting-row">
          <label>Theme</label>
          <p className="setting-help">Customize UI colors</p>
          <div className="theme-grid">
            {themeOptions.map((theme) => {
              const active = current.theme === theme.mode
              return (
                <button
                  key={theme.id}
                  type="button"
                  className={`theme-card ${active ? 'theme-card--active' : ''}`}
                  onClick={() => setSetting({ theme: theme.mode })}
                >
                  <div className={theme.previewClass}>
                    {theme.previewType === 'command' ? (
                      <>
                        <div className="theme-preview__command-bar" />
                        <div className="theme-preview__command-shell">
                          <div className="theme-preview__command-rail">
                            <span className="theme-preview__command-badge" />
                            <span className="theme-preview__command-line" />
                            <span className="theme-preview__command-line theme-preview__command-line--short" />
                          </div>
                          <div className="theme-preview__command-panel">
                            <span className="theme-preview__command-title" />
                            <span className="theme-preview__command-copy" />
                            <div className="theme-preview__command-card" />
                          </div>
                        </div>
                      </>
                    ) : theme.previewType === 'aurora' ? (
                      <>
                        <div className="theme-preview__aurora-glow" />
                        <div className="theme-preview__bar theme-preview__bar--primary" />
                        <div className="theme-preview__bar" />
                        <div className="theme-preview__bar theme-preview__bar--secondary" />
                      </>
                    ) : (
                      <>
                        <div className="theme-preview__bar theme-preview__bar--primary" />
                        <div className="theme-preview__bar" />
                        <div className="theme-preview__bar theme-preview__bar--secondary" />
                      </>
                    )}
                    {active ? (
                      <span className="theme-card__check">
                        <Check size={12} />
                      </span>
                    ) : null}
                  </div>
                  <div className="theme-card__meta">
                    <strong className="theme-card__name">{theme.name}</strong>
                    <span className="theme-card__label">{theme.label}</span>
                  </div>
                </button>
              )
            })}
          </div>
        </div>
      </motion.section>
    </section>
  )
}
