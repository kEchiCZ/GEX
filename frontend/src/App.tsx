/** Kořenový layout aplikace (SPEC 7.1) s canvas heatmapou a overlayi (SPEC 7.2). */
import { useMemo, useState } from 'react'
import './App.css'
import { TimeframeRow, TogglesRow } from './components/ControlRows'
import { Heatmap } from './components/Heatmap'
import { InstrumentHeader } from './components/InstrumentHeader'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { StrikeProfile } from './components/StrikeProfile'
import { demoGrid, demoOverlays, demoProfile } from './heatmap/demo'
import { visibleOverlays } from './heatmap/overlays'
import { AppStateProvider, useAppState } from './state/AppState'
import { CrosshairProvider } from './state/Crosshair'
import type { ContoursMode } from './heatmap/contours'
import type { HeatmapStyle } from './heatmap/render'
import type { LiveSocket } from './api/ws'

function ChartArea() {
  const { toggles } = useAppState()
  const [style, setStyle] = useState<HeatmapStyle>('gradient')
  const [contours, setContours] = useState<ContoursMode>('off')
  // Demo data do zapojení replay/live feedu (issue #27)
  const grid = useMemo(() => demoGrid(), [])
  const allOverlays = useMemo(() => demoOverlays(grid), [grid])
  const profileRows = useMemo(() => demoProfile(grid), [grid])
  const spot = allOverlays.price?.at(-1)?.close ?? null
  // Overlay přepínače odpovídají checkboxům (AC issue #24)
  const overlays = useMemo(
    () =>
      visibleOverlays(allOverlays, {
        gexLevels: toggles.gexLevels,
        sessions: toggles.sessions,
        dynGex: toggles.dynGex,
      }),
    [allOverlays, toggles.gexLevels, toggles.sessions, toggles.dynGex],
  )

  return (
    <>
      <div className="row heatmap-controls" role="toolbar" aria-label="Heatmapa nastavení">
        <label className="toggle">
          Styl
          <select value={style} onChange={(event) => setStyle(event.target.value as HeatmapStyle)}>
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
      <div className="chart-row">
        <main className="chart-area" aria-label="Heatmapa">
          <Heatmap grid={grid} style={style} contours={contours} overlays={overlays} />
        </main>
        <StrikeProfile rows={profileRows} spot={spot} />
      </div>
    </>
  )
}

export default function App({ socket }: { socket?: LiveSocket }) {
  return (
    <AppStateProvider socket={socket}>
      <CrosshairProvider>
        <div className="app">
          <Sidebar watchlist={[{ symbol: 'ES', changePct: null }]} />
          <div className="main-column">
            <InstrumentHeader />
            <TimeframeRow />
            <TogglesRow />
            <ChartArea />
            <StatusBar />
          </div>
        </div>
      </CrosshairProvider>
    </AppStateProvider>
  )
}
