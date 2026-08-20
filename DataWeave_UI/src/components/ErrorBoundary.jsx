import React from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

export class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="error-screen">
          <div className="card error-screen__card">
            <p className="card__label">
              <AlertTriangle size={14} /> Runtime Error
            </p>
            <h2 className="section__title">The UI hit a browser-side error</h2>
            <p className="card__text">
              Open the browser console for the exact stack trace. The error
              boundary is here so the app does not disappear into a blank screen.
            </p>
            {this.state.error ? (
              <pre className="error-screen__stack">
                {String(this.state.error?.stack || this.state.error)}
              </pre>
            ) : null}
            <button
              type="button"
              className="primary-button"
              onClick={() => window.location.reload()}
            >
              <RefreshCw size={16} />
              Reload
            </button>
          </div>
        </div>
      )
    }

    return this.props.children
  }
}
