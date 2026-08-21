import ThemeSwitch from './ThemeSwitch.jsx'

export default function TopBar() {
  return (
    <header className="top-bar">
      <div className="top-bar__brand">
        <span className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect width="64" height="64" rx="18" className="brand-mark__bg" />
            <path
              d="M22,12 C22,22 44,22 44,32 C44,42 22,42 22,52"
              className="brand-mark__stroke-a"
              strokeWidth="6"
              strokeLinecap="round"
              fill="none"
            />
            <path
              d="M42,12 C42,22 20,22 20,32 C20,42 42,42 42,52"
              className="brand-mark__stroke-b"
              strokeWidth="6"
              strokeLinecap="round"
              fill="none"
            />
          </svg>
        </span>
        <span className="top-bar__title">DataWeave</span>
      </div>
      <ThemeSwitch />
    </header>
  )
}
