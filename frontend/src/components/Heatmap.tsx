/** Canvas heatmapa s overlayi (SPEC 7.2): Gradient/Blobs, contours, pan/zoom,
cenová křivka, sessions, levels/walls linie, crosshair + tooltip.

Data se překreslují do offscreen bitmapy jen při změně gridu/stylu; pan/zoom
i overlaye kreslí hotový bitmap + vektory nad ním — 60 fps drží GPU drawImage.
Crosshair je sdílený kontext (SPEC: synchronizace se spodními panely a profilem).

Rozlišení canvasu sleduje zobrazenou velikost × devicePixelRatio (hi-DPI):
kreslí se v logických CSS pixelech přes setTransform(dpr), takže popisky os
jsou ostré i na velkých monitorech. Souřadnice událostí = CSS pixely.
*/
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useElementSize } from '../hooks/useElementSize'
import { contourLevels, marchingSquares } from '../heatmap/contours'
import type { ContoursMode } from '../heatmap/contours'
import { gaussianBlur, renderGrid } from '../heatmap/render'
import type { SignedPalette } from '../heatmap/render'
import type { HeatmapStyle } from '../heatmap/render'
import type { HeatmapGrid } from '../heatmap/grid'
import type { ForwardBoundary } from '../heatmap/dailyforward'
import {
  breaksOnJump,
  candleGeometry,
  formatLevel,
  fractionalRow,
  isLevelJump,
  hasLevelProjection,
  lastLevelValue,
  levelLabel,
  pricePolyline,
  tickIndices,
} from '../heatmap/overlays'
import type { OverlayData, PriceBar, PriceStyle } from '../heatmap/overlays'
import { journalMarkerNear } from '../heatmap/journalMarkers'
import type { JournalMarker } from '../heatmap/journalMarkers'
import { markerColor, markerNear, markerStyle } from '../heatmap/newsMarkers'
import type { NewsMarker as NewsMarkerType } from '../heatmap/newsMarkers'
import { signalAt, signalColor } from '../heatmap/signalMarkers'
import { bucketPhaseMinutes } from '../heatmap/buckets'
import { gapBands } from '../heatmap/spacing'
import {
  DEFAULT_VIEW,
  axisZoneAt,
  anchoredOffsetX,
  baseBucketPx,
  compensateView,
  fitPriceView,
  homeOffsetX,
  viewYForPriceRange,
  visiblePriceRange,
  zoomAxis,
  zoomBoth,
} from '../heatmap/view'
import type { AxisZone, ViewTransform } from '../heatmap/view'
import { snapToTick, tickDecimals } from '../instrument/tick'
import {
  axisIndexFromMinute,
  minuteAxisOffsets,
  minuteFromAxisIndex,
  nearestAnnotationId,
} from '../annotations/model'
import type {
  ActiveTool,
  AnnotationPayload,
  AnnotationPoint,
  AnnotationTool,
  StoredAnnotation,
} from '../annotations/model'
import { useCrosshair } from '../state/Crosshair'

const UP_COLOR = '#3ecf8e'
const DOWN_COLOR = '#f0616d'
const LEVEL_DEFAULT_COLOR = '#e8c14b'
/** Svislý odstup, pod kterým se popisky úrovní považují za kolidující (#342). */
const LABEL_ROW_GAP_PX = 12

/** Tolerance úchopu okraje range (#484) v px — musí jít trefit i při hustší ose. */
const RANGE_EDGE_TOL_PX = 6

// measureText nutí layout — cache šířky per font|text (osy překreslujeme 5×/s při živém spotu)
// Strop (#509): klíče nesou ceny/časy, za dlouhý běh by mapa rostla donekonečna.
// Při přetečení se celá zahodí — znovu se naplní za pár snímků, LRU se nevyplatí.
const TEXT_WIDTH_CACHE_MAX = 4096
const textWidthCache = new Map<string, number>()
function measuredWidth(context: CanvasRenderingContext2D, text: string): number {
  const key = `${context.font}|${text}`
  let width = textWidthCache.get(key)
  if (width === undefined) {
    if (textWidthCache.size >= TEXT_WIDTH_CACHE_MAX) textWidthCache.clear()
    width = context.measureText(text).width
    textWidthCache.set(key, width)
  }
  return width
}
// Sentinel: pohled ještě nebyl fitnut (liší se od každého resetKey včetně undefined)
const UNFITTED = Symbol('unfitted')
// Osové labely crosshairu (TradingView styl): tmavý box, světlý text
const AXIS_LABEL_BG = '#363c4a'
const AXIS_LABEL_FG = '#e6e9ef'

