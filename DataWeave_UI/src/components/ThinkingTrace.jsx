import { Brain, ChevronDown } from 'lucide-react'
import { useEffect, useState } from 'react'

/**
 * A collapsible reasoning trace shown above an answer — the pipeline's steps
 * (understand → retrieve → rank → write), streamed live and persisted with the
 * message so it stays forever, like a Claude thinking block.
 *
 * Auto-expands while the answer is still streaming; collapses once done.
 */
export default function ThinkingTrace({ steps = [], streaming = false }) {
  const [open, setOpen] = useState(streaming)

  // Collapse automatically when streaming finishes; expand when it (re)starts.
  useEffect(() => {
    setOpen(streaming)
  }, [streaming])

  if (!steps.length) return null

  return (
    <div className={`thinking ${streaming ? 'thinking--live' : ''}`}>
      <button
        type="button"
        className="thinking__header"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Brain size={14} className="thinking__icon" />
        <span className="thinking__label">{streaming ? 'Thinking…' : 'Thought process'}</span>
        <ChevronDown
          size={14}
          className={`thinking__chevron ${open ? 'thinking__chevron--open' : ''}`}
        />
      </button>

      {open ? (
        <ol className="thinking__steps">
          {steps.map((step, i) => (
            <li key={i} className="thinking__step">
              <span className="thinking__step-label">{step.label}</span>
              {step.detail ? <span className="thinking__step-detail">{step.detail}</span> : null}
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  )
}
