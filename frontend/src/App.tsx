/** Kořenový layout aplikace (SPEC 7.1) s obrazovkami Graf / Dashboard / Console / Settings. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
import { SetupCard } from './components/SetupCard'
import { SetupsView } from './components/SetupsView'
import { StrikeProfile } from './components/StrikeProfile'
import { useSetups } from './hooks/useSetups'
import { HEATMAP_MODES, HEATMAP_SCALES, buildModeGrid } from './heatmap/modes'
import type { HeatmapMode, HeatmapScale } from './heatmap/modes'
import { visibleOverlays } from './heatmap/overlays'
import type { LevelLine, PriceStyle } from './heatmap/overlays'
import { DEFAULT_VIEW } from './heatmap/view'
import type { ViewTransform } from './heatmap/view'
import { priceTick } from './instrument/tick'
import {
  WALLS_MODES,
  centerSeries,
  peakSeries,
  ridgeTracks,
  smoothSeries,
} from './heatmap/wallsModes'
import type { WallsMode } from './heatmap/wallsModes'
import { aggregateDay, aggregateLive } from './replay/aggregate'
import { sliceGrid, sliceOverlays, slicePanels } from './replay/slice'
import { useAggregateProfile } from './replay/useAggregateProfile'
import { EMPTY_LIVE, useDayData } from './replay/useDayData'
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
  const { toggles, symbol, selectedExpiry, view, timeframe, interval, setPriceInfo, socket } =
    useAppState()
  const [style, setStyle] = useState<HeatmapStyle>('gradient')
  const [contours, setContours] = useState<ContoursMode>('off')
  const [mode, setMode] = useState<HeatmapMode>('oi')
  const [heatScale, setHeatScale] = useState<HeatmapScale>('linear')
  const [wallsMode, setWallsMode] = useState<WallsMode>('off')
  const [annotationTool, setAnnotationTool] = useState<ActiveTool>(null)
  const [annotationColor, setAnnotationColor] = useState('#e8c14b')
  // Replay lišta je skrytá — aplikace jede defaultně live (přání uživatele)
  const [showReplay, setShowReplay] = useState(false)
  // Tažitelný předěl mezi grafem a pravým panelem (graf se přizpůsobí sám)
  const [profileWidth, setProfileWidth] = useState(260)
  const dividerDragRef = useRef<{ x: number; width: number } | null>(null)
  // Logická velikost heatmapy — pravý profil sdílí její Y měřítko
  const [heatSize, setHeatSize] = useState({ width: 1200, height: 640 })
  // Deep-link: ?price=line&opacity=60 (i pro automatizované snímky); default svíčky
  const [priceStyle, setPriceStyle] = useState<PriceStyle>(() =>
    new URLSearchParams(window.location.search).get('price') === 'line' ? 'line' : 'candles',
  )
  const [priceOpacity, setPriceOpacity] = useState(() => {
    const raw = Number(new URLSearchParams(window.location.search).get('opacity'))
    return Number.isFinite(raw) && raw >= 10 && raw <= 100 ? raw / 100 : 1
  })

  // Denní dataset: /replay balík (jediný fetch), fallback demo (AC #27: bez fetch per frame).
  // `rawDay` je identitou stabilní napříč spot ticky, živá cena jde zvlášť v `live` (#141).
  const today = useMemo(() => new Date().toISOString().slice(0, 10), [])
  const { day: rawDay, live } = useDayData(symbol, selectedExpiry, today, timeframe, socket)
  // Heatmap mód/škála: čistý přepočet ze surové matice (SPEC 4.3, bez fetch)
  const modeDay = useMemo(() => {
    if (!rawDay.raw || (mode === 'oi' && heatScale === 'linear')) return rawDay
    return { ...rawDay, grid: buildModeGrid(rawDay.raw, mode, heatScale) }
  }, [rawDay, mode, heatScale])
  // Timeframe: agregace 1m dat do košů v paměti (Daily má sloupec = den, koše se nepoužijí)
  const bucketMinutes = timeframe === 'daily' ? 1 : INTERVAL_MINUTES[interval]
  const day = useMemo(() => aggregateDay(modeDay, bucketMinutes), [modeDay, bucketMinutes])

  const playback = usePlayback(day.grid.minutes)
  // Živá vrstva (#141): svíčky ze spot kanálu agregované do stejných košů jako den.
  // Při přetáčení (ne-live) živá cena do grafu nepatří.
  const liveOverlay = useMemo(
    () =>
      playback.isLive
        ? aggregateLive(live, bucketMinutes, modeDay.grid.minutes, day.overlays.price ?? [])
        : EMPTY_LIVE,
    [live, bucketMinutes, modeDay.grid.minutes, day.overlays.price, playback.isLive],
  )
  // Koše, které živá vrstva přebírá — jejich statická svíčka se vynechá (jinak dvojí kresba).
  // Klíč je primitivní: mění se jednou za koš, ne s každým tickem.
  const liveBucketKey = liveOverlay.bars.map((bar) => bar.minuteIdx).join(',')
  const staticPrice = useMemo(() => {
    const price = day.overlays.price
    if (!price || liveBucketKey === '') return price
    const taken = new Set(liveBucketKey.split(',').map(Number))
    return price.some((bar) => taken.has(bar.minuteIdx))
      ? price.filter((bar) => !taken.has(bar.minuteIdx))
      : price
  }, [day.overlays.price, liveBucketKey])
  const staticOverlays = useMemo(
    () =>
      staticPrice === day.overlays.price ? day.overlays : { ...day.overlays, price: staticPrice },
    [day.overlays, staticPrice],
  )

  // Hlavička: poslední cena + denní změna vs. otevření dne (živá cena má přednost)
  useEffect(() => {
    const spots = day.spotSeries.filter((value): value is number => value !== null)
    const last = liveOverlay.bars.at(-1)?.close ?? spots.at(-1) ?? null
    const open = spots[0] ?? null
    setPriceInfo({
      last,
      changePct: last !== null && open !== null && open !== 0 ? ((last - open) / open) * 100 : null,
    })
  }, [day.spotSeries, liveOverlay.bars, setPriceInfo])
  // Pohled grafu (pan/zoom os) — sdílený heatmapou a spodními panely (společná osa X)
  const [chartView, setChartView] = useState<ViewTransform>(DEFAULT_VIEW)
  // Cenové pásmo dne pro auto-fit osy Y (fit počítá Heatmap se svou skutečnou výškou)
  const fitRange = useMemo(() => {
    const bars = day.overlays.price ?? []
    if (bars.length === 0) return null
    return {
      low: Math.min(...bars.map((bar) => bar.low ?? bar.close)),
      high: Math.max(...bars.map((bar) => bar.high ?? bar.close)),
    }
  }, [day.overlays.price])
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
    () => (playback.isLive ? staticOverlays : sliceOverlays(staticOverlays, playback.position)),
    [staticOverlays, playback.isLive, playback.position],
  )
  const profileRows = useMemo(() => {
    if (day.profileByMinute) {
      const index = Math.min(playback.position, day.profileByMinute.length - 1)
      return day.profileByMinute[index] ?? []
    }
    return day.demoProfileRows ?? []
  }, [day, playback.position])
  const spot = useMemo(
    () => liveOverlay.bars.at(-1)?.close ?? lastValue(day.spotSeries, playback.position),
    [liveOverlay.bars, day.spotSeries, playback.position],
  )
  // Stabilní props pro těžké (memoizované) děti — živý spot mění jen graf, ne panely/profil
  const panelsVisible = useMemo(
    () => ({
      vol: toggles.vol,
      optVol: toggles.optVol,
      delta: toggles.delta,
      deltaFlow: toggles.deltaFlow,
    }),
    [toggles.vol, toggles.optVol, toggles.delta, toggles.deltaFlow],
  )
  const panelTime = useMemo(
    () => ({ offsetX: chartView.offsetX, zoomX: chartView.zoomX }),
    [chartView.offsetX, chartView.zoomX],
  )
  const profileYView = useMemo(
    () => ({ offsetY: chartView.offsetY, zoomY: chartView.zoomY, baseHeight: heatSize.height }),
    [chartView.offsetY, chartView.zoomY, heatSize.height],
  )
  const handleYViewChange = useCallback(
    (next: { offsetY: number; zoomY: number }) =>
      setChartView((view) => ({ ...view, offsetY: next.offsetY, zoomY: next.zoomY })),
    [],
  )
  const handleAggregateToggle = useCallback(() => setAggregateOn((value) => !value), [])
  const handleDismissSetup = useCallback(
    (id: number) => setDismissedSetups((previous) => [...previous, id]),
    [],
  )
  // Aktivní setupy (ADR-0004): karta nad grafem + úrovně entry/cíl/stop v heatmapě
  const { setups } = useSetups()
  const [dismissedSetups, setDismissedSetups] = useState<number[]>([])
  const activeSetups = useMemo(
    () =>
      setups.filter((setup) => setup.status === 'active' && !dismissedSetups.includes(setup.id)),
    [setups, dismissedSetups],
  )
  const setupLines = useMemo<LevelLine[]>(() => {
    const minutes = grid.minutes
    if (minutes === 0) return []
    const line = (name: string, color: string, value: number): LevelLine => {
      // Jen poslední minuta nese hodnotu — kreslí se horizontální projekce s cenovkou
      const series: (number | null)[] = Array.from({ length: minutes }, () => null)
      series[minutes - 1] = value
      return { name, color, series, dash: [6, 5] }
    }
    return activeSetups.flatMap((setup) => [
      line(`setup-entry-${setup.id}`, 'rgba(77,163,255,0.9)', setup.entry),
      line(`setup-target-${setup.id}`, 'rgba(63,191,111,0.9)', setup.target),
      line(`setup-stop-${setup.id}`, 'rgba(224,85,99,0.9)', setup.stop),
    ])
  }, [activeSetups, grid.minutes])

  // Σ souhrn přes expirace v pravém profilu (Kooperovo čtení celkového positioningu)
  const [aggregateOn, setAggregateOn] = useState(false)
  const aggregateRows = useAggregateProfile(
    symbol,
    today,
    aggregateOn && day.source === 'replay',
    spot,
  )
  const displayedProfileRows = aggregateOn && aggregateRows ? aggregateRows : profileRows

  // Overlay přepínače odpovídají checkboxům (AC issue #24)
  const baseOverlays = useMemo(
    () =>
      visibleOverlays(allOverlays, {
        gexLevels: toggles.gexLevels,
        sessions: toggles.sessions,
        dynGex: toggles.dynGex,
      }),
    [allOverlays, toggles.gexLevels, toggles.sessions, toggles.dynGex],
  )

  // Walls módy (SPEC 4.4): bílé čárkované linie počítané z aktuální vrstvy gridu
  const computedWalls = useMemo<LevelLine[]>(() => {
    if (wallsMode === 'off') return []
    const white = 'rgba(255,255,255,0.85)'
    const dash = [4, 3]
    if (wallsMode === 'flip') {
      const flip = allOverlays.levels?.find((line) => line.name === 'flip')
      return flip ? [{ name: 'walls:flip', color: white, dash, series: flip.series }] : []
    }
    const { minutes, strikes, layers } = grid
    // Signed vrstva se dělí na kladnou (call) a zápornou (put) stranu
    const callLayer =
      layers.call ??
      (layers.signed ? Float32Array.from(layers.signed, (v) => Math.max(0, v)) : null)
    const putLayer =
      layers.put ??
      (layers.signed ? Float32Array.from(layers.signed, (v) => Math.max(0, -v)) : null)
    if (!callLayer || !putLayer) return []
    if (wallsMode === 'ridge') {
      const magnitude = Float32Array.from(callLayer, (v, i) => v + putLayer[i])
      return ridgeTracks(magnitude, minutes, strikes)
        .filter((track) => track.length >= 2) // osamocený bod není hřeben
        .map((track, index) => {
          const series: (number | null)[] = Array.from({ length: minutes }, () => null)
          for (const point of track) series[point.minuteIdx] = point.strike
          return { name: `walls:ridge-${index}`, color: white, dash, series }
        })
    }
    const seriesOf = (layer: Float32Array): (number | null)[] => {
      if (wallsMode === 'peak') return peakSeries(layer, minutes, strikes)
      if (wallsMode === 'center') return centerSeries(layer, minutes, strikes)
      return smoothSeries(peakSeries(layer, minutes, strikes))
    }
    return [
      { name: 'walls:call', color: white, dash, series: seriesOf(callLayer) },
      { name: 'walls:put', color: white, dash, series: seriesOf(putLayer) },
    ]
  }, [wallsMode, grid, allOverlays.levels])

  const overlays = useMemo(
    () => ({
      ...baseOverlays,
      walls: [...(baseOverlays.walls ?? []), ...computedWalls],
      levels: [...(baseOverlays.levels ?? []), ...setupLines],
    }),
    [baseOverlays, computedWalls, setupLines],
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
  if (view === 'setups') return <SetupsView />
  if (view === 'console') return <Console />
  if (view === 'settings') return <SettingsView />

  return (
    <>
      <TimeframeRow />
      <TogglesRow />
      <div className="row heatmap-controls" role="toolbar" aria-label="Heatmapa nastavení">
        <label className="toggle">
          Mode
          <select
            value={mode}
            onChange={(event) => setMode(event.target.value as HeatmapMode)}
            disabled={!rawDay.raw}
            title={rawDay.raw ? undefined : 'Módy jsou dostupné jen nad intraday replay daty'}
            aria-label="Heatmap mód"
          >
            {HEATMAP_MODES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="toggle">
          Scale
          <select
            value={heatScale}
            onChange={(event) => setHeatScale(event.target.value as HeatmapScale)}
            disabled={!rawDay.raw}
            aria-label="Škála heatmapy"
          >
            {HEATMAP_SCALES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="toggle">
          Walls
          <select
            value={wallsMode}
            onChange={(event) => setWallsMode(event.target.value as WallsMode)}
            aria-label="Walls mód"
          >
            {WALLS_MODES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
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
        <button
          className={showReplay ? 'chip active' : 'chip'}
          aria-label="Replay ovládání"
          title="Zobrazit/skrýt přehrávání dne (skryté = vždy live)"
          onClick={() => {
            if (showReplay) playback.goLive() // zavření vrací graf na live
            setShowReplay((value) => !value)
          }}
        >
          ⏮ Replay
        </button>
      </div>
      <div className="chart-row">
        <div className="chart-column">
          <main className="chart-area" aria-label="Heatmapa">
            <Heatmap
              grid={grid}
              style={style}
              contours={contours}
              overlays={overlays}
              liveBars={liveOverlay.bars}
              liveLabels={liveOverlay.labels}
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
              fitRange={fitRange}
              onLogicalSizeChange={setHeatSize}
              dateLabel={
                timeframe === 'intraday' ? today.split('-').reverse().join('.') : undefined
              }
              resetKey={`${symbol}|${selectedExpiry}|${timeframe}|${interval}|${today}`}
              priceTick={priceTick(symbol)}
            />
            <SetupCard setups={activeSetups} onDismiss={handleDismissSetup} />
            {day.source === 'demo' && (
              <div className="demo-banner" role="status">
                Demo data — pro {symbol} zatím nejsou uložená živá data.
                {timeframe === 'intraday' &&
                  ' Engine začne sbírat do ~5 minut po přidání do watchlistu.'}
              </div>
            )}
          </main>
          <BottomPanels
            data={panelSeries}
            visible={panelsVisible}
            width={heatSize.width}
            time={panelTime}
          />
          {showReplay && <PlaybackBar playback={playback} />}
        </div>
        <div
          className="panel-divider"
          role="separator"
          aria-label="Šířka pravého panelu"
          aria-orientation="vertical"
          onPointerDown={(event) => {
            dividerDragRef.current = { x: event.clientX, width: profileWidth }
            event.currentTarget.setPointerCapture(event.pointerId)
          }}
          onPointerMove={(event) => {
            const drag = dividerDragRef.current
            if (!drag) return
            // Tažení doleva panel rozšiřuje; horní mez nechá jen ~360 px na graf
            const next = drag.width + (drag.x - event.clientX)
            const maxWidth =
              typeof window !== 'undefined' ? Math.max(640, window.innerWidth - 360) : 640
            setProfileWidth(Math.min(maxWidth, Math.max(180, Math.round(next))))
          }}
          onPointerUp={() => {
            dividerDragRef.current = null
          }}
        />
        <StrikeProfile
          rows={displayedProfileRows}
          spot={spot}
          width={profileWidth}
          yView={profileYView}
          onYViewChange={handleYViewChange}
          aggregate={day.source === 'replay' ? aggregateOn : null}
          onAggregateToggle={handleAggregateToggle}
        />
      </div>
    </>
  )
}

function Shell() {
  const { theme, priceInfo } = useAppState()
  return (
    <div className="app" data-theme={theme}>
      <Sidebar />
      <div className="main-column">
        <InstrumentHeader
          lastPrice={priceInfo.last ?? undefined}
          changePct={priceInfo.changePct ?? undefined}
        />
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
