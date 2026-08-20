import { useEffect, useRef, useState } from 'react'
import { Check, ChevronDown, Gauge, RotateCw } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import { useAppStore } from '../store/store.js'

// Clamp a used/limit pair into a 0–100% width and a severity band so the meter
// fill shifts from calm → warning → critical as a free-tier window fills up.
// Kept in sync with the same helper the Settings page used before this moved.
function meter(used, limit) {
  const capacity = Number(limit) || 0
  const consumed = Number(used) || 0
  const pct = capacity > 0 ? Math.min(100, Math.round((consumed / capacity) * 100)) : 0
  const level = pct >= 90 ? 'crit' : pct >= 70 ? 'warn' : 'ok'
  return { pct, level }
}

// Collapse every provider's per-minute/per-day meters into a single worst-case
// severity for the status dot, plus whether anything is currently backing off.
function summarize(usage) {
  let level = 'ok'
  let cooling = false
  for (const p of usage || []) {
    if (Number(p.backoffSeconds) > 0) cooling = true
    for (const m of [meter(p.rpmUsed, p.rpmLimit), meter(p.rpdUsed, p.rpdLimit)]) {
      if (m.level === 'crit') level = 'crit'
      else if (m.level === 'warn' && level !== 'crit') level = 'warn'
    }
  }
  return { level, cooling }
}

const fallbackProviders = [
  { id: 'auto', label: 'Auto' },
  { id: 'openrouter', label: 'OpenRouter' },
]

