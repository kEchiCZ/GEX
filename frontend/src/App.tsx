/** Kořenový layout aplikace (SPEC 7.1) s canvas heatmapou (SPEC 7.2). */
import { useMemo, useState } from 'react'
import './App.css'
import { TimeframeRow, TogglesRow } from './components/ControlRows'
import { Heatmap } from './components/Heatmap'
import { InstrumentHeader } from './components/InstrumentHeader'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { demoGrid } from './heatmap/demo'
import { AppStateProvider } from './state/AppState'
import type { ContoursMode } from './heatmap/contours'
import type { HeatmapStyle } from './heatmap/render'
import type { LiveSocket } from './api/ws'

export default function App({ socket }: { socket?: LiveSocket }) {
  const [style, setStyle] = useState<HeatmapStyle>('gradient')
  const [contours, setContours] = useState<ContoursMode>('off')
  // Demo data do zapojení replay/live feedu (issue #27)
  const grid = useMemo(() => demoGrid(), [])

  return (
    <AppStateProvider socket={socket}>
      <div className="app">
        <Sidebar watchlist={[{ symbol: 'ES', changePct: null }]} />
        <div className="main-column">
          <InstrumentHeader />
          <TimeframeRow />
          <TogglesRow />
          <div className="row heatmap-controls" role="toolbar" aria-label="Heatmapa nastavení">
            <label className="toggle">
              Styl
              <select
                value={style}
                onChange={(event) => setStyle(event.target.value as HeatmapStyle)}
              >
                <option value="gradient">Gradient</option>
                <option value="blobs">Blobs</option>
              </select>
            </label>
            <label className="toggle">
              Contours
              <select
                value={contours}
                onChange={(event) => setContours(event.target.value as ContoursMode)}
              >
                <option value="off">Off</option>
                <option value="major">Major</option>
                <option value="all">All</option>
              </select>
            </label>
          </div>
          <main className="chart-area" aria-label="Heatmapa">
            <Heatmap grid={grid} style={style} contours={contours} />
          </main>
          <StatusBar />
        </div>
      </div>
    </AppStateProvider>
  )
}
