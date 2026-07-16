/** Kořenový layout aplikace (SPEC 7.1) s obrazovkami Graf / Dashboard / Console / Settings. */
import { useMemo, useState } from 'react'
import './App.css'
import { useAnnotations } from './annotations/useAnnotations'
import { TimeframeRow, TogglesRow } from './components/ControlRows'
import { Console } from './components/Console'
import { Dashboard } from './components/Dashboard'
import { Heatmap } from './components/Heatmap'
import { InstrumentHeader } from './components/InstrumentHeader'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { BottomPanels } from './components/BottomPanels'
import { PlaybackBar } from './components/PlaybackBar'
import { SettingsView } from './components/SettingsView'
import { StrikeProfile } from './components/StrikeProfile'
import { visibleOverlays } from './heatmap/overlays'
import type { PriceStyle } from './heatmap/overlays'
import { DEFAULT_VIEW } from './heatmap/view'
import type { ViewTransform } from './heatmap/view'
import { aggregateDay } from './replay/aggregate'
import { sliceGrid, sliceOverlays, slicePanels } from './replay/slice'
import { useDayData } from './replay/useDayData'
import { usePlayback } from './replay/usePlayback'
import { AppStateProvider, INTERVAL_MINUTES, useAppState } from './state/AppState'
import { CrosshairProvider } from './state/Crosshair'
import type { ActiveTool } from './annotations/model'
import type { ContoursMode } from './heatmap/contours'
import type { HeatmapStyle } from './heatmap/render'
import type { LiveSocket } from './api/ws'

const ANNOTATION_TOOLS: Array<{ tool: ActiveTool; label: string }> = [
  { tool: null, label: 'Kurzor' },
  { tool: 'arrow', label: 'Šipka' },
  { tool: 'line', label: 'Linie' },
  { tool: 'freehand', label: 'Freehand' },
  { tool: 'eraser', label: 'Guma' },
]

/** Poslední ne-null hodnota řady do pozice (spot, walls pro dashboard). */
function lastValue(series: (number | null)[] | undefined, position: number): number | null {
  if (!series) return null
  for (let index = Math.min(position, series.length - 1); index >= 0; index -= 1) {
    const value = series[index]
    if (value !== null) return value
  }
  return null
}

function MainContent() {
  const { toggles, symbol, selectedExpiry, view, timeframe, interval } = useAppState()
  const [style, setStyle] = useState<HeatmapStyle>('gradient')
  const [contours, setContours] = useState<ContoursMode>('off')
  const [annotationTool, setAnnotationTool] = useState<ActiveTool>(null)
  const [annotationColor, setAnnotationColor] = useState('#e8c14b')
  // Deep-link: ?price=candles&opacity=60 (i pro automatizované snímky)
  const [priceStyle, setPriceStyle] = useState<PriceStyle>(() =>
    new URLSearchParams(window.location.search).get('price') === 'candles' ? 'candles' : 'line',
  )
  const [priceOpacity, setPriceOpacity] = useState(() => {
    const raw = Number(new URLSearchParams(window.location.search).get('opacity'))
    return Number.isFinite(raw) && raw >= 10 && raw <= 100 ? raw / 100 : 1
  })

  // Denní dataset: /replay balík (jediný fetch), fallback demo (AC #27: bez fetch per frame)
  const today = useMemo(() => new Date().toISOString().slice(0, 10), [])
  const rawDay = useDayData(symbol, selectedExpiry, today, timeframe)
  // Timeframe: agregace 1m dat do košů v paměti (Daily má sloupec = den, koše se nepoužijí)
  const bucketMinutes = timeframe === 'daily' ? 1 : INTERVAL_MINUTES[interval]
  const day = useMemo(() => aggregateDay(rawDay, bucketMinutes), [rawDay, bucketMinutes])
  const playback = usePlayback(day.grid.minutes)
  // Pohled grafu (pan/zoom os) — sdílený heatmapou a spodními panely (společná osa X)
  const [chartView, setChartView] = useState<ViewTransform>(DEFAULT_VIEW)
  // Anotace: persistence per instrument + den (SPEC 7.4)
  const annotationsState = useAnnotations(symbol, today)

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
  const spot = useMemo(
    () => lastValue(day.spotSeries, playback.position),
    [day.spotSeries, playback.position],
  )

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

  if (view === 'dashboard') {
    return (
      <Dashboard
        profileRows={profileRows}
        spot={spot}
        callWall={lastValue(
          day.overlays.walls?.find((line) => line.name === 'call_wall')?.series,
          playback.position,
        )}
        putWall={lastValue(
          day.overlays.walls?.find((line) => line.name === 'put_wall')?.series,
          playback.position,
        )}
      />
    )
  }
  if (view === 'console') return <Console />
  if (view === 'settings') return <SettingsView />

  return (
    <>
      <TimeframeRow />
      <TogglesRow />
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
        <label className="toggle">
          Cena
          <select
            value={priceStyle}
            onChange={(event) => setPriceStyle(event.target.value as PriceStyle)}
            aria-label="Styl ceny"
          >
            <option value="line">Křivka</option>
            <option value="candles">Svíčky</option>
          </select>
        </label>
        <label className="toggle">
          Viditelnost
          <input
            type="range"
            min={10}
            max={100}
            value={Math.round(priceOpacity * 100)}
            onChange={(event) => setPriceOpacity(Number(event.target.value) / 100)}
            aria-label="Viditelnost ceny"
            className="opacity-slider"
          />
        </label>
        <span className="separator" />
        {ANNOTATION_TOOLS.map(({ tool, label }) => (
          <button
            key={label}
            className={annotationTool === tool ? 'chip active' : 'chip'}
            onClick={() => setAnnotationTool(tool)}
          >
            {label}
          </button>
        ))}
        <input
          type="color"
          aria-label="Barva anotace"
          value={annotationColor}
          onChange={(event) => setAnnotationColor(event.target.value)}
        />
        <span className="muted" data-testid="data-source">
          {day.source === 'replay' ? `replay ${today}` : 'demo data'}
        </span>
      </div>
      <div className="chart-row">
        <div className="chart-column">
          <main className="chart-area" aria-label="Heatmapa">
            <Heatmap
              grid={grid}
              style={style}
              contours={contours}
              overlays={overlays}
              minuteLabels={day.minuteLabels}
              priceStyle={priceStyle}
              priceOpacity={priceOpacity}
              annotations={annotationsState.annotations}
              annotationTool={annotationTool}
              annotationColor={annotationColor}
              onAnnotationCreate={(payload) => void annotationsState.create(payload)}
              onAnnotationErase={(id) => void annotationsState.erase(id)}
              view={chartView}
              onViewChange={setChartView}
            />
          </main>
          <BottomPanels
            data={panelSeries}
            visible={{ vol: toggles.vol, optVol: toggles.optVol, delta: toggles.delta }}
            time={{ offsetX: chartView.offsetX, zoomX: chartView.zoomX }}
          />
          <PlaybackBar playback={playback} />
        </div>
        <StrikeProfile rows={profileRows} spot={spot} />
      </div>
    </>
  )
}

function Shell() {
  const { theme } = useAppState()
  return (
    <div className="app" data-theme={theme}>
      <Sidebar />
      <div className="main-column">
        <InstrumentHeader />
        <MainContent />
        <StatusBar />
      </div>
    </div>
  )
}

export default function App({ socket }: { socket?: LiveSocket }) {
  return (
    <AppStateProvider socket={socket}>
      <CrosshairProvider>
        <Shell />
      </CrosshairProvider>
    </AppStateProvider>
  )
}