// Close a popover when the user clicks anywhere outside it or presses Escape.
function useDismiss(ref, onDismiss, active) {
  useEffect(() => {
    if (!active) return undefined
    const onPointer = (event) => {
      if (ref.current && !ref.current.contains(event.target)) onDismiss()
    }
    const onKey = (event) => {
      if (event.key === 'Escape') onDismiss()
    }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [ref, onDismiss, active])
}

export default function ProviderStatus() {
  const settings = useAppStore((state) => state.settings)
  const providers = useAppStore((state) => state.providers)
  const providerUsage = useAppStore((state) => state.providerUsage)
  const refreshProviderUsage = useAppStore((state) => state.refreshProviderUsage)
  const updateSettings = useAppStore((state) => state.updateSettings)

  const [pickerOpen, setPickerOpen] = useState(false)
  const [usageOpen, setUsageOpen] = useState(false)
  const pickerRef = useRef(null)
  const usageRef = useRef(null)

  useDismiss(pickerRef, () => setPickerOpen(false), pickerOpen)
  useDismiss(usageRef, () => setUsageOpen(false), usageOpen)

  // Pull fresh quota numbers whenever the usage popover is opened, so the dot
  // and meters reflect the current window rather than whatever loaded at boot.
  useEffect(() => {
    if (usageOpen) refreshProviderUsage()
  }, [usageOpen, refreshProviderUsage])

  const options = providers?.length ? providers : fallbackProviders
  const activeId = settings?.provider || 'auto'
  const activeLabel =
    options.find((o) => o.id === activeId)?.label ||
    (activeId === 'auto' ? 'Auto' : activeId)

  // Auto pins nothing, so its dot/meters reflect every provider. A specific
  // pin narrows the view to just that provider — the only one a request will
  // actually hit while the pin holds.
  const visibleUsage =
    activeId === 'auto'
      ? providerUsage || []
      : (providerUsage || []).filter((p) => p.id === activeId)

  const { level, cooling } = summarize(visibleUsage)

  const selectProvider = (id) => {
    updateSettings({ provider: id })
    setPickerOpen(false)
  }

  return (
    <div className="provider-status">
      {/* Current provider — click to switch, mirrors Claude's model selector. */}
      <div className="provider-status__picker" ref={pickerRef}>
        <button
          type="button"
          className="provider-status__chip"
          onClick={() => {
            setPickerOpen((v) => !v)
            setUsageOpen(false)
          }}
          aria-haspopup="menu"
          aria-expanded={pickerOpen}
          title="Change provider"
        >
          <span className="provider-status__name">{activeLabel}</span>
          <ChevronDown size={13} className="provider-status__caret" />
        </button>

        <AnimatePresence>
          {pickerOpen ? (
            <motion.div
              className="provider-pop provider-pop--picker"
              role="menu"
              initial={{ opacity: 0, y: 6, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: 0.98 }}
              transition={{ duration: 0.14 }}
            >
              {options.map((option) => {
                const active = option.id === activeId
                return (
                  <button
                    key={option.id}
                    type="button"
                    role="menuitemradio"
                    aria-checked={active}
                    className={`provider-pop__item ${active ? 'provider-pop__item--active' : ''}`}
                    onClick={() => selectProvider(option.id)}
                  >
                    <span>{option.label}</span>
                    {active ? <Check size={13} /> : null}
                  </button>
                )
              })}
            </motion.div>
          ) : null}
        </AnimatePresence>
      </div>

      {/* Quota dot — click to reveal the live per-provider usage meters. */}
      <div className="provider-status__usage" ref={usageRef}>
        <button
          type="button"
          className={`provider-status__dot provider-status__dot--${level} ${
            cooling ? 'provider-status__dot--cooling' : ''
          }`}
          onClick={() => {
            setUsageOpen((v) => !v)
            setPickerOpen(false)
          }}
          aria-haspopup="dialog"
          aria-expanded={usageOpen}
          aria-label="Show provider usage limits"
          title="Provider usage & limits"
        >
          <span className="provider-status__dot-core" />
        </button>

        <AnimatePresence>
          {usageOpen ? (
            <motion.div
              className="provider-pop provider-pop--usage"
              role="dialog"
              aria-label="Provider usage"
              initial={{ opacity: 0, y: 6, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: 0.98 }}
              transition={{ duration: 0.14 }}
            >
              <div className="provider-pop__head">
                <span className="provider-pop__title">
                  <Gauge size={14} />
                  Provider Usage
                </span>
                <button
                  type="button"
                  className="usage-refresh"
                  onClick={() => refreshProviderUsage()}
                  aria-label="Refresh usage"
                  title="Refresh usage"
                >
                  <RotateCw size={12} />
                </button>
              </div>

              {visibleUsage.length ? (
                <div className="provider-pop__list">
                  {visibleUsage.map((p) => {
                    const rpm = meter(p.rpmUsed, p.rpmLimit)
                    const rpd = meter(p.rpdUsed, p.rpdLimit)
                    const isCooling = Number(p.backoffSeconds) > 0
                    return (
                      <div key={p.id} className="usage-card usage-card--compact">
                        <div className="usage-card__head">
                          <span className="usage-card__name">{p.label}</span>
                          {isCooling ? (
                            <span className="usage-card__cooldown">
                              cooling {Math.ceil(p.backoffSeconds)}s
                            </span>
                          ) : null}
                        </div>

                        <div className="usage-meter">
                          <div className="usage-meter__label">
                            <span>Per minute</span>
                            <span className="usage-meter__count">
                              {p.rpmUsed} / {p.rpmLimit}
                            </span>
                          </div>
                          <div className="usage-track">
                            <div
                              className={`usage-fill usage-fill--${rpm.level}`}
                              style={{ width: `${rpm.pct}%` }}
                            />
                          </div>
                        </div>

                        <div className="usage-meter">
                          <div className="usage-meter__label">
                            <span>Per day</span>
                            <span className="usage-meter__count">
                              {p.rpdUsed} / {p.rpdLimit}
                            </span>
                          </div>
                          <div className="usage-track">
                            <div
                              className={`usage-fill usage-fill--${rpd.level}`}
                              style={{ width: `${rpd.pct}%` }}
                            />
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              ) : (
                <p className="usage-empty">
                  {activeId === 'auto'
                    ? "Usage isn't available yet."
                    : `No usage data for ${activeLabel} yet.`}
                </p>
              )}
            </motion.div>
          ) : null}
        </AnimatePresence>
      </div>
    </div>
  )
}
