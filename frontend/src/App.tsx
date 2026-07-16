/** Kořenový layout aplikace (SPEC 7.1). Hlavní plocha je placeholder do #23 (heatmapa). */
import './App.css'
import { TimeframeRow, TogglesRow } from './components/ControlRows'
import { InstrumentHeader } from './components/InstrumentHeader'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { AppStateProvider } from './state/AppState'
import type { LiveSocket } from './api/ws'

export default function App({ socket }: { socket?: LiveSocket }) {
  return (
    <AppStateProvider socket={socket}>
      <div className="app">
        <Sidebar watchlist={[{ symbol: 'ES', changePct: null }]} />
        <div className="main-column">
          <InstrumentHeader />
          <TimeframeRow />
          <TogglesRow />
          <main className="chart-area" aria-label="Heatmapa">
            <p className="muted placeholder">Heatmapa (issue #23)</p>
          </main>
          <StatusBar />
        </div>
      </div>
    </AppStateProvider>
  )
}
