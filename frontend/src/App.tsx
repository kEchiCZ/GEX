/** Kořenový layout aplikace (SPEC 7.1) s obrazovkami Graf / Dashboard / Console / Settings. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import './App.css'
import { alignSeriesToLabels, signalGateInfo } from './api/news'
import { buildNewsMarkers, significantOnly } from './heatmap/newsMarkers'
import type { NewsMarker } from './heatmap/newsMarkers'
import { buildJournalMarkers } from './heatmap/journalMarkers'
import { fetchJournal } from './api/journal'
import type { JournalEntry } from './api/journal'
import { NewsMarkerDialog } from './components/NewsMarkerDialog'
import { buildSignalMarkers } from './heatmap/signalMarkers'
import { BriefingView } from './components/BriefingView'
import { JournalView } from './components/JournalView'
import { useSentimentState } from './hooks/useSentimentState'
import { useSentimentDaily } from './hooks/useSentimentDaily'
import { alignPlaneProfiles, useGreekPlane } from './hooks/useGreekPlane'
import { dayLabel } from './replay/daily'
import { useAnnotations } from './annotations/useAnnotations'
import { NewsView } from './components/NewsView'
import { useNews } from './hooks/useNews'
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
import { ChainView } from './components/ChainView'
import { SetupsView } from './components/SetupsView'
import { StatsView } from './components/StatsView'
import { StrikeProfile } from './components/StrikeProfile'
import { useSetups } from './hooks/useSetups'
import { buildGexGrid, projectGexField } from './heatmap/gexmode'
import { extendDailyGrid, forwardBoundaries, forwardLabels, futureBlocks, projectDailyForward } from './heatmap/dailyforward' // prettier-ignore
import { useGexForward } from './hooks/useGexForward'
import { HEATMAP_MODES, HEATMAP_SCALES, buildModeGrid } from './heatmap/modes'
import type { HeatmapScale, MeasuredHeatmapMode } from './heatmap/modes'
import { projectGrid, projectionLabels, projectionLength } from './heatmap/projection'
import { expirySettleUtc, sessionDateFor } from './instrument/expiry'
import { sessionDateIso } from './instrument/tz'
import { SETUP_COLORS, resolveSecondaryWalls, visibleOverlays } from './heatmap/overlays'
import type { LevelLine, PriceStyle } from './heatmap/overlays'
import { CHARM_PALETTE, DEFAULT_SIGNED_PALETTE, VANNA_PALETTE } from './heatmap/render'
import { DEFAULT_VIEW, ZOOM_MAX, ZOOM_MIN, visiblePriceRange } from './heatmap/view'
import type { ViewTransform } from './heatmap/view'
import { gexRegime, profileZeroNearest } from './instrument/regime'
import { settleWatchLevel } from './instrument/settlewatch'
import { pcrAt, pcrVolumeSeries } from './instrument/sentiment'
import { dataAgeMinutes, ohlcCoverage, STALE_AFTER_MINUTES } from './instrument/coverage'
import { priceTick } from './instrument/tick'
import {
  WALLS_MODES,
  centerSeries,
  peakSeries,
  ridgeTracks,
  smoothSeries,
} from './heatmap/wallsModes'
import type { WallsMode } from './heatmap/wallsModes'
import { projectedSessions } from './instrument/sessions'
import { aggregateDay, aggregateLive } from './replay/aggregate'
import { sliceGrid, sliceOverlays, slicePanels } from './replay/slice'
import { useAggregateProfile } from './replay/useAggregateProfile'
import { EMPTY_LIVE, minuteLabel, useDayData } from './replay/useDayData'
import { usePlayback } from './replay/usePlayback'
import { AppStateProvider, INTERVAL_MINUTES, useAppState } from './state/AppState'
import { CrosshairProvider } from './state/Crosshair'
import { clampedNumber, clampedNumberMap, oneOf, priceRangeMap, usePersistentState } from './state/persist' // prettier-ignore
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
function lastWeakFlag(flags: (boolean | null)[]): boolean | null {
  for (let index = flags.length - 1; index >= 0; index -= 1) {
    if (flags[index] !== null) return flags[index]
  }
  return null
}

function lastValue(series: (number | null)[] | undefined, position: number): number | null {
  if (!series) return null
  for (let index = Math.min(position, series.length - 1); index >= 0; index -= 1) {
    const value = series[index]
    if (value !== null) return value
  }
  return null
}

function MainContent() {
  const {
    toggles,
    symbol,
    selectedExpiry,
    view,
    setView,
    setJournalDraft,
    tradersMode,
    timeframe,
    interval,
    setPriceInfo,
    setRegimeInfo,
    setSettleWatch,
    setOhlcCoverage,
    signalMode,
    underlayPlane,
    forwardRange,
    newsMarkerFilter,
    gexUnits,
    oiSource,
    socket,
  } = useAppState()
  // Zprávy a sentiment (#288/#289) — jeden zdroj pro panel, sidebar i chip
  const newsData = useNews()
  // Stav RiskOn/RiskOff (#295) — varovný badge šipek při unconfirmed změně
  const sentState = useSentimentState()
  // Volby grafu přežívají refresh (ADR-0007, #167); URL deep-link má přednost
  const [style, setStyle] = usePersistentState<HeatmapStyle>(
    'style',
    'gradient',
    oneOf(['gradient', 'blobs']),
  )
  const [contours, setContours] = usePersistentState<ContoursMode>(
    'contours',
    'off',
    oneOf(['off', 'major', 'all']),
  )
  // Persistovaný 'dyn_gex' z dob módu spadne reviverem na 'oi' (#242)
  const [mode, setMode] = usePersistentState<MeasuredHeatmapMode>(
    'mode',
    'oi',
    oneOf(HEATMAP_MODES.map((item) => item.value)),
  )
  const [heatScale, setHeatScale] = usePersistentState<HeatmapScale>(
    'scale',
    'linear',
    oneOf(HEATMAP_SCALES.map((item) => item.value)),
  )
  const [wallsMode, setWallsMode] = usePersistentState<WallsMode>(
    'walls',
    'off',
    oneOf(WALLS_MODES.map((item) => item.value)),
  )
  const [annotationTool, setAnnotationTool] = useState<ActiveTool>(null)
  const [annotationColor, setAnnotationColor] = useState('#e8c14b')
  // Replay lišta je skrytá — aplikace jede defaultně live (přání uživatele)
  const [showReplay, setShowReplay] = useState(false)
  // Tažitelný předěl mezi grafem a pravým panelem (graf se přizpůsobí sám)
  const [profileWidth, setProfileWidth] = usePersistentState(
    'profileWidth',
    260,
    clampedNumber(180, 2000),
  )
  const dividerDragRef = useRef<{ x: number; width: number } | null>(null)
  // Tažitelný vodorovný předěl nad spodními panely — sdílená výška všech (#169)
  const [panelHeight, setPanelHeight] = usePersistentState(
    'panelHeight',
    84,
    clampedNumber(50, 320),
  )
  const panelDragRef = useRef<{ y: number; height: number } | null>(null)
  // Logická velikost heatmapy — pravý profil sdílí její Y měřítko
  const [heatSize, setHeatSize] = useState({ width: 1200, height: 640 })
  // Deep-link: ?price=line&opacity=60 (i pro automatizované snímky) přebíjí uložený stav
  const urlParams = new URLSearchParams(window.location.search)
  const urlOpacity = Number(urlParams.get('opacity'))
  const [priceStyle, setPriceStyle] = usePersistentState<PriceStyle>(
    'priceStyle',
    'candles',
    oneOf(['line', 'candles']),
    urlParams.get('price') === 'line' ? 'line' : null,
  )
  const [priceOpacity, setPriceOpacity] = usePersistentState(
    'priceOpacity',
    1,
    clampedNumber(0.1, 1),
    Number.isFinite(urlOpacity) && urlOpacity >= 10 && urlOpacity <= 100 ? urlOpacity / 100 : null,
  )

  // Denní dataset: /replay balík (jediný fetch), fallback demo (AC #27: bez fetch per frame).
  // `rawDay` je identitou stabilní napříč spot ticky, živá cena jde zvlášť v `live` (#141).
  // Obchodní den = Globex seance (#512): po 17:00 CT běží seance zítřka —
  // /replay pro ten den sešije večer na serveru. Kontrola 1×/min (#508):
  // aplikace běžící přes hranici seance se sama překlopí na nový den,
  // místo aby do reloadu fetchovala a zobrazovala včerejšek.
  const [today, setToday] = useState(() => sessionDateIso())
  useEffect(() => {
    const timer = window.setInterval(() => {
      const current = sessionDateIso()
      setToday((previous) => (previous === current ? previous : current))
    }, 60_000)
    return () => window.clearInterval(timer)
  }, [])
  // Proběhlá expirace se čte jako replay svého posledního dne (#352) — bez
  // socketu: kanály price/spot/flow jsou per symbol a přilepily by dnešní
  // svíčky do historického dne.
  const viewDate = sessionDateFor(selectedExpiry, today)
  const isHistoricalExpiry = viewDate !== today
  const {
    day: rawDay,
    live,
    staleData,
  } = useDayData(
    symbol,
    selectedExpiry,
    viewDate,
    timeframe,
    isHistoricalExpiry ? undefined : socket,
  )
  // FA zdroj OI (#232): opt-in přepínač per symbol. Aktivní je jen když FA
  // data opravdu existují — bez řady oiest se poctivě padá na měřené
  // (a badge se nekreslí, aby graf netvrdil odhad, který nemá).
  const faActive = oiSource === 'fa' && rawDay.rawFa !== null
  // Heatmap mód/škála: čistý přepočet ze surové matice (SPEC 4.3, bez fetch).
  // Dyn GEX už není mód — je to podkladová vrstva (#242), viz gexUnderDay níž.
  // Při FA zdroji se módy počítají nad maticí s OI_est (měřená zůstává netknutá).
  const modeDay = useMemo(() => {
    const raw = faActive ? rawDay.rawFa : rawDay.raw
    if (!raw) return rawDay
    if (!faActive && mode === 'oi' && heatScale === 'linear') return rawDay
    return { ...rawDay, grid: buildModeGrid(raw, mode, heatScale) }
  }, [rawDay, mode, heatScale, faActive])
  // Timeframe: agregace 1m dat do košů v paměti (Daily má sloupec = den, koše se nepoužijí)
  const bucketMinutes = timeframe === 'daily' ? 1 : INTERVAL_MINUTES[interval]
  const day = useMemo(() => aggregateDay(modeDay, bucketMinutes), [modeDay, bucketMinutes])
  // Podkladová plocha (#242 → #204): gamma z /replay balíku; charm/vanna se
  // stahují a odebírají jen když jsou zobrazené (kanál per plocha)
  const greekPlane = useGreekPlane(
    symbol,
    selectedExpiry,
    viewDate,
    underlayPlane,
    isHistoricalExpiry ? undefined : socket,
  )
  const planeProfiles = useMemo(() => {
    if (underlayPlane === 'gex') {
      // FA zdroj (#232): Dyn GEX podklad z FA profilů (tentýž odhad jako
      // heatmapa a FA levels); charm/vanna FA variantu nemají
      return faActive && rawDay.gexProfileFa ? rawDay.gexProfileFa : rawDay.gexProfile
    }
    if (underlayPlane === 'off') return null
    if (greekPlane.profiles.length === 0) return null
    return alignPlaneProfiles(greekPlane.profiles, rawDay.minutesIso)
  }, [underlayPlane, rawDay.gexProfile, rawDay.gexProfileFa, rawDay.minutesIso, greekPlane.profiles, faActive]) // prettier-ignore
  const planeField =
    underlayPlane === 'gex'
      ? faActive && day.gexFieldFa
        ? day.gexFieldFa
        : day.gexField
      : greekPlane.field
  const planePalette =
    underlayPlane === 'charm'
      ? CHARM_PALETTE
      : underlayPlane === 'vanna'
        ? VANNA_PALETTE
        : DEFAULT_SIGNED_PALETTE
  // Modelované pole POD měřeným módem — průhledné buňky měřené vrstvy ukážou
  // pole, koncentrace ho překryjí. Stejná pipeline jako hlavní grid (agregace
  // košů, slice, projekce), aby rozměry seděly 1:1.
  const gexUnderDay = useMemo(() => {
    if (underlayPlane === 'off') return null
    // Daily umí zatím jen Dyn GEX (#572) — charm/vanna forward až po ověření čtení
    if (timeframe !== 'intraday' && underlayPlane !== 'gex') return null
    if (!planeProfiles || planeProfiles.every((row) => row === null)) return null
    const built = buildGexGrid(planeProfiles, rawDay.grid.strikes, rawDay.grid.minutes, heatScale, gexUnits) // prettier-ignore
    return aggregateDay({ ...rawDay, grid: built }, bucketMinutes)
  }, [underlayPlane, timeframe, planeProfiles, rawDay, heatScale, gexUnits, bucketMinutes])

  const playback = usePlayback(day.grid.minutes)
  // Forward GEX (#572): bloky budoucích dnů — jen Daily + Dyn GEX podklad.
  // V replay (přetáčení) se projekce nekreslí (ADR-0006), guard níže.
  const forwardBlocksAll = useGexForward(
    symbol,
    timeframe === 'daily' && underlayPlane === 'gex' && toggles.projection,
  )
  const lastDailyDate = timeframe === 'daily' ? day.overlays.timestamp : ''
  const dailyForward = useMemo(() => {
    if (timeframe !== 'daily' || underlayPlane !== 'gex' || !toggles.projection) return []
    if (!playback.isLive || !lastDailyDate) return []
    // Pojistka proti staré ose: první forward den musí navazovat na poslední
    // naměřený sloupec (engine počítá od dnešní seance) — jinak by v ose
    // vznikla tichá díra
    if (!forwardBlocksAll.some((block) => block.day === lastDailyDate)) return []
    return futureBlocks(forwardBlocksAll, lastDailyDate, forwardRange)
  }, [timeframe, underlayPlane, toggles.projection, playback.isLive, lastDailyDate, forwardBlocksAll, forwardRange]) // prettier-ignore
  const dailyForwardMarkers = useMemo(
    () => (dailyForward.length > 0 ? forwardBoundaries(dailyForward, day.grid.minutes) : []),
    [dailyForward, day.grid],
  )
  // Živá vrstva (#141): svíčky ze spot kanálu agregované do stejných košů jako den.
  // Při přetáčení (ne-live) živá cena do grafu nepatří.
  const liveOverlay = useMemo(
    () =>
      playback.isLive
        ? aggregateLive(
            live,
            bucketMinutes,
            modeDay.grid.minutes,
            day.overlays.price ?? [],
            modeDay.minutesIso,
          )
        : EMPTY_LIVE,
    [live, bucketMinutes, modeDay.grid.minutes, modeDay.minutesIso, day.overlays.price, playback.isLive], // prettier-ignore
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
  // GEX režim badge (#209): živý spot vůči flip zóně (měřený × dynamický flip).
  // Živé hodnoty, ne playback řez — badge je kontext „teď", stejně jako priceInfo.
  // Záměrně NEzvážený profil (#569): flip je cenová úroveň a nesmí záviset
  // na zobrazovací jednotce (P²/100 nuly nemění, ale interpolaci mezi uzly ano).
  useEffect(() => {
    const spots = day.spotSeries.filter((value): value is number => value !== null)
    const liveSpot = liveOverlay.bars.at(-1)?.close ?? spots.at(-1) ?? null
    const flipSeries = day.overlays.levels?.find((line) => line.name === 'flip')?.series
    const measuredFlip = flipSeries ? lastValue(flipSeries, flipSeries.length - 1) : null
    const profiles = day.gexProfile ?? []
    let lastProfile = null
    for (let i = profiles.length - 1; i >= 0; i -= 1) {
      if (profiles[i]) {
        lastProfile = profiles[i]
        break
      }
    }
    const dynamicFlip =
      lastProfile && liveSpot !== null ? profileZeroNearest(lastProfile, liveSpot) : null
    setRegimeInfo({
      state: gexRegime(liveSpot, measuredFlip, dynamicFlip),
      measuredFlip,
      dynamicFlip,
    })
    // Settle watch (#603): klíčová zeď dne (silné před slabými, pak nejbližší)
    const wallCandidates = (day.overlays.walls ?? []).map((line) => ({
      name: line.name,
      level: lastValue(line.series, line.series.length - 1) ?? Number.NaN,
      weak: line.weak ? (lastWeakFlag(line.weak) ?? null) : null,
    }))
    setSettleWatch(settleWatchLevel(wallCandidates, liveSpot))
  }, [day.spotSeries, day.overlays.levels, day.overlays.walls, day.gexProfile, liveOverlay.bars, setRegimeInfo, setSettleWatch]) // prettier-ignore
  // Pokrytí OHLC do hlavičky (#470) — počítá se nad 1m osou, ne nad koši, aby
  // číslo znamenalo minuty dne bez ohledu na zvolený timeframe
  useEffect(() => {
    setOhlcCoverage(
      ohlcCoverage(
        rawDay.minutesIso,
        (rawDay.overlays.price ?? []).map((bar) => bar.minuteIdx),
      ),
    )
  }, [rawDay.minutesIso, rawDay.overlays.price, setOhlcCoverage])
  // Čas posledních dat grafu (#470): živá minuta má přednost před poslední naměřenou.
  // Tiká po 30 s, aby stáří stárlo samo — bez ticku by značka po zamrznutí sběru
  // ukazovala „čerstvo" tak dlouho, dokud nepřijde jiný render.
  const [stampNow, setStampNow] = useState(() => new Date())
  useEffect(() => {
    const timer = setInterval(() => setStampNow(new Date()), 30_000)
    return () => clearInterval(timer)
  }, [])
  const dataStamp = useMemo(() => {
    const iso = liveOverlay.minutesIso.at(-1) ?? rawDay.lastMinuteIso
    const ageMinutes = dataAgeMinutes(iso, stampNow)
    if (!iso || ageMinutes === null) return null
    return {
      label: minuteLabel(iso),
      ageMinutes,
      stale: ageMinutes >= STALE_AFTER_MINUTES,
    }
  }, [liveOverlay.minutesIso, rawDay.lastMinuteIso, stampNow])
  // Pohled grafu (pan/zoom os) — sdílený heatmapou a spodními panely (společná osa X)
  const [chartView, setChartView] = useState<ViewTransform>(DEFAULT_VIEW)
  // Zoom X per timeframe přežívá refresh i změnu instrumentu (#419);
  // reset pohledu (dvojklik/⟲) uložený zoom TF maže → návrat k fit-to-width
  const [chartZoom, setChartZoom] = usePersistentState<Record<string, number>>(
    'chartZoomX',
    {},
    clampedNumberMap(ZOOM_MIN, ZOOM_MAX),
  )
  const zoomKey = timeframe === 'daily' ? 'daily' : `intraday:${interval}`
  const savedZoomX = chartZoom[zoomKey] ?? null
  const persistZoomX = useCallback(
    (zoomX: number) => setChartZoom((previous) => ({ ...previous, [zoomKey]: zoomX })),
    [zoomKey, setChartZoom],
  )
  // Viditelné cenové pásmo osy Y per instrument (#422) — kotva na cenu, ne na
  // zoomY/offsetY, aby obnovení sedělo i po rozšíření obálky strikes
  const [chartYRange, setChartYRange] = usePersistentState<
    Record<string, { top: number; bottom: number }>
  >('chartYRange', {}, priceRangeMap())
  const yRangeKey = timeframe === 'daily' ? `daily:${symbol}` : symbol
  const savedYRange = chartYRange[yRangeKey] ?? null
  // Uložení pásma Y — volá se JEN z uživatelských gest (Heatmap onUserYRange,
  // drag pravé osy v profilu): tam pohled a grid vždy patří k sobě (#426)
  const storeYRange = useCallback(
    (range: { top: number; bottom: number }) =>
      setChartYRange((previous) => {
        const stored = previous[yRangeKey]
        if (
          stored &&
          Math.abs(stored.top - range.top) < 1e-9 &&
          Math.abs(stored.bottom - range.bottom) < 1e-9
        ) {
          return previous
        }
        return { ...previous, [yRangeKey]: range }
      }),
    [yRangeKey, setChartYRange],
  )
  // Reset pohledu (dvojklik/⟲): smaže uložený zoom X i pásmo Y → návrat k auto-fitu
  const clearSavedView = useCallback(() => {
    setChartZoom((previous) => {
      const rest = { ...previous }
      delete rest[zoomKey]
      return rest
    })
    setChartYRange((previous) => {
      const rest = { ...previous }
      delete rest[yRangeKey]
      return rest
    })
  }, [zoomKey, yRangeKey, setChartZoom, setChartYRange])
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
  const annotationsState = useAnnotations(symbol, viewDate)
  // Ctrl+Z / Ctrl+Shift+Z nad kreslením (#590) — tlačítka ↶ ↷ dělají totéž
  const { undo: undoAnnotation, redo: redoAnnotation } = annotationsState
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'z') return
      const tag = (event.target as HTMLElement | null)?.tagName
      // V editovatelných polích si Ctrl+Z řeší prohlížeč sám
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      event.preventDefault()
      if (event.shiftKey) void redoAnnotation()
      else void undoAnnotation()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [undoAnnotation, redoAnnotation])

  // Přetáčení = synchronní krájení všech panelů v paměti
  const grid = useMemo(
    () => (playback.isLive ? day.grid : sliceGrid(day.grid, playback.position)),
    [day.grid, playback.isLive, playback.position],
  )
  // Projekce heatmapy do settle (ADR-0006): poslední naměřený sloupec držený
  // konstantní. Jen intraday a jen LIVE — Daily má sloupec = den; při přetáčení
  // by se projektoval vynulovaný sloupec za pozicí slice (#156).
  const projectedGrid = useMemo(() => {
    // Daily Forward (#572): měřené módy se neprojektují — osa dostane prázdné
    // sloupce budoucích dnů, model nese jen Dyn podklad (gexUnderGrid níže)
    if (timeframe === 'daily') {
      return dailyForward.length > 0 ? extendDailyGrid(grid, dailyForward.length) : grid
    }
    if (!toggles.projection || !selectedExpiry || !playback.isLive) {
      return grid
    }
    const extra = projectionLength(
      day.lastMinuteIso ?? undefined,
      expirySettleUtc(selectedExpiry),
      bucketMinutes,
    )
    return projectGrid(grid, extra)
  }, [grid, toggles.projection, timeframe, selectedExpiry, day.lastMinuteIso, bucketMinutes, playback.isLive, dailyForward]) // prettier-ignore
  // Dyn GEX podklad (#242): stejný slice + projekce jako hlavní grid — projekční
  // zóna nese modelované budoucí sloupce (ADR-0009 fáze 2)
  const gexUnderGrid = useMemo(() => {
    if (!gexUnderDay || !planeProfiles) return null
    const sliced = playback.isLive
      ? gexUnderDay.grid
      : sliceGrid(gexUnderDay.grid, playback.position)
    if (timeframe === 'daily') {
      // Forward GEX (#572): budoucí dny z bloků #519; hranice expirací nese
      // grid v hardEdgesX, ať blur útes nerozmaže
      return dailyForward.length > 0
        ? projectDailyForward(sliced, dailyForward, {
            profiles: planeProfiles,
            scale: heatScale,
            units: gexUnits,
          })
        : sliced
    }
    if (!toggles.projection || !selectedExpiry || !playback.isLive) {
      return sliced
    }
    const extra = projectionLength(
      day.lastMinuteIso ?? undefined,
      expirySettleUtc(selectedExpiry),
      bucketMinutes,
    )
    return projectGexField(sliced, extra, planeField, {
      profiles: planeProfiles,
      lastMinuteIso: day.lastMinuteIso,
      bucketMinutes,
      scale: heatScale,
      units: gexUnits,
    })
  }, [gexUnderDay, planeProfiles, planeField, playback.isLive, playback.position, toggles.projection, timeframe, selectedExpiry, day.lastMinuteIso, bucketMinutes, heatScale, gexUnits, dailyForward]) // prettier-ignore
  const projectionExtra = projectedGrid.minutes - (projectedGrid.dataMinutes ?? projectedGrid.minutes) // prettier-ignore
  const chartLabels = useMemo(() => {
    if (projectionExtra <= 0) return day.minuteLabels
    if (timeframe === 'daily') return [...day.minuteLabels, ...forwardLabels(dailyForward)]
    return [
      ...day.minuteLabels,
      ...projectionLabels(
        day.lastMinuteIso ?? undefined,
        projectionExtra,
        bucketMinutes,
        minuteLabel,
      ),
    ]
  }, [day.minuteLabels, day.lastMinuteIso, projectionExtra, bucketMinutes, timeframe, dailyForward]) // prettier-ignore
  // Markery zpráv se počítají nad CELOU osou včetně projekce (#287): jen tak
  // se nadcházející CPI vykreslí vpravo od živé hrany, kde ho trader čeká.
  // Filtr „Významné" (#408) pouští jen importance ≥ 2 — okrajové titulky
  // plochu nezahltí, FOMC/CPI zůstávají.
  const newsMarkers = useMemo(() => {
    if (!toggles.news) return []
    const important = newsMarkerFilter === 'important'
    return buildNewsMarkers(
      important ? significantOnly(newsData.news) : newsData.news,
      important ? significantOnly(newsData.upcoming) : newsData.upcoming,
      chartLabels,
      minuteLabel,
    )
  }, [toggles.news, newsMarkerFilter, newsData.news, newsData.upcoming, chartLabels])
  // Dialog zpráv kliknutého markeru (#408)
  const [newsDialogMarker, setNewsDialogMarker] = useState<NewsMarker | null>(null)
  // Značky deníku v ose (#673, Traders mode): záznamy symbolu se párují na osu
  // stejným formatterem jako popisky (vzor news markerů). Refetch i při návratu
  // z Deníku (změna view) — nový záznam se má ukázat hned.
  const [journalEntries, setJournalEntries] = useState<JournalEntry[]>([])
  useEffect(() => {
    if (!tradersMode) {
      setJournalEntries([])
      return
    }
    let cancelled = false
    void fetchJournal({ symbol }).then((rows) => {
      if (!cancelled) setJournalEntries(rows)
    })
    return () => {
      cancelled = true
    }
  }, [tradersMode, symbol, view])
  const journalMarkers = useMemo(() => {
    if (!tradersMode || journalEntries.length === 0) return []
    // Daily pohled páruje datem (sloupec = den), intraday minutou
    const format = timeframe === 'daily' ? (iso: string) => dayLabel(iso.slice(0, 10)) : minuteLabel
    return buildJournalMarkers(journalEntries, chartLabels, format)
  }, [tradersMode, journalEntries, chartLabels, timeframe])
  // Shift+klik do plochy (#673): rychlý zápis k minutě pod kurzorem — myšlenka
  // přijde většinou až s odstupem od okamžiku, ✎ u Replay nese jen minutu playbacku
  const handleJournalQuickAdd = useCallback(
    (minuteIdx: number) => {
      if (day.minutesIso.length === 0) return
      const absolute = Math.max(0, Math.min(minuteIdx * bucketMinutes, day.minutesIso.length - 1))
      const iso = day.minutesIso[absolute]
      if (!iso) return
      setJournalDraft({ tsRef: iso })
      setView('journal')
    },
    [day.minutesIso, bucketMinutes, setJournalDraft, setView],
  )
  // Šipky signálů (#295, SPEC 9.0): dropdown vybírá zobrazenou větev (S9 —
  // počítají se obě vždy); ⚠ badge při nepotvrzené intradenní změně stavu
  const signalMarkers = useMemo(() => {
    if (signalMode === 'off') return []
    const branch = signalMode === 'news' ? 'NEWS' : 'COMBINED'
    const rows = newsData.signals.filter((row) => row.mode === branch && row.symbol === symbol)
    return buildSignalMarkers(rows, chartLabels, minuteLabel, {
      warning: sentState?.unconfirmed ?? false,
    })
  }, [signalMode, newsData.signals, symbol, chartLabels, sentState?.unconfirmed])
  // Progres ke gate pro dropdown (SPEC 9.0 „collecting data")
  const signalGate = useMemo(() => signalGateInfo(newsData.stats, symbol), [newsData.stats, symbol])
  // Daily OHLC SentIndexu (#296) — jen když je Daily pohled a panel zapnutý
  const sentimentDaily = useSentimentDaily(symbol, timeframe === 'daily' && toggles.news)
  const panelSeries = useMemo(() => {
    const base = playback.isLive ? day.panels : slicePanels(day.panels, playback.position)
    if (!toggles.news) return base
    // Daily pohled (#296): svíčka per sloupec-den, párovaná datem přes týž
    // formatter, kterým vznikly popisky osy (vzor alignSeriesToLabels)
    if (timeframe === 'daily') {
      const byLabel = new Map(sentimentDaily.map((row) => [dayLabel(row.date), row]))
      const candles = day.minuteLabels.map((label) => {
        const row = byLabel.get(label)
        return row ? { open: row.open, high: row.high, low: row.low, close: row.close } : null
      })
      const sliced = playback.isLive ? candles : candles.slice(0, playback.position + 1)
      return { ...base, sentimentCandles: sliced }
    }
    // Sentiment přichází z jiného zdroje než bary (news-engine → API), takže
    // se páruje podle času, ne podle indexu (#288)
    const aligned = alignSeriesToLabels(newsData.series, day.minuteLabels, minuteLabel)
    const sliced = playback.isLive ? aligned : aligned.slice(0, playback.position + 1)
    return { ...base, sentiment: sliced }
  }, [
    day.panels,
    day.minuteLabels,
    newsData.series,
    sentimentDaily,
    timeframe,
    playback.isLive,
    playback.position,
    toggles.news,
  ])
  const allOverlays = useMemo(
    () => (playback.isLive ? staticOverlays : sliceOverlays(staticOverlays, playback.position)),
    [staticOverlays, playback.isLive, playback.position],
  )
  const profileRows = useMemo(() => {
    if (day.profileByMinute) {
      const index = Math.min(playback.position, day.profileByMinute.length - 1)
      return day.profileByMinute.rowsAt(index)
    }
    return day.demoProfileRows ?? []
  }, [day, playback.position])
  const spot = useMemo(
    () => liveOverlay.bars.at(-1)?.close ?? lastValue(day.spotSeries, playback.position),
    [liveOverlay.bars, day.spotSeries, playback.position],
  )
  // Absolutní hodnoty buňky do tooltipu heatmapy (#470). Zdroj je profil KOŠE, ne
  // surová 1m matice: `day.raw` se agregací nepřevzorkovává, takže indexovat ji
  // indexem koše by ukazovalo cizí minutu. Ukazují se OI i Vol bez ohledu na mód —
  // obojí je měřená veličina té buňky a mód mění jen to, co se z nich kreslí.
  const cellAbsolute = useCallback(
    (bucketIdx: number, strike: number): string | null => {
      const rows = day.profileByMinute?.rowsAt(bucketIdx) ?? day.demoProfileRows
      const row = rows?.find((item) => item.strike === strike)
      if (!row) return null
      const n = (value: number) => Math.round(value).toLocaleString('cs-CZ')
      return `OI C/P ${n(row.callOi)} / ${n(row.putOi)} · Vol C/P ${n(row.callVolume)} / ${n(row.putVolume)}`
    },
    [day.profileByMinute, day.demoProfileRows],
  )
  // Dyn GEX profil minuty pod playbackem (ADR-0009) — poslední s daty do pozice.
  // Při FA zdroji čte GEX křivka pravého profilu FA řadu (#232).
  const gexProfileRow = useMemo(() => {
    const source = faActive && day.gexProfileFa ? day.gexProfileFa : day.gexProfile
    if (!source || source.length === 0) return null
    const index = Math.min(playback.position, source.length - 1)
    for (let i = index; i >= 0; i -= 1) {
      const row = source[i]
      if (row) return row
    }
    return null
  }, [day.gexProfile, day.gexProfileFa, faActive, playback.position])
  // Stabilní props pro těžké (memoizované) děti — živý spot mění jen graf, ne panely/profil
  const panelsVisible = useMemo(
    () => ({
      vol: toggles.vol,
      optVol: toggles.optVol,
      delta: toggles.delta,
      deltaFlow: toggles.deltaFlow,
      evoOi: toggles.evoOi,
      // Checkbox News zapíná zároveň panel Sentiment (#288); markery v grafu
      // se na něj navěsí v #287
      sentiment: toggles.news,
    }),
    [toggles.vol, toggles.optVol, toggles.delta, toggles.deltaFlow, toggles.evoOi, toggles.news],
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
    (next: { offsetY: number; zoomY: number }) => {
      setChartView((view) => ({ ...view, offsetY: next.offsetY, zoomY: next.zoomY }))
      // Drag pravé osy je uživatelské gesto — pásmo Y se persistuje i odsud (#426)
      const range = visiblePriceRange(
        projectedGrid.strikes,
        { offsetX: 0, zoomX: 1, offsetY: next.offsetY, zoomY: next.zoomY },
        heatSize.height,
      )
      if (range) storeYRange(range)
    },
    [projectedGrid.strikes, heatSize.height, storeYRange],
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
    // Checkbox Setupy (#399): vypnutí skryje linie, detektor běží dál
    if (!toggles.setups || minutes === 0) return []
    const line = (name: string, color: string, value: number): LevelLine => {
      // Jen poslední minuta nese hodnotu — kreslí se horizontální projekce s cenovkou
      const series: (number | null)[] = Array.from({ length: minutes }, () => null)
      series[minutes - 1] = value
      return { name, color, series, dash: [6, 5] }
    }
    return activeSetups.flatMap((setup) => [
      line(`setup-entry-${setup.id}`, SETUP_COLORS.entry, setup.entry),
      line(`setup-target-${setup.id}`, SETUP_COLORS.target, setup.target),
      line(`setup-stop-${setup.id}`, SETUP_COLORS.stop, setup.stop),
    ])
  }, [toggles.setups, activeSetups, grid.minutes])

  // GEX žebřík (#244): významné striky k pozici playbacku jako horizontální
  // úrovně s cenovkou — jednobodová série (vzor setup linií), zelená call nad
  // spotem, červená put pod ním; přípona cenovky = podíl na síle strany
  const ladderLines = useMemo<LevelLine[]>(() => {
    const minutes = grid.minutes
    if (!toggles.ladder || minutes === 0 || !day.ladder) return []
    let entry = null
    for (let i = Math.min(playback.position, day.ladder.length - 1); i >= 0; i -= 1) {
      if (day.ladder[i]) {
        entry = day.ladder[i]
        break
      }
    }
    if (!entry) return []
    const line = (strike: number, share: number, side: 'call' | 'put'): LevelLine => {
      const series: (number | null)[] = Array.from({ length: minutes }, () => null)
      series[minutes - 1] = strike
      return {
        name: `ladder-${side}-${strike}`,
        color: side === 'call' ? 'rgba(62,207,142,0.85)' : 'rgba(240,97,109,0.85)',
        series,
        dash: [6, 5],
        labelSuffix: ` · ${Math.round(share * 100)} %`,
      }
    }
    return [
      ...entry.callStrikes.map((strike, i) => line(strike, entry.callShares[i] ?? 0, 'call')),
      ...entry.putStrikes.map((strike, i) => line(strike, entry.putShares[i] ?? 0, 'put')),
    ]
  }, [toggles.ladder, day.ladder, grid.minutes, playback.position])

  // Σ souhrn přes expirace v pravém profilu (čtení celkového positioningu napříč expiracemi)
  const [aggregateOn, setAggregateOn] = useState(false)
  const aggregateRows = useAggregateProfile(
    symbol,
    viewDate,
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
        flowAdjusted: toggles.flowAdjusted,
      }),
    [allOverlays, toggles.gexLevels, toggles.sessions, toggles.dynGex, toggles.flowAdjusted],
  )

  // Seance i v projektované zóně (#195) — všechny seance dne najednou,
  // včetně US Open/Close, které teprve přijdou
  const projectedSessionMarkers = useMemo(() => {
    if (!toggles.sessions || projectionExtra <= 0 || !playback.isLive || !selectedExpiry) return []
    if (timeframe !== 'intraday' || !day.lastMinuteIso) return []
    return projectedSessions(
      day.lastMinuteIso,
      expirySettleUtc(selectedExpiry),
      bucketMinutes,
      day.grid.minutes,
    )
  }, [toggles.sessions, projectionExtra, playback.isLive, selectedExpiry, timeframe, day.lastMinuteIso, bucketMinutes, day.grid.minutes]) // prettier-ignore

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
      // Sekundární zeď (ADR-0008): spárování po úrovních dle přepínače
      walls: [
        ...resolveSecondaryWalls(baseOverlays.walls ?? [], toggles.secondaryWall),
        ...computedWalls,
      ],
      levels: [...(baseOverlays.levels ?? []), ...setupLines, ...ladderLines],
      // Budoucí seance v projekci (#195)
      sessions: [...(baseOverlays.sessions ?? []), ...projectedSessionMarkers],
      // Markery zpráv (#287) — osa nese i projekční část, takže nadcházející
      // scheduled eventy padnou napravo od živé hrany
      newsMarkers,
      // Značky deníku (#673) — jen v Traders mode (memo je bez něj prázdné)
      journalMarkers,
      // Šipky signálů (#295): při přetáčení jen ty, co v čase pozice existovaly
      signals: playback.isLive
        ? signalMarkers
        : signalMarkers.filter((signal) => signal.minuteIdx <= playback.position),
    }),
    [baseOverlays, computedWalls, setupLines, ladderLines, toggles.secondaryWall, projectedSessionMarkers, newsMarkers, journalMarkers, signalMarkers, playback.isLive, playback.position], // prettier-ignore
  )

  if (view === 'dashboard') {
    return (
      <Dashboard
        profileRows={profileRows}
        spot={spot}
        pcr={day.raw ? pcrAt(day.raw, Math.min(playback.position, day.raw.minutes - 1)) : undefined}
        pcrSeries={day.raw ? pcrVolumeSeries(day.raw) : undefined}
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
  if (view === 'chain') return <ChainView />
  if (view === 'news') return <NewsView />
  if (view === 'setups') return <SetupsView />
  if (view === 'briefing') return <BriefingView />
  if (view === 'journal') return <JournalView />
  if (view === 'stats') return <StatsView />
  if (view === 'console') return <Console />
  if (view === 'settings') return <SettingsView />

  return (
    <>
      <TimeframeRow />
      <TogglesRow signalGate={signalGate} />
      <div className="row heatmap-controls" role="toolbar" aria-label="Heatmapa nastavení">
        <label className="toggle">
          Mode
          <select
            value={mode}
            onChange={(event) => setMode(event.target.value as MeasuredHeatmapMode)}
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
        {/* Undo/redo kreslení (#590) — pokrývá i mazání gumou a přesun (#589) */}
        <button
          className="chip"
          onClick={() => void annotationsState.undo()}
          disabled={!annotationsState.canUndo}
          aria-label="Zpět"
          title="Zpět (Ctrl+Z)"
        >
          ↶
        </button>
        <button
          className="chip"
          onClick={() => void annotationsState.redo()}
          disabled={!annotationsState.canRedo}
          aria-label="Vpřed"
          title="Vpřed (Ctrl+Shift+Z)"
        >
          ↷
        </button>
        <span className="muted" data-testid="data-source">
          {day.source === 'replay'
            ? `replay ${viewDate}${isHistoricalExpiry ? ' · den expirace' : ''}`
            : 'demo data'}
        </span>
        {/* Čas posledních dat u grafu (#470): když engine přestane sbírat, graf
        vypadá jako živý — jen se přestane hýbat. Tady je to vidět hned. */}
        {dataStamp && (
          <span
            className={dataStamp.stale ? 'data-stamp stale' : 'data-stamp'}
            data-testid="data-stamp"
            title={
              dataStamp.stale
                ? `Poslední data jsou ${Math.floor(dataStamp.ageMinutes)} min stará — sběr stojí, nebo je trh zavřený.`
                : 'Čas poslední minuty, kterou graf má (bar nebo snapshot).'
            }
          >
            {dataStamp.stale ? '⊘' : '◷'} Data {dataStamp.label}
          </span>
        )}
        {/* FA zdroj (#232): graf ukazuje ODHAD, ne měření — badge to musí křičet */}
        {faActive && (
          <span
            className="fa-badge"
            data-testid="fa-badge"
            title="OI vrstvy, Dyn GEX podklad i GEX křivka profilu jedou z FA odhadu OI_est = ranní OI + α·klasifikovaný tok (ADR-0011) — je to model, ne měření. OI Δ složka pravého profilu zůstává měřená."
          >
            FA odhad
          </span>
        )}
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
        {/* Rychlý vstup do deníku (#673): okamžik = minuta pod playbackem */}
        <button
          className="chip"
          aria-label="Záznam do deníku k této minutě"
          title="Otevře Deník s předvyplněným okamžikem (aktuální minuta playbacku). Přesnou minutu vybereš Shift+klikem do grafu."
          onClick={() => {
            const minuteIso = playback.isLive
              ? new Date().toISOString()
              : (day.minutesIso[Math.min(playback.position, day.minutesIso.length - 1)] ??
                new Date().toISOString())
            setJournalDraft({ tsRef: minuteIso })
            setView('journal')
          }}
        >
          ✎
        </button>
      </div>
      <div className="chart-row">
        <div className="chart-column">
          <main className="chart-area" aria-label="Heatmapa">
            <Heatmap
              grid={projectedGrid}
              underGrid={gexUnderGrid}
              underPalette={planePalette}
              style={style}
              contours={contours}
              overlays={overlays}
              liveBars={liveOverlay.bars}
              liveLabels={liveOverlay.labels}
              minuteLabels={chartLabels}
              forwardMarkers={dailyForwardMarkers}
              cellAbsolute={cellAbsolute}
              priceStyle={priceStyle}
              priceOpacity={priceOpacity}
              annotations={annotationsState.annotations}
              bucketMinutes={bucketMinutes}
              minutesIso={day.minutesIso}
              annotationTool={annotationTool}
              annotationColor={annotationColor}
              onAnnotationCreate={(payload) => void annotationsState.create(payload)}
              onAnnotationErase={(id) => void annotationsState.erase(id)}
              onAnnotationMove={(id, payload) => void annotationsState.move(id, payload)}
              view={chartView}
              onViewChange={setChartView}
              initialZoomX={savedZoomX}
              initialPriceRange={savedYRange}
              onUserZoomX={persistZoomX}
              onUserYRange={storeYRange}
              onViewReset={clearSavedView}
              fitRange={fitRange}
              onLogicalSizeChange={setHeatSize}
              dateLabel={
                timeframe === 'intraday' ? viewDate.split('-').reverse().join('.') : undefined
              }
              resetKey={`${symbol}|${selectedExpiry}|${timeframe}|${interval}|${viewDate}`}
              priceTick={priceTick(symbol)}
              onNewsMarkerClick={setNewsDialogMarker}
              onJournalMarkerClick={() => setView('journal')}
              onJournalQuickAdd={timeframe === 'intraday' ? handleJournalQuickAdd : undefined}
            />
            {newsDialogMarker && (
              <NewsMarkerDialog
                marker={newsDialogMarker}
                onClose={() => setNewsDialogMarker(null)}
              />
            )}
            {/* Checkbox Setupy (#399): globální viditelnost vrstvy setupů */}
            {toggles.setups && <SetupCard setups={activeSetups} onDismiss={handleDismissSetup} />}
            {/* Data se nedaří obnovit (#516): zobrazený stav je starý — nikdy
                tiše neukazovat zastaralé jako živé */}
            {staleData && (
              <div className="stale-banner" role="status" data-testid="stale-banner">
                {`Data se nedaří obnovit (${staleData.failures}× po sobě) — zobrazen stav z ` +
                  (staleData.lastMinuteIso
                    ? `${new Date(staleData.lastMinuteIso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} (${Math.max(0, Math.round((Date.now() - new Date(staleData.lastMinuteIso).getTime()) / 60000))} min staré)`
                    : 'neznámého času')}
              </div>
            )}
            {day.source === 'demo' && (
              <div className="demo-banner" role="status">
                {isHistoricalExpiry
                  ? `Demo data — pro expiraci ${viewDate} už nejsou uložená data ` +
                    '(mimo retenci 14 dní).'
                  : `Demo data — pro ${symbol} zatím nejsou uložená živá data.` +
                    (timeframe === 'intraday'
                      ? ' Engine začne sbírat do ~5 minut po přidání do watchlistu.'
                      : '')}
              </div>
            )}
          </main>
          {(toggles.vol || toggles.optVol || toggles.delta || toggles.deltaFlow) && (
            <div
              className="panel-divider-h"
              role="separator"
              aria-label="Výška spodních panelů"
              aria-orientation="horizontal"
              onPointerDown={(event) => {
                panelDragRef.current = { y: event.clientY, height: panelHeight }
                event.currentTarget.setPointerCapture(event.pointerId)
              }}
              onPointerMove={(event) => {
                const drag = panelDragRef.current
                if (!drag) return
                // Tažení nahoru panely zvětšuje (předěl sedí nad nimi). Delta myši
                // se dělí počtem viditelných panelů — mění se výška KAŽDÉHO z nich,
                // takže hrana bloku jinak utíká N× rychleji než kurzor (#177)
                const visibleCount = Math.max(
                  1,
                  [toggles.vol, toggles.optVol, toggles.delta, toggles.deltaFlow].filter(Boolean)
                    .length,
                )
                const next = drag.height + (drag.y - event.clientY) / visibleCount
                setPanelHeight(Math.min(320, Math.max(50, Math.round(next))))
              }}
              onPointerUp={() => {
                panelDragRef.current = null
              }}
            />
          )}
          <BottomPanels
            data={panelSeries}
            visible={panelsVisible}
            width={heatSize.width}
            time={panelTime}
            // Shodné měřítko osy X s heatmapou i při projekci (ADR-0006) —
            // panely samy kreslí dál jen naměřená data
            totalMinutes={projectedGrid.minutes}
            height={panelHeight}
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
          gexProfile={gexProfileRow}
          gexUnits={gexUnits}
          axisStrikes={day.grid.strikes}
          symbol={symbol}
          expiry={selectedExpiry}
        />
      </div>
    </>
  )
}

function Shell() {
  const { theme, priceInfo, ohlcCoverage: ohlcCoverageInfo } = useAppState()
  // Ctrl+kolečko / pinch (chodí jako ctrl+wheel) NAD GRAFEM a jeho ukazateli
  // nesmí zoomovat stránku — tam patří zoom grafu (#179). Mimo .chart-row
  // (sidebar, lišty) zůstává zoom prohlížeče plně funkční, jinak by se
  // rozjetý page zoom nedal vrátit (#181); Ctrl+0 / Ctrl± fungují vždy.
  useEffect(() => {
    const blockPageZoom = (event: WheelEvent) => {
      if (!event.ctrlKey) return
      if (event.target instanceof Element && event.target.closest('.chart-row')) {
        event.preventDefault()
      }
    }
    window.addEventListener('wheel', blockPageZoom, { passive: false })
    return () => window.removeEventListener('wheel', blockPageZoom)
  }, [])
  return (
    <div className="app" data-theme={theme}>
      <Sidebar />
      <div className="main-column">
        <InstrumentHeader
          lastPrice={priceInfo.last ?? undefined}
          changePct={priceInfo.changePct ?? undefined}
          ohlc={ohlcCoverageInfo}
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
