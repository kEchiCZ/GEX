import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import * as perfMeter from './perf/meter'

declare global {
  interface Window {
    /** Měřič výkonu grafu (#1409) z konzole a Playwrightu (E-2a.13):
    `gexlensPerf.setPerfEnabled(true)`, `gexlensPerf.perfExport()`. UI přidá E-2a.1b. */
    gexlensPerf?: typeof perfMeter
  }
}
window.gexlensPerf = perfMeter

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
