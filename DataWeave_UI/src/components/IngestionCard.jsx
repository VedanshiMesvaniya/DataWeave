import { Check, ChevronDown, FileText, Loader2, Minus, X } from 'lucide-react'
import { useState } from 'react'

function StepIcon({ status }) {
  if (status === 'done') return <Check size={13} className="ingest-step__icon ingest-step__icon--done" />
  if (status === 'error') return <X size={13} className="ingest-step__icon ingest-step__icon--error" />
  if (status === 'skipped') return <Minus size={13} className="ingest-step__icon ingest-step__icon--skipped" />
  if (status === 'running')
    return <Loader2 size={13} className="ingest-step__icon ingest-step__icon--running" />
  return <span className="ingest-step__dot" aria-hidden="true" />
}

/**
 * Renders the step-by-step ingestion trace for an uploaded file. Streams live
 * while the pipeline runs and — because it's persisted as a normal message —
 * stays in the chat forever, like a Claude reasoning block.
 */
export default function IngestionCard({ message }) {
  const running = message.status === 'running'
  // Collapse a finished trace by default; keep it open while running.
  const [open, setOpen] = useState(running)
  const steps = message.steps || []
  const doneCount = steps.filter((s) => s.status === 'done').length

  const headline =
    message.status === 'running'
      ? 'Ingesting document…'
      : message.status === 'skipped'
        ? 'Already ingested'
        : message.status === 'error'
          ? 'Ingestion failed'
          : 'Document ingested'

  return (
    <div className={`ingest-card ingest-card--${message.status}`}>
      <button
        type="button"
        className="ingest-card__header"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <FileText size={16} className="ingest-card__file-icon" />
        <div className="ingest-card__titles">
          <span className="ingest-card__title">{headline}</span>
          <span className="ingest-card__file">{message.fileName}</span>
        </div>
        <span className="ingest-card__progress">
          {running ? `${doneCount}/${steps.length}` : null}
        </span>
        <ChevronDown
          size={16}
          className={`ingest-card__chevron ${open ? 'ingest-card__chevron--open' : ''}`}
        />
      </button>

      {open ? (
        <ol className="ingest-card__steps">
          {steps.map((step) => (
            <li key={step.stage} className={`ingest-step ingest-step--${step.status}`}>
              <StepIcon status={step.status} />
              <span className="ingest-step__label">{step.label}</span>
              {step.detail ? <span className="ingest-step__detail">{step.detail}</span> : null}
            </li>
          ))}
        </ol>
      ) : null}

      {message.content && !running ? (
        <p className="ingest-card__summary">{message.content}</p>
      ) : null}
    </div>
  )
}
