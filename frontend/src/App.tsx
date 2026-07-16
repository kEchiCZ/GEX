/** Kořenový layout aplikace (SPEC 7.1) s heatmapou, overlayi a playbackem (SPEC 7.3). */
import { useMemo, useState } from 'react'
import './App.css'
import { TimeframeRow, TogglesRow } from './components/ControlRows'
import { Heatmap } from './components/Heatmap'
import { InstrumentHeader } from './components/InstrumentHeader'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { BottomPanels } from './components/BottomPanels'
import { PlaybackBar } from './components/PlaybackBar'
import { StrikeProfile } from './components/StrikeProfile'
import { visibleOverlays } from './heatmap/overlays'
import { sliceGrid, sliceOverlays, slicePanels } from './replay/slice'
import { useDayData } from './replay/useDayData'
import { usePlayback } from './replay/usePlayback'
import { AppStateProvider, useAppState } from './state/AppState'
import { CrosshairProvider } from './state/Crosshair'
import type { ContoursMode } from './heatmap/contours'
import type { HeatmapStyle } from './heatmap/render'
import type { LiveSocket } from './api/ws'

function ChartArea() {
  const { toggles, symbol, selectedExpiry } = useAppState()
  const [style, setStyle] = useState<HeatmapStyle>('gradient')
  const [contours, setContours] = useState<ContoursMode>('off')

  // Denní dataset: /replay balík (jediný fetch), fallback demo (AC #27: bez fetch per frame)
  const today = useMemo(() => new Date().toISOString().slice(0, 10), [])
  const day = useDayData(symbol, selectedExpiry, today)
  const playback = usePlayback(day.grid.minutes)

  // Přetáčení = synchronní krájení všech panelů v paměti
  const grid = useMemo(
    () => (playback.isLive ? day.grid : sliceGrid(day.grid, playback.position)),
    [day.grid, playback.isLive, playback.position],
  )
  const panelSeries = useMemo(
    () => (playback.isLive ? day.panels : slicePanels(day.panels, playback.position)),
    [day.panels, playback.isLive, playback.position],
  )
  const allOverlays = useMemo(
    () => (playback.isLive ? day.overlays : sliceOverlays(day.overlays, playback.position)),
    [day.overlays, playback.isLive, playback.position],
  )
  const profileRows = useMemo(() => {
    if (day.profileByMinute) {
      const index = Math.min(playback.position, day.profileByMinute.length - 1)
      return day.profileByMinute[index] ?? []
    }
    return day.demoProfileRows ?? []
  }, [day, playback.position])
  const spot = useMemo(() => {
    const start = Math.min(playback.position, day.spotSeries.length - 1)
    for (let index = start; index >= 0; index -= 1) {
      const value = day.spotSeries[index]
      if (value !== null) return value
    }
    return null
  }, [day.spotSeries, playback.position])

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
        <span className="muted" data-testid="data-source">
          {day.source === 'replay' ? `replay ${today}` : 'demo data'}
        </span>
      </div>
      <div className="chart-row">
        <div className="chart-column">
          <main className="chart-area" aria-label="Heatmapa">
            <Heatmap grid={grid} style={style} contours={contours} overlays={overlays} />
          </main>
          <BottomPanels
            data={panelSeries}
            visible={{ vol: toggles.vol, optVol: toggles.optVol, delta: toggles.delta }}
          />
          <PlaybackBar playback={playback} />
        </div>
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