export function Heatmap({
  grid,
  underGrid = null,
  underPalette,
  style,
  contours,
  overlays = {},
  liveBars = [],
  liveLabels = [],
  minuteLabels = [],
  priceStyle = 'line',
  priceOpacity = 1,
  annotations = [],
  annotationTool = null,
  annotationColor = '#e8c14b',
  bucketMinutes = 1,
  minutesIso,
  onAnnotationCreate,
  onAnnotationErase,
  onAnnotationMove,
  cellAbsolute,
  view: controlledView,
  initialZoomX = null,
  initialPriceRange = null,
  onUserZoomX,
  onUserYRange,
  onViewReset,
  onViewChange,
  fitRange = null,
  onLogicalSizeChange,
  dateLabel,
  resetKey,
  priceTick = 0.25,
  onNewsMarkerClick,
  onJournalMarkerClick,
  onJournalQuickAdd,
  range = null,
  onRangeDrag,
  onRangeCommit,
  rangeCreate = false,
  forwardMarkers = [],
}: {
  grid: HeatmapGrid
  /** Dyn GEX pole jako podklad (#242) — kreslí se POD měřeným gridem; průhledné
      buňky měřené vrstvy ho ukážou. Musí mít shodné rozměry (App to zaručuje). */
  underGrid?: HeatmapGrid | null
  /** Paleta podkladové plochy (#204): gamma zelená–červená, charm jantar–modrá, vanna teal–fialová. */
  underPalette?: SignedPalette
  style: HeatmapStyle
  contours: ContoursMode
  /** Hranice expirací Forward GEX (#572): svislice + popisek odpadlé gammy. */
  forwardMarkers?: ForwardBoundary[]
  overlays?: OverlayData
  /** Živé svíčky ze spot kanálu — kreslí se na dynamickou vrstvu (#141). */
  liveBars?: PriceBar[]
  /** Popisky minut za koncem gridu; index = `minuteIdx - grid.minutes`. */
  liveLabels?: string[]
  /** Popisky časové osy (HH:MM) per minuta — osa X dole. */
  minuteLabels?: string[]
  priceStyle?: PriceStyle
  /** Viditelnost cenové vrstvy nad heatmapou (0–1). */
  priceOpacity?: number
  annotations?: StoredAnnotation[]
  annotationTool?: ActiveTool
  annotationColor?: string
  /** Minut na jeden sloupec gridu (#430): anotace se ukládají v absolutních
      minutách dne a tady se převádí na index bucketu aktuálního TF.
      Daily pohled nechává 1 (jednotka = sloupec-den, beze změny chování). */
  bucketMinutes?: number
  /** ISO časy minut 1m osy dne (#502): osa může nést díry a backfill vkládá
      sloupce doprostřed — absolutní minuta anotace se na index osy převádí
      přes tuhle mapu, ne aritmetikou indexu. Bez ní platí identita
      (demo data, Daily pohled). */
  minutesIso?: string[]
  onAnnotationCreate?: (payload: AnnotationPayload) => void
  onAnnotationErase?: (id: number) => void
  /** Přesun anotace tažením v režimu Kurzor (#589); bez handleru se tažení
      chová jako dosud (pan plochy) a anotace se nezvýrazňují. */
  onAnnotationMove?: (id: number, payload: AnnotationPayload) => void
  /** Absolutní hodnoty buňky do tooltipu (#470) — normalizovaná čísla v gridu
      nesou barvu, ne velikost pozice. `null` = pro tu buňku nejsou. */
  cellAbsolute?: (bucketIdx: number, strike: number) => string | null
  /** Řízený pohled (pan/zoom os) — sdílení časové osy se spodními panely. */
  view?: ViewTransform
  onViewChange?: (view: ViewTransform) => void
  /** Persistovaný zoom X pro daný TF (#419); null = výchozí fit-to-width. */
  initialZoomX?: number | null
  /** Persistované viditelné cenové pásmo osy Y per instrument (#422);
      null = auto-fit na denní pásmo. */
  initialPriceRange?: { top: number; bottom: number } | null
  /** Uživatelská změna zoomu X (kolečko/tažení osy) — rodič ji persistuje (#419). */
  onUserZoomX?: (zoomX: number) => void
  /** Uživatelská změna pohledu Y (kolečko/pan/tažení osy) — hlásí se viditelné
      cenové pásmo k persistenci. JEN z gest: pohled a grid tu vždy patří
      k sobě, na rozdíl od přechodných renderů při přepnutí instrumentu (#426). */
  onUserYRange?: (range: { top: number; bottom: number }) => void
  /** Reset pohledu (dvojklik/⟲) — rodič smaže persistovaný zoom TF (#419). */
  onViewReset?: () => void
  /** Cenové pásmo dne pro auto-fit osy Y (výchozí pohled i cíl resetu). */
  fitRange?: { low: number; high: number } | null
  /** Hlášení logické velikosti (CSS px) — pravý profil sdílí Y měřítko. */
  onLogicalSizeChange?: (size: { width: number; height: number }) => void
  /** Datum grafu (intraday) — prefix časového labelu crosshairu na ose X. */
  dateLabel?: string
  /** Identita datasetu (symbol/expirace/timeframe/den) — auto-fit se provede jen při její změně. */
  resetKey?: string | number
  /** Min cenový tick instrumentu — crosshair cena na ose Y se na něj zaokrouhlí. */
  priceTick?: number
  /** Klik na news marker (pás/glyf u spodní hrany) — otevře dialog zpráv (#408). */
  onNewsMarkerClick?: (marker: NewsMarkerType) => void
  /** Klik na značku deníku (pás u horní hrany, #673) — otevře Deník. */
  onJournalMarkerClick?: (marker: JournalMarker) => void
  /** Shift+klik do plochy (#673): rychlý zápis do deníku k minutě pod kurzorem —
      myšlenka přijde většinou až s odstupem, ✎ u Replay nese jen aktuální minutu. */
  onJournalQuickAdd?: (minuteIdx: number) => void
  /** Range selector (#484): aktivní okno v koších osy; kreslí ztlumení + úchyty. */
  range?: { startBucket: number; endBucket: number } | null
  /** Živá změna range při tažení (vytvoření Alt+drag / nástrojem, úchyty okrajů,
      Alt+drag středu posouvá okno). Commit řeší rodič v onRangeCommit. */
  onRangeDrag?: (startBucket: number, endBucket: number) => void
  onRangeCommit?: () => void
  /** Nástroj „Rozsah" aktivní — obyčejné tažení vytváří range místo panu. */
  rangeCreate?: boolean
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const overlayRef = useRef<HTMLCanvasElement>(null)
  // Dynamická vrstva (#141): crosshair + živé svíčky — překresluje se 5×/s, ale je levná
  const dynamicRef = useRef<HTMLCanvasElement>(null)
  const offscreenRef = useRef<HTMLCanvasElement | null>(null)
  // Logická plocha = zobrazená velikost v CSS px; raster = × devicePixelRatio
  const { ref: stackRef, size } = useElementSize<HTMLDivElement>({ width: 1200, height: 640 })
  const dpr = typeof window !== 'undefined' ? (window.devicePixelRatio ?? 1) : 1
  const logicalW = size.width
  const logicalH = size.height
  const [internalView, setInternalView] = useState<ViewTransform>(DEFAULT_VIEW)
  // Řízený vs. vlastní pohled: rodič může sdílet transformaci se spodními panely
  const view = controlledView ?? internalView
  const setView = useCallback(
    (updater: (previous: ViewTransform) => ViewTransform) => {
      if (onViewChange) onViewChange(updater(controlledView ?? DEFAULT_VIEW))
      else setInternalView(updater)
    },
    [onViewChange, controlledView],
  )
  useEffect(() => {
    onLogicalSizeChange?.(size)
  }, [size, onLogicalSizeChange])
  // Změna velikosti plátna (předěly panelů, resize okna) nesmí hýbat obsahem:
  // base měřítko závisí na rozměrech, proto se zoom kompenzuje tak, aby
  // pixelové pozice buněk zůstaly — uživatel si graf srovná sám (#171).
  // useLayoutEffect: kompenzace se aplikuje PŘED paintem, jinak prohlížeč
  // stihne vykreslit snímek s novou velikostí a starým zoomem (#173)
  const previousSizeRef = useRef<{ width: number; height: number } | null>(null)
  useLayoutEffect(() => {
    const previous = previousSizeRef.current
    previousSizeRef.current = { width: logicalW, height: logicalH }
    if (!previous || (previous.width === logicalW && previous.height === logicalH)) return
    setView((current) =>
      compensateView(current, grid.minutes, previous, { width: logicalW, height: logicalH }),
    )
    // Jen na změnu velikosti — změna počtu minut kompenzaci spouštět nesmí
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logicalW, logicalH])
  // Výchozí pohled: fit cenového pásma na skutečnou výšku canvasu (hi-DPI, resize);
  // osa X ukotvená k pravému okraji, když data nevyplní šířku (TradingView styl)
  const homeView = useMemo(() => {
    const base = fitRange
      ? fitPriceView(grid.strikes, fitRange.low, fitRange.high, logicalH)
      : DEFAULT_VIEW
    return { ...base, offsetX: homeOffsetX(grid.minutes, logicalW) }
  }, [fitRange, grid.strikes, grid.minutes, logicalH, logicalW])
  // Persistovaný zoom TF (#419): poslední NAMĚŘENÁ svíčka ve 3/4 šířky —
  // vpravo zůstává čtvrtina na projekční zónu (ADR-0006)
  const initialView = useMemo(() => {
    // Osa Y: persistované cenové pásmo instrumentu (#422) má přednost před
    // auto-fitem — ale jen když se protíná s obálkou strikes. Pásmo mimo obálku
    // (otrávený zápis, velký overnight pohyb) by schovalo svíčky mimo plátno,
    // proto se ignoruje a platí auto-fit (#426).
    const lastStrike = grid.strikes[grid.strikes.length - 1]
    const rangeValid =
      initialPriceRange !== null &&
      grid.strikes.length > 1 &&
      initialPriceRange.top > grid.strikes[0] &&
      initialPriceRange.bottom < lastStrike
    const yPart =
      rangeValid && initialPriceRange
        ? viewYForPriceRange(grid.strikes, initialPriceRange.top, initialPriceRange.bottom, logicalH) // prettier-ignore
        : null
    const withY = yPart ? { ...homeView, ...yPart } : homeView
    if (initialZoomX === null) return withY
    const dataMinutes = grid.dataMinutes ?? grid.minutes
    return {
      ...withY,
      zoomX: initialZoomX,
      offsetX: anchoredOffsetX(grid.minutes, dataMinutes, logicalW, initialZoomX),
    }
  }, [homeView, initialZoomX, initialPriceRange, grid.strikes, grid.dataMinutes, grid.minutes, logicalH, logicalW]) // prettier-ignore
  // Auto-fit jen JEDNOU na dataset (resetKey = symbol/expirace/timeframe/den).
  // Resize pravého panelu, živý přírůstek minut ani úprava os pohled neresetují —
  // uživatelův pan/zoom tak zůstává zachovaný a X se neukotvuje samo doprava.
  const fittedKeyRef = useRef<string | number | undefined | symbol>(UNFITTED)
  // Poslední programově aplikovaný pohled — dokud se od něj uživatel neodchýlí
  // (gesto, resize kompenzace), je pohled v „auto režimu" a smí se sám dolaďovat
  const appliedViewRef = useRef<ViewTransform | null>(null)
  useEffect(() => {
    if (fittedKeyRef.current === resetKey) return
    if (!fitRange) return // počkej na reálná data (cenové pásmo dne)
    fittedKeyRef.current = resetKey
    appliedViewRef.current = initialView
    setView(() => initialView)
  }, [resetKey, fitRange, initialView, setView])
  // Auto režim (#423, #428): fit proběhne na PRVNÍ data po přepnutí datasetu,
  // jenže osa se pak prodlouží o projekci (offsetX by ujel mimo plátno) a
  // cenové pásmo se rozšíří o zbytek dne (úzký fit by nechal cenu mimo výřez).
  // Dokud uživatel do pohledu nezasáhne, pohled proto sleduje aktuální
  // initialView; první gesto ho zmrazí.
  useEffect(() => {
    if (fittedKeyRef.current !== resetKey) return
    const applied = appliedViewRef.current
    if (!applied) return
    const viewEquals = (a: ViewTransform, b: ViewTransform): boolean =>
      a.zoomX === b.zoomX && a.zoomY === b.zoomY && a.offsetX === b.offsetX && a.offsetY === b.offsetY // prettier-ignore
    if (!viewEquals(view, applied)) return // uživatel/resize převzal kontrolu
    if (viewEquals(initialView, applied)) return
    appliedViewRef.current = initialView
    setView(() => initialView)
  }, [initialView, view, resetKey, setView])
  // Tažení: pan plochy, nebo roztahování jedné osy (TradingView styl)
  const dragRef = useRef<{ x: number; y: number; mode: 'pan' | 'scale-x' | 'scale-y' } | null>(null)
  // Tažení range (#484): create drží kotvu (druhý okraj), move offset úchopu
  const rangeDragRef = useRef<{ mode: 'create' | 'move'; anchor: number } | null>(null)
  // Stav pro kurzor (ref nevyvolá re-render; při tažení se stejně renderuje
  // každou změnou range, ale hover mimo tažení potřebuje vlastní stav)
  const [rangeDragging, setRangeDragging] = useState(false)
  const [rangeHover, setRangeHover] = useState<'edge' | 'move' | null>(null)
  // Výchozí bod stisku — klik (bez tažení) na news marker otevře dialog (#408)
  const clickRef = useRef<{ x: number; y: number } | null>(null)
  const [axisHover, setAxisHover] = useState<AxisZone>(null)
  const [draft, setDraft] = useState<AnnotationPoint[] | null>(null)
  // Přesun anotace tažením (#589): rozpracovaná pozice se kreslí místo uložené,
  // uloží se až na pointerup. `origin` je datový bod stisku, `points` původní body.
  const [moving, setMoving] = useState<{ id: number; points: AnnotationPoint[] } | null>(null)
  const moveRef = useRef<{ id: number; origin: AnnotationPoint; points: AnnotationPoint[] } | null>(
    null,
  )
  // Anotace pod kurzorem v režimu Kurzor — zvýrazní se, ať je poznat, že tažení
  // pohne jí a ne grafem
  const [hoverAnnotationId, setHoverAnnotationId] = useState<number | null>(null)
  // Surová pozice kurzoru (CSS px) — osové labely crosshairu (cena na Y je spojitá)
  const [pointer, setPointer] = useState<{ x: number; y: number } | null>(null)
  const { position: crosshair, setPosition: setCrosshair } = useCrosshair()

  const strikeCount = grid.strikes.length

  const contourSegments = useMemo(() => {
    if (contours === 'off') return []
    // S Dyn GEX podkladem (#242) obrysují kontury modelované pole;
    // podklad má z App zaručené shodné rozměry s hlavním gridem
    const source =
      underGrid &&
      underGrid.minutes === grid.minutes &&
      underGrid.strikes.length === grid.strikes.length
        ? underGrid
        : grid
    const field = source.layers.signed ?? source.layers.call ?? source.layers.put
    if (!field) return []
    const smoothed = gaussianBlur(field, source.minutes, strikeCount)
    // Prahy per strana nad znaménkovým polem (#571); záporná strana jedním
    // algoritmem nad -field (#570) — u čistě kladných polí je sada prázdná
    const levels = contourLevels(smoothed, contours)
    const segments = levels.positive.flatMap((level) =>
      marchingSquares(smoothed, source.minutes, strikeCount, level),
    )
    if (levels.negative.length > 0) {
      const negated = Float32Array.from(smoothed, (value) => -value)
      for (const level of levels.negative) {
        segments.push(...marchingSquares(negated, source.minutes, strikeCount, level))
      }
    }
    return segments
  }, [grid, underGrid, contours, strikeCount])

  // Mapa 1m osy pro anotace (#502) — null = identita index == minuta
  const axisOffsets = useMemo(
    () => (minutesIso ? minuteAxisOffsets(minutesIso) : null),
    [minutesIso],
  )
  // Posun hranic košů proti začátku osy (#584): index koše × bucketMinutes − fáze = index 1m osy
  const bucketPhase = useMemo(
    () => bucketPhaseMinutes(minutesIso ?? [], bucketMinutes),
    [minutesIso, bucketMinutes],
  )

  /** Převod dat → obrazovka v logických CSS px (sdílený pro data i overlay canvas). */
  const mapping = useCallback(() => {
    const scaleX = baseBucketPx(grid.minutes, logicalW) * view.zoomX
    const scaleY = (logicalH / strikeCount) * view.zoomY
    return {
      scaleX,
      scaleY,
      minuteToX: (minuteIdx: number) => (minuteIdx + 0.5) * scaleX + view.offsetX,
      rowToY: (row: number) => (strikeCount - 1 - row + 0.5) * scaleY + view.offsetY,
      screenToCell: (x: number, y: number) => {
        const minuteIdx = Math.floor((x - view.offsetX) / scaleX)
        const rowFromTop = Math.floor((y - view.offsetY) / scaleY)
        const strikeIdx = strikeCount - 1 - rowFromTop
        return { minuteIdx, strikeIdx }
      },
      // Anotace: spojité datové souřadnice (čas × strike, ne pixely — SPEC 7.4)
      screenToDataPoint: (x: number, y: number): AnnotationPoint => {
        // Absolutní minuta dne (#430, #502): spojitý index bucketu → index 1m
        // osy → skutečná minuta přes mapu osy. Anotace tak drží pozici při
        // přepnutí timeframe i po backfillu minut doprostřed osy.
        // `bucketPhase` je posun hranic košů proti začátku osy (#584) — bez něj
        // by anotace po přepnutí TF ujely až o (bucketMinutes − 1) minut
        const axisIndex = ((x - view.offsetX) / scaleX - 0.5) * bucketMinutes - bucketPhase
        const minute = axisOffsets ? minuteFromAxisIndex(axisOffsets, axisIndex) : axisIndex
        const row = strikeCount - 1 - ((y - view.offsetY) / scaleY - 0.5)
        const clamped = Math.min(strikeCount - 1, Math.max(0, row))
        const lowIdx = Math.min(strikeCount - 2, Math.max(0, Math.floor(clamped)))
        const fraction = clamped - lowIdx
        const strike =
          strikeCount > 1
            ? grid.strikes[lowIdx] + fraction * (grid.strikes[lowIdx + 1] - grid.strikes[lowIdx])
            : (grid.strikes[0] ?? 0)
        return { minute, strike }
      },
    }
  }, [grid.minutes, grid.strikes, strikeCount, view, logicalW, logicalH, bucketMinutes, bucketPhase, axisOffsets]) // prettier-ignore

  // 1) Data → offscreen bitmapa (jen při změně dat/stylu). S Dyn GEX podkladem
  // (#242) se pole kreslí PRVNÍ a měřený grid přes něj — putImageData by podklad
  // přepsala, měřená vrstva proto jde přes drawImage (alfa kompozice).
  useEffect(() => {
    const buffer = renderGrid(grid, style)
    const offscreen = document.createElement('canvas')
    offscreen.width = buffer.width
    offscreen.height = buffer.height
    const context = offscreen.getContext('2d')
    if (!context) return // jsdom v testech
    const underMatches =
      underGrid !== null &&
      underGrid.minutes === grid.minutes &&
      underGrid.strikes.length === grid.strikes.length
    if (underMatches) {
      const underBuffer = renderGrid(underGrid, style, underPalette)
      context.putImageData(
        new ImageData(underBuffer.data, underBuffer.width, underBuffer.height),
        0,
        0,
      )
      const top = document.createElement('canvas')
      top.width = buffer.width
      top.height = buffer.height
      const topContext = top.getContext('2d')
      if (topContext) {
        topContext.putImageData(new ImageData(buffer.data, buffer.width, buffer.height), 0, 0)
        context.drawImage(top, 0, 0)
      }
    } else {
      context.putImageData(new ImageData(buffer.data, buffer.width, buffer.height), 0, 0)
    }
    offscreenRef.current = offscreen
    drawData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grid, underGrid, style, underPalette])

  // 2) Bitmapa → viditelný canvas (pan/zoom)
  const drawData = useCallback(() => {
    const canvas = canvasRef.current
    const offscreen = offscreenRef.current
    if (!canvas || !offscreen) return
    const context = canvas.getContext('2d')
    if (!context) return
    context.setTransform(1, 0, 0, 1, 0, 0)
    context.clearRect(0, 0, canvas.width, canvas.height)
    context.imageSmoothingEnabled = true // bilineární interpolace Gradient stylu
    const scaleX = baseBucketPx(offscreen.width, logicalW) * view.zoomX
    const scaleY = (logicalH / offscreen.height) * view.zoomY
    context.setTransform(dpr * scaleX, 0, 0, dpr * scaleY, dpr * view.offsetX, dpr * view.offsetY)
    context.drawImage(offscreen, 0, 0)
    // Děravá strike osa (#548): natažený bitmap by data roztáhl přes díru —
    // pásmo díry se vymaže (zůstane tmavé) a krajní buňky drží cap na medián
    // rozestupů; platí i pro Dyn GEX podklad (#242), sdílí tutéž osu
    context.setTransform(dpr, 0, 0, dpr, 0, 0)
    for (const band of gapBands(grid.strikes, scaleY, view.offsetY)) {
      context.clearRect(0, band.top, logicalW, band.bottom - band.top)
    }
    context.setTransform(1, 0, 0, 1, 0, 0)
  }, [view, logicalW, logicalH, dpr, grid.strikes])

  // 3) STATICKÁ overlay vrstva: kontury, uzavřené svíčky, sessions, levels/walls,
  // anotace, popisky os, timestamp. Překresluje se jen při změně dat/pohledu — NE
  // při spot ticku ani pohybu crosshairu (#141).
  const drawStatic = useCallback(() => {
    const canvas = overlayRef.current
    if (!canvas) return
    const context = canvas.getContext('2d')
    if (!context) return
    const { minuteToX, rowToY, scaleX, scaleY } = mapping()
    // Kreslení v logických CSS px; raster je dpr× větší → ostré popisky (hi-DPI)
    context.setTransform(dpr, 0, 0, dpr, 0, 0)
    context.clearRect(0, 0, logicalW, logicalH)

    // Díry v ose (#516): sloupce bez zaznamenaných dat — diagonální šrafura
    // (vzor oimissing #465), ať „souvislá" Daily řada neskrývá výpadek sběru
    if (grid.missingMinutes?.some(Boolean)) {
      context.save()
      context.strokeStyle = 'rgba(150,150,150,0.3)'
      context.lineWidth = 1
      for (let minuteIdx = 0; minuteIdx < grid.minutes; minuteIdx += 1) {
        if (!grid.missingMinutes[minuteIdx]) continue
        const x0 = minuteToX(minuteIdx) - 0.5 * scaleX
        context.save()
        context.beginPath()
        context.rect(x0, 0, scaleX, logicalH)
        context.clip()
        context.beginPath()
        for (let x = x0 - logicalH; x < x0 + scaleX; x += 7) {
          context.moveTo(x, logicalH)
          context.lineTo(x + logicalH, 0)
        }
        context.stroke()
        context.restore()
      }
      context.restore()
    }

    // Kontury (bílé přerušované, SPEC 7.2)
    if (contourSegments.length > 0) {
      context.strokeStyle = 'rgba(255,255,255,0.8)'
      context.setLineDash([4, 3])
      context.lineWidth = 1
      context.beginPath()
      for (const [x1, y1, x2, y2] of contourSegments) {
        context.moveTo(minuteToX(x1 - 0.5), rowToY(y1))
        context.lineTo(minuteToX(x2 - 0.5), rowToY(y2))
      }
      context.stroke()
      context.setLineDash([])
    }

    // Sessions markery (svislé čáry s popisky): všechny popisky zarovnané
    // v JEDNOM horním řádku; víc seancí na téže minutě se vypisuje pod sebe
    // (svislý sloupec u markeru), ne do dlouhého řádku „A · B · C" (#193)
    context.font = '11px sans-serif'
    for (const session of overlays.sessions ?? []) {
      const x = minuteToX(session.minuteIdx) - 0.5 * scaleX
      context.strokeStyle = 'rgba(125,133,150,0.6)'
      context.setLineDash([6, 4])
      context.beginPath()
      context.moveTo(x, 0)
      context.lineTo(x, logicalH)
      context.stroke()
      context.setLineDash([])
      context.fillStyle = 'rgba(125,133,150,0.9)'
      session.label.split(' · ').forEach((label, row) => {
        context.fillText(label, x + 4, 12 + row * 13)
      })
    }

    // Svislice expirací Forward GEX (#572): oranžové čárkované na hranici
    // dne PO odpadu expirace; OPEX (3. pátek) sytější a silnější. Popisek
    // nese podíl odpadlé gammy — bez čísla je čára jen dekorace.
    for (const marker of forwardMarkers) {
      const x = minuteToX(marker.minuteIdx) - 0.5 * scaleX
      context.strokeStyle = marker.isOpex ? 'rgba(255,140,20,0.95)' : 'rgba(240,160,60,0.65)'
      context.lineWidth = marker.isOpex ? 2 : 1
      context.setLineDash([5, 4])
      context.beginPath()
      context.moveTo(x, 0)
      context.lineTo(x, logicalH)
      context.stroke()
      context.setLineDash([])
      context.lineWidth = 1
      context.fillStyle = marker.isOpex ? 'rgba(255,140,20,0.95)' : 'rgba(240,160,60,0.9)'
      const expiryLabel = marker.expiries
        .map((expiry) => `${Number(expiry.slice(6, 8))}.${Number(expiry.slice(4, 6))}.`)
        .join('+')
      const shareLabel = marker.share !== null ? ` −${Math.round(marker.share * 100)} %` : ''
      context.fillText(
        `po exp ${expiryLabel}${shareLabel}${marker.isOpex ? ' (OPEX)' : ''}`,
        x + 4,
        26,
      )
    }

    // Markery zpráv (#287, SPEC 9.1): svislá značka v čase události, barva dle
    // sentimentu, jas a tloušťka dle důležitosti, glyf kategorie nad horní
    // hranou. Nadcházející scheduled eventy jsou duté — o dopadu se neví nic.
    for (const marker of overlays.newsMarkers ?? []) {
      const x = minuteToX(marker.minuteIdx) - 0.5 * scaleX
      const { alpha, width } = markerStyle(marker)
      context.strokeStyle = markerColor(marker, alpha)
      context.lineWidth = width
      if (marker.upcoming) context.setLineDash([4, 4])
      context.beginPath()
      // Značka nejde přes celou výšku jako sessions — nesmí konkurovat cenové
      // křivce; stačí pás u spodní hrany a glyf nahoře
      context.moveTo(x, logicalH * 0.72)
      context.lineTo(x, logicalH)
      context.stroke()
      context.setLineDash([])

      context.fillStyle = markerColor(marker, Math.min(1, alpha + 0.05))
      context.font = '11px sans-serif'
      context.fillText(marker.glyph, x - 4, logicalH * 0.72 - 4)
      if (marker.count > 1) {
        // Cluster: jeden marker s počtem místo změti čar (SPEC 9.1)
        context.font = '9px sans-serif'
        context.fillText(String(marker.count), x + 6, logicalH * 0.72 - 4)
      }
    }

    // Značky deníku (#673, Traders mode): pás u HORNÍ hrany, aby nekolidoval
    // s news markery dole; glyf ✎ pod čárkou, cluster s počtem
    for (const marker of overlays.journalMarkers ?? []) {
      const x = minuteToX(marker.minuteIdx) - 0.5 * scaleX
      context.strokeStyle = 'rgba(232,193,75,0.85)'
      context.lineWidth = 1.5
      context.beginPath()
      context.moveTo(x, 0)
      context.lineTo(x, logicalH * 0.1)
      context.stroke()
      context.fillStyle = 'rgba(232,193,75,0.95)'
      context.font = '11px sans-serif'
      context.fillText('✎', x - 4, logicalH * 0.1 + 12)
      if (marker.count > 1) {
        context.font = '9px sans-serif'
        context.fillText(String(marker.count), x + 6, logicalH * 0.1 + 12)
      }
    }

    // Předěl mezi naměřenými daty a projekcí (ADR-0006)
    const dataMinutes = grid.dataMinutes ?? grid.minutes
    if (dataMinutes < grid.minutes) {
      const x = minuteToX(dataMinutes) - 0.5 * scaleX
      context.strokeStyle = 'rgba(215,220,230,0.5)'
      context.lineWidth = 1
      context.setLineDash([3, 3])
      context.beginPath()
      context.moveTo(x, 0)
      context.lineTo(x, logicalH)
      context.stroke()
      context.setLineDash([])
      context.fillStyle = 'rgba(180,188,202,0.8)'
      context.fillText('projekce →', x + 5, logicalH - 26)
    }

    // Levels a walls linie (dle módu; barva per linie, volitelné čárkování).
    // Linie se slabými úseky (dominance zdi pod prahem, ADR-0010) se kreslí
    // dvěma průchody: plné úseky normálně, slabé ztlumeně a tečkovaně.
    const levelLines = [...(overlays.levels ?? []), ...(overlays.walls ?? [])]
    const strikeStep = strikeCount > 1 ? Math.abs(grid.strikes[1] - grid.strikes[0]) : 0
    const strokeLevelLine = (
      line: (typeof levelLines)[number],
      include: (minuteIdx: number) => boolean,
    ): void => {
      context.beginPath()
      let pen = false
      let lastValue: number | null = null
      line.series.forEach((value, minuteIdx) => {
        const row = value === null ? null : fractionalRow(grid.strikes, value)
        if (row === null || value === null || !include(minuteIdx)) {
          pen = false
          lastValue = null
          return
        }
        // Flip s více nulovými průchody přeskakuje — svislou spojnici přes
        // celý graf nahrazuje mezera (#197)
        if (pen && lastValue !== null && breaksOnJump(line.name)) {
          if (isLevelJump(lastValue, value, strikeStep)) pen = false
        }
        const x = minuteToX(minuteIdx)
        const y = rowToY(row)
        if (pen) context.lineTo(x, y)
        else context.moveTo(x, y)
        pen = true
        lastValue = value
      })
      context.stroke()
    }
    for (const line of levelLines) {
      context.strokeStyle = line.color || LEVEL_DEFAULT_COLOR
      context.lineWidth = 1.5
      const weak = line.weak
      if (line.dash) context.setLineDash(line.dash)
      if (weak === undefined) {
        strokeLevelLine(line, () => true)
      } else {
        strokeLevelLine(line, (minuteIdx) => weak[minuteIdx] !== true)
        context.globalAlpha = 0.4
        context.setLineDash([2, 3])
        strokeLevelLine(line, (minuteIdx) => weak[minuteIdx] === true)
        context.globalAlpha = 1
      }
      context.setLineDash([])
    }

    // Horizontální projekce úrovní přes celou šířku s popiskem.
    // Jen pojmenované úrovně (flip/walls/centroid/max pain) — počítané walls řady ne.
    // Popisek bez podkladového obdélníku: hodnota NAD čarou v barvě čáry.
    // Max Pain je plnou čarou s textem „Max Pain" vpravo před osou Y.
    context.font = 'bold 10px sans-serif'
    // Popisky blízkých úrovní by se překrývaly — každý další se odsune vpravo
    // za ten předchozí. S názvy (#342) jsou širší, takže bez toho splývají.
    const drawnLabels: { y: number; endX: number }[] = []
    for (const line of levelLines) {
      if (!hasLevelProjection(line.name)) continue
      const name = levelLabel(line.name)
      const value = lastLevelValue(line.series)
      const row = value === null ? null : fractionalRow(grid.strikes, value)
      if (value === null || row === null) continue
      const isMaxPain = line.name === 'max_pain'
      const y = rowToY(row)
      const color = line.color || LEVEL_DEFAULT_COLOR
      context.strokeStyle = color
      context.lineWidth = 1
      // Sekundární zeď si drží vlastní tečkování, ať jde poznat od primární
      if (!isMaxPain) context.setLineDash(line.dash ?? [6, 5])
      context.beginPath()
      context.moveTo(0, y)
      context.lineTo(logicalW, y)
      context.stroke()
      context.setLineDash([])
      // Popisek zdi nese i aktuální dominanci (ADR-0010, #223)
      const label =
        (name === null ? '' : `${name} `) + formatLevel(value) + (line.labelSuffix ?? '')
      const width = measuredWidth(context, label)
      context.fillStyle = color
      if (isMaxPain) {
        context.fillText(label, logicalW - width - 6, y - 4)
      } else {
        let x = 50
        for (const drawn of drawnLabels) {
          if (Math.abs(drawn.y - y) < LABEL_ROW_GAP_PX && drawn.endX + 8 > x) {
            x = drawn.endX + 8
          }
        }
        context.fillText(label, x, y - 4)
        drawnLabels.push({ y, endX: x + width })
      }
    }
    context.font = '11px sans-serif'

    // 1m cena: křivka s tick barvami, nebo svíčky (přepínač + viditelnost)
    const points = pricePolyline(overlays.price ?? [], grid.strikes)
    context.globalAlpha = Math.min(1, Math.max(0, priceOpacity))
    if (priceStyle === 'candles') {
      const candles = candleGeometry(overlays.price ?? [], grid.strikes)
      const bodyWidth = Math.max(2, scaleX * 0.6)
      // Knoty i těla dávkově po barvě (2 tahy místo N) — jinak N stroke()/snímek
      // dusí hlavní vlákno při živém spotu (překreslení 5×/s). SPEC 7.2 výkon.
      context.lineWidth = Math.max(1, scaleX * 0.1)
      for (const up of [true, false]) {
        const color = up ? UP_COLOR : DOWN_COLOR
        context.strokeStyle = color
        context.beginPath()
        for (const candle of candles) {
          if (candle.up !== up) continue
          const x = minuteToX(candle.minuteIdx)
          context.moveTo(x, rowToY(candle.highRow))
          context.lineTo(x, rowToY(candle.lowRow))
        }
        context.stroke()
        // Těla také jedním fill (rect path) místo N fillRect
        context.fillStyle = color
        context.beginPath()
        for (const candle of candles) {
          if (candle.up !== up) continue
          const x = minuteToX(candle.minuteIdx)
          const topY = rowToY(Math.max(candle.openRow, candle.closeRow))
          const bottomY = rowToY(Math.min(candle.openRow, candle.closeRow))
          context.rect(x - bodyWidth / 2, topY, bodyWidth, Math.max(1, bottomY - topY))
        }
        context.fill()
      }
    } else {
      // Segmenty dávkově po barvě ticku (2 tahy místo N)
      context.lineWidth = 1.5
      for (const up of [true, false]) {
        context.strokeStyle = up ? UP_COLOR : DOWN_COLOR
        context.beginPath()
        for (let index = 1; index < points.length; index += 1) {
          const current = points[index]
          if (current.up !== up) continue
          const previous = points[index - 1]
          context.moveTo(minuteToX(previous.minuteIdx), rowToY(previous.row))
          context.lineTo(minuteToX(current.minuteIdx), rowToY(current.row))
        }
        context.stroke()
      }
    }
    context.globalAlpha = 1

    // Šipky signálů na cenové křivce (#295, SPEC 9.0): ▲ teal long pod cenou /
    // ▼ červená short nad ní, sytost dle strength, decentní vodorovná stopa
    // do expiry_ts, ⚠ badge při nepotvrzené změně stavu (6.3).
    if (overlays.signals && overlays.signals.length > 0) {
      const bars = overlays.price ?? []
      const closeAtOrBefore = (minuteIdx: number): number | null => {
        let close: number | null = null
        for (const bar of bars) {
          if (bar.minuteIdx > minuteIdx) break
          close = bar.close
        }
        return close
      }
      for (const signal of overlays.signals) {
        const close = closeAtOrBefore(signal.minuteIdx)
        const row = close === null ? null : fractionalRow(grid.strikes, close)
        if (row === null) continue
        const x = minuteToX(signal.minuteIdx)
        const y = rowToY(row)
        const color = signalColor(signal)
        // Stopa platnosti — tenká, ztlumená, ať nekonkuruje ceně
        if (signal.endIdx > signal.minuteIdx) {
          context.strokeStyle = signalColor(signal, 0.35)
          context.lineWidth = 1
          context.beginPath()
          context.moveTo(x, y)
          context.lineTo(minuteToX(signal.endIdx), y)
          context.stroke()
        }
        // Trojúhelník: long míří nahoru a sedí POD cenou, short zrcadlově
        const size = 6
        const gap = 5
        context.fillStyle = color
        context.beginPath()
        if (signal.direction === 'long') {
          context.moveTo(x, y + gap)
          context.lineTo(x - size, y + gap + size * 1.5)
          context.lineTo(x + size, y + gap + size * 1.5)
        } else {
          context.moveTo(x, y - gap)
          context.lineTo(x - size, y - gap - size * 1.5)
          context.lineTo(x + size, y - gap - size * 1.5)
        }
        context.closePath()
        context.fill()
        if (signal.warning) {
          context.font = '10px sans-serif'
          const badgeY = signal.direction === 'long' ? y + gap + size * 1.5 + 11 : y - gap - size * 1.5 - 4 // prettier-ignore
          context.fillText('⚠', x + size + 2, badgeY)
          context.font = '11px sans-serif'
        }
      }
    }

    // Anotace (SPEC 7.4): kreslené v datových souřadnicích, škálují se s pan/zoom.
    // Absolutní minuta dne → index 1m osy přes mapu (#502) → index bucketu.
    const annotationX = (minute: number): number =>
      minuteToX(
        ((axisOffsets ? axisIndexFromMinute(axisOffsets, minute) : minute) + bucketPhase) /
          bucketMinutes,
      )
    const drawAnnotation = (
      tool: AnnotationTool,
      color: string,
      points: AnnotationPoint[],
      emphasized = false,
    ) => {
      if (points.length < 2) return
      context.strokeStyle = color
      // Zvýraznění pod kurzorem / při přesunu — silnější tah (#589)
      context.lineWidth = emphasized ? 4 : 2
      context.beginPath()
      points.forEach((point, index) => {
        const px = annotationX(point.minute)
        const py = rowToY(fractionalRow(grid.strikes, point.strike) ?? 0)
        if (index === 0) context.moveTo(px, py)
        else context.lineTo(px, py)
      })
      context.stroke()
      if (tool === 'arrow') {
        const from = points[0]
        const to = points[points.length - 1]
        const x1 = annotationX(from.minute)
        const y1 = rowToY(fractionalRow(grid.strikes, from.strike) ?? 0)
        const x2 = annotationX(to.minute)
        const y2 = rowToY(fractionalRow(grid.strikes, to.strike) ?? 0)
        const angle = Math.atan2(y2 - y1, x2 - x1)
        const head = 10
        context.beginPath()
        context.moveTo(x2, y2)
        context.lineTo(x2 - head * Math.cos(angle - 0.5), y2 - head * Math.sin(angle - 0.5))
        context.moveTo(x2, y2)
        context.lineTo(x2 - head * Math.cos(angle + 0.5), y2 - head * Math.sin(angle + 0.5))
        context.stroke()
      }
    }
    for (const annotation of annotations) {
      // Přesouvaná anotace se kreslí na rozpracované pozici, ne na uložené (#589)
      const dragged = moving?.id === annotation.id
      drawAnnotation(
        annotation.payload.tool,
        annotation.payload.color,
        dragged ? moving.points : annotation.payload.points,
        dragged || hoverAnnotationId === annotation.id,
      )
    }
    if (draft && annotationTool && annotationTool !== 'eraser') {
      drawAnnotation(annotationTool, annotationColor, draft)
    }

    // Popisky os (kreslené naposled, ať jsou nad daty)
    context.font = '11px sans-serif'
    // Osa Y: strikes u levého okraje
    for (const row of tickIndices(strikeCount, scaleY, 26)) {
      const y = rowToY(row)
      if (y < 8 || y > logicalH - 20) continue
      const label = String(grid.strikes[row])
      context.fillStyle = 'rgba(18,21,28,0.75)'
      context.fillRect(2, y - 8, measuredWidth(context, label) + 8, 15)
      context.fillStyle = 'rgba(180,188,202,0.95)'
      context.fillText(label, 6, y + 4)
    }
    // Osa X: čas u spodního okraje
    for (const minuteIdx of tickIndices(grid.minutes, scaleX, 88)) {
      const x = minuteToX(minuteIdx)
      if (x < 24 || x > logicalW - 44) continue
      const label = minuteLabels[minuteIdx] ?? `m${minuteIdx}`
      const width = measuredWidth(context, label)
      context.fillStyle = 'rgba(18,21,28,0.75)'
      context.fillRect(x - width / 2 - 4, logicalH - 19, width + 8, 15)
      context.fillStyle = 'rgba(180,188,202,0.95)'
      context.fillText(label, x - width / 2, logicalH - 7)
    }

    // Timestamp dat (SPEC 7.2)
    if (overlays.timestamp) {
      context.fillStyle = 'rgba(125,133,150,0.9)'
      context.font = '11px sans-serif'
      context.fillText(overlays.timestamp, logicalW - 150, logicalH - 26)
    }

    // Range selector (#484): okrajové linie s úchyty. Samotné ZTLUMENÍ mimo
    // okno kreslí dynamická vrstva (leží nad statickou i nad živými svíčkami —
    // jeden dim, nic se nesčítá; zpětná vazba: dim pod cenovou vrstvou vypadal
    // skoro nulově). Fallback bez WebGL (#490).
    if (range) {
      const startX = minuteToX(range.startBucket) - 0.5 * scaleX
      const endX = minuteToX(range.endBucket + 1) - 0.5 * scaleX
      context.strokeStyle = 'rgba(77,163,255,0.9)'
      context.lineWidth = 1.5
      for (const x of [startX, endX]) {
        context.beginPath()
        context.moveTo(x, 0)
        context.lineTo(x, logicalH)
        context.stroke()
        // Úchyt uprostřed výšky — tažením se okraj upravuje
        context.fillStyle = 'rgba(77,163,255,0.9)'
        context.fillRect(x - 3, logicalH / 2 - 12, 6, 24)
      }
    }
  }, [
    mapping,
    bucketMinutes,
    bucketPhase,
    axisOffsets,
    contourSegments,
    overlays,
    grid.strikes,
    grid.minutes,
    grid.missingMinutes,
    annotations,
    draft,
    moving,
    hoverAnnotationId,
    annotationTool,
    annotationColor,
    priceStyle,
    priceOpacity,
    minuteLabels,
    strikeCount,
    grid.minutes,
    grid.dataMinutes,
    logicalW,
    logicalH,
    dpr,
    forwardMarkers,
    range,
  ])

  // 4) DYNAMICKÁ overlay vrstva (#141): živé svíčky ze spotu, značka aktuální ceny
  // a crosshair. Jen pár čar a jedna svíčka — překreslení 5×/s hlavní vlákno neblokuje.
  const drawDynamic = useCallback(() => {
    const canvas = dynamicRef.current
    if (!canvas) return
    const context = canvas.getContext('2d')
    if (!context) return
    const { minuteToX, rowToY, scaleX, screenToDataPoint } = mapping()
    context.setTransform(dpr, 0, 0, dpr, 0, 0)
    context.clearRect(0, 0, logicalW, logicalH)

    // Živé svíčky: navazují na poslední uzavřenou (u křivky kvůli spojitosti úseku)
    const closedBars = overlays.price ?? []
    const lastClosed = closedBars.at(-1)
    context.globalAlpha = Math.min(1, Math.max(0, priceOpacity))
    if (liveBars.length > 0) {
      if (priceStyle === 'candles') {
        const bodyWidth = Math.max(2, scaleX * 0.6)
        context.lineWidth = Math.max(1, scaleX * 0.1)
        for (const candle of candleGeometry(liveBars, grid.strikes)) {
          const color = candle.up ? UP_COLOR : DOWN_COLOR
          const x = minuteToX(candle.minuteIdx)
          context.strokeStyle = color
          context.beginPath()
          context.moveTo(x, rowToY(candle.highRow))
          context.lineTo(x, rowToY(candle.lowRow))
          context.stroke()
          const topY = rowToY(Math.max(candle.openRow, candle.closeRow))
          const bottomY = rowToY(Math.min(candle.openRow, candle.closeRow))
          context.fillStyle = color
          context.fillRect(x - bodyWidth / 2, topY, bodyWidth, Math.max(1, bottomY - topY))
        }
      } else {
        const joined = lastClosed ? [lastClosed, ...liveBars] : liveBars
        const points = pricePolyline(joined, grid.strikes)
        context.lineWidth = 1.5
        for (let index = 1; index < points.length; index += 1) {
          const current = points[index]
          const previous = points[index - 1]
          context.strokeStyle = current.up ? UP_COLOR : DOWN_COLOR
          context.beginPath()
          context.moveTo(minuteToX(previous.minuteIdx), rowToY(previous.row))
          context.lineTo(minuteToX(current.minuteIdx), rowToY(current.row))
          context.stroke()
        }
      }
    }
    context.globalAlpha = 1 // značka aktuální ceny zůstává plně viditelná

    // Range (#484): ztlumit i živé svíčky téhle vrstvy mimo okno — statická
    // vrstva je pod nimi, bez tohohle by živá hrana zůstala plně svítit.
    // Crosshair a značka ceny se kreslí až po dimu (mají zůstat čitelné).
    if (range) {
      const startX = minuteToX(range.startBucket) - 0.5 * scaleX
      const endX = minuteToX(range.endBucket + 1) - 0.5 * scaleX
      context.fillStyle = 'rgba(8,10,15,0.72)'
      if (startX > 0) context.fillRect(0, 0, Math.min(startX, logicalW), logicalH)
      if (endX < logicalW) context.fillRect(Math.max(0, endX), 0, logicalW - endX, logicalH)
    }

    // Značka aktuální ceny: živý bar má přednost před poslední uzavřenou minutou
    const lastBar = liveBars.at(-1) ?? lastClosed
    const lastRow = lastBar ? fractionalRow(grid.strikes, lastBar.close) : null
    if (lastBar && lastRow !== null) {
      const y = rowToY(lastRow)
      const color = lastBar.up ? UP_COLOR : DOWN_COLOR
      context.strokeStyle = color
      context.lineWidth = 1
      context.setLineDash([2, 3])
      context.beginPath()
      context.moveTo(0, y)
      context.lineTo(logicalW, y)
      context.stroke()
      context.setLineDash([])
      context.fillStyle = color
      context.fillRect(logicalW - 56, y - 9, 56, 18)
      context.fillStyle = '#12151c'
      context.font = 'bold 11px sans-serif'
      context.fillText(lastBar.close.toFixed(2), logicalW - 52, y + 4)
    }

    // Crosshair synchronizovaný napříč panely (bez striku jen svislá čára)
    if (crosshair) {
      const x = minuteToX(crosshair.minuteIdx)
      context.strokeStyle = 'rgba(215,220,230,0.55)'
      context.lineWidth = 1
      // Svislá linka snapnutá na svíčku (bar)
      context.beginPath()
      context.moveTo(x, 0)
      context.lineTo(x, logicalH)
      context.stroke()
      // Vodorovná linka sleduje kurzor (spojitá cena) — jen při najetí na plochu grafu
      if (pointer) {
        context.beginPath()
        context.moveTo(0, pointer.y)
        context.lineTo(logicalW, pointer.y)
        context.stroke()
      }
      // Buňka pod kurzorem se NEobtahuje (#588): obrys velikosti koše × strike vypadal
      // jako prázdná svíčka a překrýval tu skutečnou. Co je pod kurzorem, říká tooltip.

      // Osové labely crosshairu (TradingView styl) — kreslené naposled, nad vším
      context.font = 'bold 11px sans-serif'
      // Osa X (dole): datum + čas pod svislou linkou (jen nad daty — mimo svíce bez času)
      const timeStr =
        minuteLabels[crosshair.minuteIdx] ?? liveLabels[crosshair.minuteIdx - grid.minutes]
      const timeLabel = timeStr ? `${dateLabel ? `${dateLabel} ` : ''}${timeStr}`.trim() : ''
      if (timeLabel) {
        const width = measuredWidth(context, timeLabel) + 12
        const boxX = Math.min(logicalW - width, Math.max(0, x - width / 2))
        context.fillStyle = AXIS_LABEL_BG
        context.fillRect(boxX, logicalH - 18, width, 16)
        context.fillStyle = AXIS_LABEL_FG
        context.fillText(timeLabel, boxX + 6, logicalH - 6)
      }
      // Osa Y (vpravo): cena na úrovni kurzoru, zaokrouhlená na tick instrumentu
      if (pointer) {
        const raw = screenToDataPoint(pointer.x, pointer.y).strike
        const priceLabel = snapToTick(raw, priceTick).toFixed(tickDecimals(priceTick))
        const width = measuredWidth(context, priceLabel) + 12
        const boxY = Math.min(logicalH - 8, Math.max(8, pointer.y))
        context.fillStyle = AXIS_LABEL_BG
        context.fillRect(logicalW - width, boxY - 8, width, 16)
        context.fillStyle = AXIS_LABEL_FG
        context.fillText(priceLabel, logicalW - width + 6, boxY + 4)
      }
    }
  }, [
    mapping,
    overlays.price,
    liveBars,
    liveLabels,
    grid.strikes,
    grid.minutes,
    crosshair,
    pointer,
    priceStyle,
    priceOpacity,
    minuteLabels,
    dateLabel,
    priceTick,
    range,
    logicalW,
    logicalH,
    dpr,
  ])

  // useLayoutEffect: změna width/height atributu canvas vyčistí — překreslení
  // musí proběhnout před paintem, jinak při tažení předělů problikává (#173)
  useLayoutEffect(() => {
    drawData()
  }, [drawData])

  useLayoutEffect(() => {
    drawStatic()
  }, [drawStatic])

  useLayoutEffect(() => {
    drawDynamic()
  }, [drawDynamic])

  /** Souřadnice události v logických CSS px (raster i mapping sdílí stejný prostor). */
  const canvasPoint = (event: {
    clientX: number
    clientY: number
  }): { x: number; y: number } | null => {
    const canvas = overlayRef.current
    if (!canvas) return null
    const rect = canvas.getBoundingClientRect()
    return { x: event.clientX - rect.left, y: event.clientY - rect.top }
  }

  // Uživatelské gesto měnící pohled: změny zoomX a pásma Y hlásí rodiči
  // k persistenci (#419, #426) — jediné místo, odkud se persistence plní
  const setGestureView = useCallback(
    (updater: (previous: ViewTransform) => ViewTransform) => {
      setView((previous) => {
        const next = updater(previous)
        if (next.zoomX !== previous.zoomX) onUserZoomX?.(next.zoomX)
        if (next.zoomY !== previous.zoomY || next.offsetY !== previous.offsetY) {
          const range = visiblePriceRange(grid.strikes, next, logicalH)
          if (range) onUserYRange?.(range)
        }
        return next
      })
    },
    [setView, onUserZoomX, onUserYRange, grid.strikes, logicalH],
  )

  // Kolečko: zoom ukotvený ke kurzoru; nad pruhem osy jen daná osa (TradingView styl)
  const onWheel = (event: React.WheelEvent<HTMLCanvasElement>) => {
    const point = canvasPoint(event)
    if (!point) return
    const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15
    const zone = axisZoneAt(point.x, point.y, logicalH)
    setGestureView((previous) =>
      zone === 'x'
        ? zoomAxis(previous, 'x', factor, point.x)
        : zone === 'y'
          ? zoomAxis(previous, 'y', factor, point.y)
          : zoomBoth(previous, factor, point.x, point.y),
    )
  }

  const resetView = () => {
    // Reset je programová změna pohledu — bez aktualizace applied ref by ho
    // auto-follow vyhodnotil jako uživatelské gesto a trvale se vypnul (#501)
    appliedViewRef.current = homeView
    setView(() => homeView)
    onViewReset?.()
  }

  const eventDataPoint = (event: React.PointerEvent<HTMLCanvasElement>): AnnotationPoint | null => {
    const point = canvasPoint(event)
    return point ? mapping().screenToDataPoint(point.x, point.y) : null
  }

  /** Anotace v dosahu datového bodu — společná tolerance gumy (#588) a přesunu (#589). */
  const annotationAt = (point: AnnotationPoint): number | null => {
    const strikeStep = strikeCount > 1 ? Math.abs(grid.strikes[1] - grid.strikes[0]) : 1
    // Tolerance: ~5 minut a 2 strike kroky
    return nearestAnnotationId(annotations, point, 5 * bucketMinutes, 2 * strikeStep)
  }

  const onPointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    if (annotationTool === 'eraser') {
      const point = eventDataPoint(event)
      if (point && onAnnotationErase) {
        const target = annotationAt(point)
        if (target !== null) onAnnotationErase(target)
      }
      return
    }
    // Režim Kurzor: tažení, které začne na anotaci, ji přesune (#589); jinde pan plochy
    if (!annotationTool && onAnnotationMove) {
      const point = eventDataPoint(event)
      const target = point ? annotationAt(point) : null
      const source = annotations.find((annotation) => annotation.id === target)
      if (point && source) {
        // `moving` se nastaví teprve prvním pohybem — klik bez tažení tak nevyvolá
        // zbytečný PUT s nezměněnou pozicí
        moveRef.current = { id: source.id, origin: point, points: source.payload.points }
        event.currentTarget.setPointerCapture(event.pointerId)
        return
      }
    }
    if (annotationTool) {
      const point = eventDataPoint(event)
      if (point) setDraft([point, point])
      event.currentTarget.setPointerCapture(event.pointerId)
      return
    }
    // Range selector (#484): existující okno má přednost — tažení za okraj
    // upravuje tu stranu, tažení uvnitř posouvá celé okno; obojí BEZ
    // modifikátorů a nezávisle na zvoleném nástroji (zpětná vazba uživatele:
    // s aktivním nástrojem Rozsah šlo jen kreslit nová okna). Nové okno
    // vytváří nástroj Rozsah nebo Alt+drag; Alt+drag uvnitř okna taky kreslí
    // nové (úniková cesta). Drag na ose (zoom X) má přednost — nekoliduje.
    if (onRangeDrag) {
      const point = canvasPoint(event)
      const zone = point ? axisZoneAt(point.x, point.y, logicalH) : null
      if (point && zone === null) {
        const { screenToCell, minuteToX, scaleX } = mapping()
        const { minuteIdx } = screenToCell(point.x, point.y)
        if (range && !event.altKey) {
          const startX = minuteToX(range.startBucket) - 0.5 * scaleX
          const endX = minuteToX(range.endBucket + 1) - 0.5 * scaleX
          if (Math.abs(point.x - startX) <= RANGE_EDGE_TOL_PX) {
            rangeDragRef.current = { mode: 'create', anchor: range.endBucket }
            setRangeDragging(true)
            event.currentTarget.setPointerCapture(event.pointerId)
            return
          }
          if (Math.abs(point.x - endX) <= RANGE_EDGE_TOL_PX) {
            rangeDragRef.current = { mode: 'create', anchor: range.startBucket }
            setRangeDragging(true)
            event.currentTarget.setPointerCapture(event.pointerId)
            return
          }
          if (point.x > startX && point.x < endX) {
            rangeDragRef.current = { mode: 'move', anchor: minuteIdx - range.startBucket }
            setRangeDragging(true)
            event.currentTarget.setPointerCapture(event.pointerId)
            return
          }
        }
        if (rangeCreate || event.altKey) {
          rangeDragRef.current = { mode: 'create', anchor: minuteIdx }
          setRangeDragging(true)
          onRangeDrag(minuteIdx, minuteIdx)
          event.currentTarget.setPointerCapture(event.pointerId)
          return
        }
      }
    }
    // Tažení za pruh osy = roztahování/stahování dané osy; jinde pan plochy
    const point = canvasPoint(event)
    const zone = point ? axisZoneAt(point.x, point.y, logicalH) : null
    clickRef.current = point
    dragRef.current = {
      x: event.clientX,
      y: event.clientY,
      mode: zone === 'x' ? 'scale-x' : zone === 'y' ? 'scale-y' : 'pan',
    }
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  const onPointerMove = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = overlayRef.current
    // Tažení range (#484) má přednost před panem/kreslením
    const rangeDrag = rangeDragRef.current
    if (rangeDrag && onRangeDrag) {
      const point = canvasPoint(event)
      if (point) {
        const { minuteIdx } = mapping().screenToCell(point.x, point.y)
        if (rangeDrag.mode === 'move' && range) {
          const width = range.endBucket - range.startBucket
          // Clamp na hranice naměřených dat — posun za okraj okno nezmenšuje
          const maxStart = Math.max(0, (grid.dataMinutes ?? grid.minutes) - 1 - width)
          const start = Math.max(0, Math.min(minuteIdx - rangeDrag.anchor, maxStart))
          onRangeDrag(start, start + width)
        } else {
          onRangeDrag(Math.min(rangeDrag.anchor, minuteIdx), Math.max(rangeDrag.anchor, minuteIdx))
        }
      }
      return
    }
    const dragging = moveRef.current
    if (dragging) {
      const point = eventDataPoint(event)
      if (point) {
        // Posun v DATOVÉM prostoru (minuta × strike), ne v pixelech — anotace tak
        // drží pozici i po přepnutí timeframe nebo backfillu minut (#502)
        const dMinute = point.minute - dragging.origin.minute
        const dStrike = point.strike - dragging.origin.strike
        setMoving({
          id: dragging.id,
          points: dragging.points.map((item) => ({
            minute: item.minute + dMinute,
            strike: item.strike + dStrike,
          })),
        })
      }
      return
    }
    if (draft && annotationTool && annotationTool !== 'eraser') {
      const point = eventDataPoint(event)
      if (point) {
        setDraft(
          (previous) =>
            annotationTool === 'freehand'
              ? [...(previous ?? []), point]
              : [previous?.[0] ?? point, point], // šipka/linie: start + aktuální konec
        )
      }
      return
    }
    if (dragRef.current) {
      const deltaX = event.clientX - dragRef.current.x
      const deltaY = event.clientY - dragRef.current.y
      const mode = dragRef.current.mode
      dragRef.current = { x: event.clientX, y: event.clientY, mode }
      if (mode === 'scale-x') {
        // Kotva = pravý okraj: poslední svíčka drží pozici. Doleva = roztáhnout,
        // doprava = zmenšit (jako osa Y nahoru = roztáhnout) — obrácené znaménko.
        const factor = Math.exp(-deltaX * 0.005)
        setGestureView((previous) => zoomAxis(previous, 'x', factor, logicalW))
      } else if (mode === 'scale-y') {
        const factor = Math.exp(-deltaY * 0.005)
        setGestureView((previous) => zoomAxis(previous, 'y', factor, logicalH / 2))
      } else {
        setGestureView((previous) => ({
          ...previous,
          offsetX: previous.offsetX + deltaX,
          offsetY: previous.offsetY + deltaY,
        }))
      }
      return
    }
    if (!canvas) return
    const point = canvasPoint(event)
    if (!point) return
    const { x, y } = point
    setAxisHover(axisZoneAt(x, y, logicalH))
    // Hover nad range (#484): okraj → ew-resize, vnitřek → grab
    if (range && onRangeDrag && axisZoneAt(x, y, logicalH) === null) {
      const { minuteToX: rangeMinuteToX, scaleX: rangeScaleX } = mapping()
      const startX = rangeMinuteToX(range.startBucket) - 0.5 * rangeScaleX
      const endX = rangeMinuteToX(range.endBucket + 1) - 0.5 * rangeScaleX
      const nextHover =
        Math.abs(x - startX) <= RANGE_EDGE_TOL_PX || Math.abs(x - endX) <= RANGE_EDGE_TOL_PX
          ? 'edge'
          : x > startX && x < endX
            ? 'move'
            : null
      setRangeHover((previous) => (previous === nextHover ? previous : nextHover))
    } else {
      setRangeHover((previous) => (previous === null ? previous : null))
    }
    const { minuteIdx, strikeIdx } = mapping().screenToCell(x, y)
    // Crosshair drží i mimo svíce (prázdná/budoucí plocha po posunu) — nesnapuje
    // se na neexistující bar; strike je null mimo cenové pásmo, minuta smí být mimo rozsah.
    const strike = strikeIdx >= 0 && strikeIdx < strikeCount ? grid.strikes[strikeIdx] : null
    setCrosshair({ minuteIdx, strike })
    setPointer({ x, y })
    // Anotace pod kurzorem (jen režim Kurzor s povoleným přesunem, #589)
    const hover =
      !annotationTool && onAnnotationMove ? annotationAt(mapping().screenToDataPoint(x, y)) : null
    setHoverAnnotationId((previous) => (previous === hover ? previous : hover))
  }

  const onPointerUp = (event: React.PointerEvent<HTMLCanvasElement>) => {
    if (rangeDragRef.current) {
      rangeDragRef.current = null
      setRangeDragging(false)
      onRangeCommit?.()
      return
    }
    const dragging = moveRef.current
    if (dragging) {
      moveRef.current = null
      const dropped = moving
      setMoving(null)
      const source = annotations.find((annotation) => annotation.id === dragging.id)
      // Klik bez tažení nic nepřesouvá (dropped === null) — jen vybere/odklikne
      if (dropped && source && onAnnotationMove) {
        onAnnotationMove(dragging.id, { ...source.payload, points: dropped.points })
      }
      return
    }
    if (draft && annotationTool && annotationTool !== 'eraser') {
      if (onAnnotationCreate && draft.length >= 2) {
        onAnnotationCreate({ tool: annotationTool, color: annotationColor, points: draft })
      }
      setDraft(null)
      return
    }
    dragRef.current = null
    // Klik (bez tažení) do pásu markerů u spodní hrany → dialog zpráv (#408).
    // Pan se pozná podle uražené vzdálenosti — kapture drží tentýž pointer.
    const start = clickRef.current
    clickRef.current = null
    if (!start) return
    const point = canvasPoint(event)
    if (!point || Math.hypot(point.x - start.x, point.y - start.y) > 4) return
    const { screenToCell, scaleX } = mapping()
    const { minuteIdx } = screenToCell(point.x, point.y)
    // Tolerance v minutách dle zoomu: glyf je ~8 px široký i při hustší ose
    const tolerance = Math.max(1, Math.ceil(6 / Math.max(scaleX, 0.01)))
    // Shift+klik (#673): rychlý zápis do deníku k minutě pod kurzorem
    if (event.shiftKey && onJournalQuickAdd) {
      onJournalQuickAdd(minuteIdx)
      return
    }
    // Značky deníku žijí u horní hrany (#673)
    if (point.y < logicalH * 0.25 && onJournalMarkerClick) {
      const journal = journalMarkerNear(overlays.journalMarkers ?? [], minuteIdx, tolerance)
      if (journal) {
        onJournalMarkerClick(journal)
        return
      }
    }
    if (!onNewsMarkerClick) return
    if (point.y < logicalH * 0.6) return // news markery žijí jen u spodní hrany
    const markers = overlays.newsMarkers ?? []
    if (markers.length === 0) return
    const marker = markerNear(markers, minuteIdx, tolerance)
    if (marker) onNewsMarkerClick(marker)
  }

  // Tooltip buňky (čas, strike, hodnoty metrik)
  const tooltip = useMemo(() => {
    if (!crosshair) return null
    if (crosshair.strike === null) return null
    // Mimo rozsah minut (prázdná/budoucí plocha) tooltip nemá data — jen crosshair
    if (crosshair.minuteIdx < 0 || crosshair.minuteIdx >= grid.minutes) return null
    const strikeIdx = grid.strikes.indexOf(crosshair.strike)
    if (strikeIdx < 0) return null
    // Díra v ose (#516): sloupec bez zaznamenaných dat — hodnoty by lhaly nulou
    if (grid.missingMinutes?.[crosshair.minuteIdx]) return 'bez zaznamenaných dat'
    const index = strikeIdx * grid.minutes + crosshair.minuteIdx
    const projected = crosshair.minuteIdx >= (grid.dataMinutes ?? grid.minutes)
    const parts: string[] = [
      projected ? 'projekce' : `min ${crosshair.minuteIdx}`,
      `strike ${crosshair.strike}`,
    ]
    if (grid.layers.call) parts.push(`call ${grid.layers.call[index].toFixed(2)}`)
    if (grid.layers.put) parts.push(`put ${grid.layers.put[index].toFixed(2)}`)
    if (grid.layers.signed) parts.push(`± ${grid.layers.signed[index].toFixed(2)}`)
    // Signál v minutě crosshairu: režim, zdůvodnění, n, Wilson LB (#295, SPEC 9.0)
    const signal = signalAt(overlays.signals ?? [], crosshair.minuteIdx)
    // Absolutní hodnoty VEDLE normalizovaných (#470): 0.52 je jen podíl vůči p99 dne
    // a o velikosti pozice neřekne nic. Normalizovaná se čte jako barva, absolutní
    // se rozhoduje. Projekční sloupce měřená data nemají.
    const absolute = projected ? null : cellAbsolute?.(crosshair.minuteIdx, crosshair.strike)
    const line = parts.join(' · ')
    return [line, absolute, signal?.tooltip].filter(Boolean).join('\n')
  }, [crosshair, grid, overlays.signals, cellAbsolute])

  return (
    <div className="heatmap-stack" ref={stackRef}>
      {/* Explicitní px rozměry (ne CSS 100 %): při tažení předělů se starý
      raster nesmí na mezisnímek roztáhnout — ořízne ho overflow stacku (#173) */}
      <canvas
        ref={canvasRef}
        className="heatmap-canvas"
        width={Math.round(logicalW * dpr)}
        height={Math.round(logicalH * dpr)}
        style={{ width: logicalW, height: logicalH }}
      />
      <canvas
        ref={overlayRef}
        className="heatmap-overlay"
        width={Math.round(logicalW * dpr)}
        height={Math.round(logicalH * dpr)}
        role="img"
        aria-label="GEX heatmapa"
        style={{
          width: logicalW,
          height: logicalH,
          cursor:
            axisHover === 'x'
              ? 'ew-resize'
              : axisHover === 'y'
                ? 'ns-resize'
                : // Range (#484): okraj se roztahuje, vnitřek posouvá celé okno
                  rangeDragging
                  ? 'grabbing'
                  : rangeHover === 'edge'
                    ? 'ew-resize'
                    : rangeHover === 'move'
                      ? 'grab'
                      : // Anotace pod kurzorem se dá uchopit a přesunout (#589)
                        hoverAnnotationId !== null
                        ? moving
                          ? 'grabbing'
                          : 'grab'
                        : undefined,
        }}
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={() => {
          setCrosshair(null)
          setAxisHover(null)
          setPointer(null)
        }}
        onDoubleClick={resetView}
      />
      <canvas
        ref={dynamicRef}
        className="heatmap-dynamic"
        width={Math.round(logicalW * dpr)}
        height={Math.round(logicalH * dpr)}
        style={{ width: logicalW, height: logicalH }}
        aria-hidden="true"
      />
      <button
        type="button"
        className="chip heatmap-reset"
        aria-label="Reset zobrazení"
        title="Reset zobrazení (nebo dvojklik do grafu)"
        onClick={resetView}
      >
        ⟲
      </button>
      {tooltip && (
        <div className="heatmap-tooltip" role="tooltip">
          {tooltip}
        </div>
      )}
    </div>
  )
}
