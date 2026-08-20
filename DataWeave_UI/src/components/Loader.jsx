export default function Loader({ label = 'Loading demo data' }) {
  return (
    <div className="loader" role="status" aria-live="polite">
      <span className="loader__dot" />
      <span className="loader__dot" />
      <span className="loader__dot" />
      <span className="loader__label">{label}</span>
    </div>
  )
}
