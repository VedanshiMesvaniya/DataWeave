export default function Card({ label, title, children }) {
  return (
    <article className="card">
      {label ? <p className="card__label">{label}</p> : null}
      {title ? <h3 className="card__title">{title}</h3> : null}
      {children ? <p className="card__text">{children}</p> : null}
    </article>
  )
}
