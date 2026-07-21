/** Kořenová ErrorBoundary (#159): výjimka v render/assemble řetězci nesmí
skončit tichou bílou stránkou (stejný projev jako #146) — místo ní hláška
s detailem chyby a tlačítkem pro obnovení. */
import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

interface ErrorBoundaryState {
  error: Error | null
}

export class ErrorBoundary extends Component<{ children: ReactNode }, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Neošetřená chyba aplikace:', error, info.componentStack)
  }

  render(): ReactNode {
    if (this.state.error === null) return this.props.children
    return (
      <div role="alert" style={{ padding: '2rem', fontFamily: 'sans-serif' }}>
        <h1 style={{ fontSize: '1.2rem' }}>GEXLens narazil na chybu</h1>
        <p>Zkuste stránku obnovit; pokud chyba přetrvává, detail níže patří do issue.</p>
        <pre
          style={{ whiteSpace: 'pre-wrap', background: 'rgba(128,128,128,0.15)', padding: '1rem' }}
        >
          {this.state.error.stack ?? this.state.error.message}
        </pre>
        <button type="button" onClick={() => window.location.reload()}>
          Obnovit stránku
        </button>
      </div>
    )
  }
}
