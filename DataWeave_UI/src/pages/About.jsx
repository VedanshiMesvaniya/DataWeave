import Card from '../components/Card.jsx'

export default function About() {
  return (
    <section className="page section">
      <div className="section__header">
        <div>
          <h2 className="section__title">About DataWeave</h2>
          <p className="section__subtitle">
            A dark, local-first chat interface using static demo data for now.
          </p>
        </div>
      </div>

      <div className="grid">
        <Card label="Goal" title="Private intelligence">
          The interface keeps the same visual direction as your reference and is
          designed to work with local or remote APIs later.
        </Card>
        <Card label="Stack" title="Built with your packages">
          React Router, Zustand, Axios, Framer Motion, markdown rendering, and
          toast notifications are already wired in.
        </Card>
      </div>

      <div className="about-copy">
        <p>
          The first load uses static demo data so the UI works immediately. When
          you connect your backend later, the live API responses can replace the
          placeholder data without changing the component tree.
        </p>
        <p>
          Use the chat page for the main conversation flow, the documents page
          for uploads and file metadata, and the settings page for API
          configuration later.
        </p>
      </div>
    </section>
  )
}
