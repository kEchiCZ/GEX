/** Canvas heatmapa s overlayi (SPEC 7.2): Gradient/Blobs, contours, pan/zoom,
cenová křivka, sessions, levels/walls linie, crosshair + tooltip.

Data se překreslují do offscreen bitmapy jen při změně gridu/stylu; pan/zoom
i overlaye kreslí hotový bitmap + vektory nad ním — 60 fps drží GPU drawImage.
Crosshair je sdílený kontext (SPEC: synchronizace se spodními panely a profilem).

Rozlišení canvasu sleduje zobrazenou velikost × devicePixelRatio (hi-DPI):
kreslí se v logických CSS pixelech přes setTransform(dpr), takže popisky os
jsou ostré i na velkých monitorech. Souřadnice událostí = CSS pixely.
*/
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useElementSize } from '../hooks/useElementSize'
import { contourLevels, marchingSquares } from '../heatmap/contours'
import type { ContoursMode } from '../heatmap/contours'
import { gaussianBlur, renderGrid } from '../heatmap/render'
import type { HeatmapStyle } from '../heatmap/render'
import type { HeatmapGrid } from '../heatmap/grid'
import {
  candleGeometry,
  formatLevel,
  fractionalRow,
  lastLevelValue,
  pricePolyline,
  tickIndices,
} from '../heatmap/overlays'
import type { OverlayData, PriceStyle } from '../heatmap/overlays'
import {
  DEFAULT_VIEW,
  axisZoneAt,
  baseBucketPx,
  fitPriceView,
  homeOffsetX,
  zoomAxis,
  zoomBoth,
} from '../heatmap/view'
import type { AxisZone, ViewTransform } from '../heatmap/view'
import { nearestAnnotationId } from '../annotations/model'
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
// Sentinel: pohled ještě nebyl fitnut (liší se od každého resetKey včetně undefined)
const UNFITTED = Symbol('unfitted')
// Osové labely crosshairu (TradingView styl): tmavý box, světlý text
const AXIS_LABEL_BG = '#363c4a'
const AXIS_LABEL_FG = '#e6e9ef'

export function Heatmap({
  grid,
  style,
  contours,
  overlays = {},
  minuteLabels = [],
  priceStyle = 'line',
  priceOpacity = 1,
  annotations = [],
  annotationTool = null,
  annotationColor = '#e8c14b',
  onAnnotationCreate,
  onAnnotationErase,
  view: controlledView,
  onViewChange,
  fitRange = null,
  onLogicalSizeChange,
  dateLabel,
  resetKey,
}: {
  grid: HeatmapGrid
  style: HeatmapStyle
  contours: ContoursMode
  overlays?: OverlayData
  /** Popisky časové osy (HH:MM) per minuta — osa X dole. */
  minuteLabels?: string[]
  priceStyle?: PriceStyle
  /** Viditelnost cenové vrstvy nad heatmapou (0–1). */
  priceOpacity?: number
  annotations?: StoredAnnotation[]
  annotationTool?: ActiveTool
  annotationColor?: string
  onAnnotationCreate?: (payload: AnnotationPayload) => void
  onAnnotationErase?: (id: number) => void
  /** Řízený pohled (pan/zoom os) — sdílení časové osy se spodními panely. */
  view?: ViewTransform
  onViewChange?: (view: ViewTransform) => void
  /** Cenové pásmo dne pro auto-fit osy Y (výchozí pohled i cíl resetu). */
  fitRange?: { low: number; high: number } | null
  /** Hlášení logické velikosti (CSS px) — pravý profil sdílí Y měřítko. */
  onLogicalSizeChange?: (size: { width: number; height: number }) => void
  /** Datum grafu (intraday) — prefix časového labelu crosshairu na ose X. */
  dateLabel?: string
  /** Identita datasetu (symbol/expirace/timeframe/den) — auto-fit se provede jen při její změně. */
  resetKey?: string | number
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const overlayRef = useRef<HTMLCanvasElement>(null)
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
  // Výchozí pohled: fit cenového pásma na skutečnou výšku canvasu (hi-DPI, resize);
  // osa X ukotvená k pravému okraji, když data nevyplní šířku (TradingView styl)
  const homeView = useMemo(() => {
    const base = fitRange
      ? fitPriceView(grid.strikes, fitRange.low, fitRange.high, logicalH)
      : DEFAULT_VIEW
    return { ...base, offsetX: homeOffsetX(grid.minutes, logicalW) }
  }, [fitRange, grid.strikes, grid.minutes, logicalH, logicalW])
  // Auto-fit jen JEDNOU na dataset (resetKey = symbol/expirace/timeframe/den).
  // Resize pravého panelu, živý přírůstek minut ani úprava os pohled neresetují —
  // uživatelův pan/zoom tak zůstává zachovaný a X se neukotvuje samo doprava.
  const fittedKeyRef = useRef<string | number | undefined | symbol>(UNFITTED)
  useEffect(() => {
    if (fittedKeyRef.current === resetKey) return
    if (!fitRange) return // počkej na reálná data (cenové pásmo dne)
    fittedKeyRef.current = resetKey
    setView(() => homeView)
  }, [resetKey, fitRange, homeView, setView])
  // Tažení: pan plochy, nebo roztahování jedné osy (TradingView styl)
  const dragRef = useRef<{ x: number; y: number; mode: 'pan' | 'scale-x' | 'scale-y' } | null>(null)
  const [axisHover, setAxisHover] = useState<AxisZone>(null)
  const [draft, setDraft] = useState<AnnotationPoint[] | null>(null)
  // Surová pozice kurzoru (CSS px) — osové labely crosshairu (cena na Y je spojitá)
  const [pointer, setPointer] = useState<{ x: number; y: number } | null>(null)
  const { position: crosshair, setPosition: setCrosshair } = useCrosshair()

  const strikeCount = grid.strikes.length

  const contourSegments = useMemo(() => {
    if (contours === 'off') return []
    const field = grid.layers.signed ?? grid.layers.call ?? grid.layers.put
    if (!field) return []
    const smoothed = gaussianBlur(field, grid.minutes, strikeCount)
    const magnitudes = Float32Array.from(smoothed, Math.abs)
    return contourLevels(magnitudes, contours).flatMap((level) =>
      marchingSquares(magnitudes, grid.minutes, strikeCount, level),
    )
  }, [grid, contours, strikeCount])

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
        const minute = (x - view.offsetX) / scaleX - 0.5
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
  }, [grid.minutes, grid.strikes, strikeCount, view, logicalW, logicalH])

  // 1) Data → offscreen bitmapa (jen při změně dat/stylu)
  useEffect(() => {
    const buffer = renderGrid(grid, style)
    const offscreen = document.createElement('canvas')
    offscreen.width = buffer.width
    offscreen.height = buffer.height
    const context = offscreen.getContext('2d')
    if (!context) return // jsdom v testech
    context.putImageData(new ImageData(buffer.data, buffer.width, buffer.height), 0, 0)
    offscreenRef.current = offscreen
    drawData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grid, style])

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
    context.setTransform(1, 0, 0, 1, 0, 0)
  }, [view, logicalW, logicalH, dpr])

  // 3) Overlay canvas: kontury, cena, sessions, levels/walls, crosshair, timestamp
  const drawOverlay = useCallback(() => {
    const canvas = overlayRef.current
    if (!canvas) return
    const context = canvas.getContext('2d')
    if (!context) return
    const { minuteToX, rowToY, scaleX, scaleY, screenToDataPoint } = mapping()
    // Kreslení v logických CSS px; raster je dpr× větší → ostré popisky (hi-DPI)
    context.setTransform(dpr, 0, 0, dpr, 0, 0)
    context.clearRect(0, 0, logicalW, logicalH)

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

    // Sessions markery (svislé čáry s popisky)
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
      context.font = '11px sans-serif'
      context.fillText(session.label, x + 4, 12)
    }

    // Levels a walls linie (dle módu; barva per linie, volitelné čárkování)
    const levelLines = [...(overlays.levels ?? []), ...(overlays.walls ?? [])]
    for (const line of levelLines) {
      context.strokeStyle = line.color || LEVEL_DEFAULT_COLOR
      context.lineWidth = 1.5
      if (line.dash) context.setLineDash(line.dash)
      context.beginPath()
      let pen = false
      line.series.forEach((value, minuteIdx) => {
        const row = value === null ? null : fractionalRow(grid.strikes, value)
        if (row === null) {
          pen = false
          return
        }
        const x = minuteToX(minuteIdx)
        const y = rowToY(row)
        if (pen) context.lineTo(x, y)
        else context.moveTo(x, y)
        pen = true
      })
      context.stroke()
      context.setLineDash([])
    }

    // Horizontální projekce úrovní přes celou šířku s cenovkou (Moodix styl).
    // Jen pojmenované úrovně (flip/walls/centroid/max pain) — počítané walls řady ne.
    context.font = 'bold 10px sans-serif'
    for (const line of levelLines) {
      if (line.name.startsWith('walls:')) continue
      const value = lastLevelValue(line.series)
      const row = value === null ? null : fractionalRow(grid.strikes, value)
      if (value === null || row === null) continue
      const y = rowToY(row)
      context.strokeStyle = line.color || LEVEL_DEFAULT_COLOR
      context.lineWidth = 1
      context.setLineDash([6, 5])
      context.beginPath()
      context.moveTo(0, y)
      context.lineTo(logicalW, y)
      context.stroke()
      context.setLineDash([])
      const label = formatLevel(value)
      const width = context.measureText(label).width + 8
      context.fillStyle = line.color || LEVEL_DEFAULT_COLOR
      context.fillRect(46, y - 8, width, 15)
      context.fillStyle = '#12151c'
      context.fillText(label, 50, y + 4)
    }
    context.font = '11px sans-serif'

    // 1m cena: křivka s tick barvami, nebo svíčky (přepínač + viditelnost)
    const points = pricePolyline(overlays.price ?? [], grid.strikes)
    context.globalAlpha = Math.min(1, Math.max(0, priceOpacity))
    if (priceStyle === 'candles') {
      const candles = candleGeometry(overlays.price ?? [], grid.strikes)
      const bodyWidth = Math.max(2, scaleX * 0.6)
      for (const candle of candles) {
        const x = minuteToX(candle.minuteIdx)
        const color = candle.up ? UP_COLOR : DOWN_COLOR
        // Knot high–low
        context.strokeStyle = color
        context.lineWidth = Math.max(1, scaleX * 0.1)
        context.beginPath()
        context.moveTo(x, rowToY(candle.highRow))
        context.lineTo(x, rowToY(candle.lowRow))
        context.stroke()
        // Tělo open–close (rowToY klesá s rostoucím řádkem → top = vyšší řádek)
        const topY = rowToY(Math.max(candle.openRow, candle.closeRow))
        const bottomY = rowToY(Math.min(candle.openRow, candle.closeRow))
        context.fillStyle = color
        context.fillRect(x - bodyWidth / 2, topY, bodyWidth, Math.max(1, bottomY - topY))
      }
    } else {
      for (let index = 1; index < points.length; index += 1) {
        const previous = points[index - 1]
        const current = points[index]
        context.strokeStyle = current.up ? UP_COLOR : DOWN_COLOR
        context.lineWidth = 1.5
        context.beginPath()
        context.moveTo(minuteToX(previous.minuteIdx), rowToY(previous.row))
        context.lineTo(minuteToX(current.minuteIdx), rowToY(current.row))
        context.stroke()
      }
    }
    context.globalAlpha = 1 // značka aktuální ceny zůstává plně viditelná
    const lastPoint = points.at(-1)
    if (lastPoint) {
      const y = rowToY(lastPoint.row)
      context.strokeStyle = lastPoint.up ? UP_COLOR : DOWN_COLOR
      context.setLineDash([2, 3])
      context.beginPath()
      context.moveTo(0, y)
      context.lineTo(logicalW, y)
      context.stroke()
      context.setLineDash([])
      const price = overlays.price?.at(-1)?.close
      if (price !== undefined) {
        context.fillStyle = lastPoint.up ? UP_COLOR : DOWN_COLOR
        context.fillRect(logicalW - 56, y - 9, 56, 18)
        context.fillStyle = '#12151c'
        context.font = 'bold 11px sans-serif'
        context.fillText(price.toFixed(2), logicalW - 52, y + 4)
      }
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
      // Zvýraznění buňky pod kurzorem (jen nad daty — mimo svíce se nekreslí)
      const inRangeMinute = crosshair.minuteIdx >= 0 && crosshair.minuteIdx < grid.minutes
      const row = crosshair.strike === null ? -1 : grid.strikes.indexOf(crosshair.strike)
      if (inRangeMinute && row >= 0) {
        const y = rowToY(row)
        context.strokeStyle = 'rgba(215,220,230,0.9)'
        context.strokeRect(x - 0.5 * scaleX, y - 0.5 * scaleY, scaleX, scaleY)
      }
    }

    // Anotace (SPEC 7.4): kreslené v datových souřadnicích, škálují se s pan/zoom
    const drawAnnotation = (tool: AnnotationTool, color: string, points: AnnotationPoint[]) => {
      if (points.length < 2) return
      context.strokeStyle = color
      context.lineWidth = 2
      context.beginPath()
      points.forEach((point, index) => {
        const px = minuteToX(point.minute)
        const py = rowToY(fractionalRow(grid.strikes, point.strike) ?? 0)
        if (index === 0) context.moveTo(px, py)
        else context.lineTo(px, py)
      })
      context.stroke()
      if (tool === 'arrow') {
        const from = points[0]
        const to = points[points.length - 1]
        const x1 = minuteToX(from.minute)
        const y1 = rowToY(fractionalRow(grid.strikes, from.strike) ?? 0)
        const x2 = minuteToX(to.minute)
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
      drawAnnotation(annotation.payload.tool, annotation.payload.color, annotation.payload.points)
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
      context.fillRect(2, y - 8, context.measureText(label).width + 8, 15)
      context.fillStyle = 'rgba(180,188,202,0.95)'
      context.fillText(label, 6, y + 4)
    }
    // Osa X: čas u spodního okraje
    for (const minuteIdx of tickIndices(grid.minutes, scaleX, 88)) {
      const x = minuteToX(minuteIdx)
      if (x < 24 || x > logicalW - 44) continue
      const label = minuteLabels[minuteIdx] ?? `m${minuteIdx}`
      const width = context.measureText(label).width
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

    // Osové labely crosshairu (TradingView styl) — kreslené naposled, nad vším
    if (crosshair) {
      context.font = 'bold 11px sans-serif'
      // Osa X (dole): datum + čas pod svislou linkou (jen nad daty — mimo svíce bez času)
      const timeStr = minuteLabels[crosshair.minuteIdx]
      const timeLabel = timeStr ? `${dateLabel ? `${dateLabel} ` : ''}${timeStr}`.trim() : ''
      if (timeLabel) {
        const x = minuteToX(crosshair.minuteIdx)
        const width = context.measureText(timeLabel).width + 12
        const boxX = Math.min(logicalW - width, Math.max(0, x - width / 2))
        context.fillStyle = AXIS_LABEL_BG
        context.fillRect(boxX, logicalH - 18, width, 16)
        context.fillStyle = AXIS_LABEL_FG
        context.fillText(timeLabel, boxX + 6, logicalH - 6)
      }
      // Osa Y (vpravo): spojitá cena na úrovni kurzoru
      if (pointer) {
        const price = screenToDataPoint(pointer.x, pointer.y).strike
        const priceLabel = price.toFixed(2)
        const width = context.measureText(priceLabel).width + 12
        const boxY = Math.min(logicalH - 8, Math.max(8, pointer.y))
        context.fillStyle = AXIS_LABEL_BG
        context.fillRect(logicalW - width, boxY - 8, width, 16)
        context.fillStyle = AXIS_LABEL_FG
        context.fillText(priceLabel, logicalW - width + 6, boxY + 4)
      }
    }
  }, [
    mapping,
    contourSegments,
    overlays,
    grid.strikes,
    crosshair,
    annotations,
    draft,
    annotationTool,
    annotationColor,
    priceStyle,
    priceOpacity,
    minuteLabels,
    strikeCount,
    grid.minutes,
    logicalW,
    logicalH,
    dpr,
    pointer,
    dateLabel,
  ])

  useEffect(() => {
    drawData()
  }, [drawData])

  useEffect(() => {
    drawOverlay()
  }, [drawOverlay])

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

  // Kolečko: zoom ukotvený ke kurzoru; nad pruhem osy jen daná osa (TradingView styl)
  const onWheel = (event: React.WheelEvent<HTMLCanvasElement>) => {
    const point = canvasPoint(event)
    if (!point) return
    const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15
    const zone = axisZoneAt(point.x, point.y, logicalH)
    setView((previous) =>
      zone === 'x'
        ? zoomAxis(previous, 'x', factor, point.x)
        : zone === 'y'
          ? zoomAxis(previous, 'y', factor, point.y)
          : zoomBoth(previous, factor, point.x, point.y),
    )
  }

  const resetView = () => setView(() => homeView)

  const eventDataPoint = (event: React.PointerEvent<HTMLCanvasElement>): AnnotationPoint | null => {
    const point = canvasPoint(event)
    return point ? mapping().screenToDataPoint(point.x, point.y) : null
  }

  const onPointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    if (annotationTool === 'eraser') {
      const point = eventDataPoint(event)
      if (point && onAnnotationErase) {
        // Tolerance gumy: ~5 minut a 2 strike kroky
        const strikeStep = strikeCount > 1 ? Math.abs(grid.strikes[1] - grid.strikes[0]) : 1
        const target = nearestAnnotationId(annotations, point, 5, 2 * strikeStep)
        if (target !== null) onAnnotationErase(target)
      }
      return
    }
    if (annotationTool) {
      const point = eventDataPoint(event)
      if (point) setDraft([point, point])
      event.currentTarget.setPointerCapture(event.pointerId)
      return
    }
    // Tažení za pruh osy = roztahování/stahování dané osy; jinde pan plochy
    const point = canvasPoint(event)
    const zone = point ? axisZoneAt(point.x, point.y, logicalH) : null
    dragRef.current = {
      x: event.clientX,
      y: event.clientY,
      mode: zone === 'x' ? 'scale-x' : zone === 'y' ? 'scale-y' : 'pan',
    }
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  const onPointerMove = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = overlayRef.current
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
        // Kotva = pravý okraj: poslední svíčka drží pozici, historie se roztahuje
        const factor = Math.exp(deltaX * 0.005)
        setView((previous) => zoomAxis(previous, 'x', factor, logicalW))
      } else if (mode === 'scale-y') {
        const factor = Math.exp(-deltaY * 0.005)
        setView((previous) => zoomAxis(previous, 'y', factor, logicalH / 2))
      } else {
        setView((previous) => ({
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
    const { minuteIdx, strikeIdx } = mapping().screenToCell(x, y)
    // Crosshair drží i mimo svíce (prázdná/budoucí plocha po posunu) — nesnapuje
    // se na neexistující bar; strike je null mimo cenové pásmo, minuta smí být mimo rozsah.
    const strike = strikeIdx >= 0 && strikeIdx < strikeCount ? grid.strikes[strikeIdx] : null
    setCrosshair({ minuteIdx, strike })
    setPointer({ x, y })
  }

  const onPointerUp = () => {
    if (draft && annotationTool && annotationTool !== 'eraser') {
      if (onAnnotationCreate && draft.length >= 2) {
        onAnnotationCreate({ tool: annotationTool, color: annotationColor, points: draft })
      }
      setDraft(null)
      return
    }
    dragRef.current = null
  }

  // Tooltip buňky (čas, strike, hodnoty metrik)
  const tooltip = useMemo(() => {
    if (!crosshair) return null
    if (crosshair.strike === null) return null
    // Mimo rozsah minut (prázdná/budoucí plocha) tooltip nemá data — jen crosshair
    if (crosshair.minuteIdx < 0 || crosshair.minuteIdx >= grid.minutes) return null
    const strikeIdx = grid.strikes.indexOf(crosshair.strike)
    if (strikeIdx < 0) return null
    const index = strikeIdx * grid.minutes + crosshair.minuteIdx
    const parts: string[] = [`min ${crosshair.minuteIdx}`, `strike ${crosshair.strike}`]
    if (grid.layers.call) parts.push(`call ${grid.layers.call[index].toFixed(2)}`)
    if (grid.layers.put) parts.push(`put ${grid.layers.put[index].toFixed(2)}`)
    if (grid.layers.signed) parts.push(`± ${grid.layers.signed[index].toFixed(2)}`)
    return parts.join(' · ')
  }, [crosshair, grid])

  return (
    <div className="heatmap-stack" ref={stackRef}>
      <canvas
        ref={canvasRef}
        className="heatmap-canvas"
        width={Math.round(logicalW * dpr)}
        height={Math.round(logicalH * dpr)}
      />
      <canvas
        ref={overlayRef}
        className="heatmap-overlay"
        width={Math.round(logicalW * dpr)}
        height={Math.round(logicalH * dpr)}
        role="img"
        aria-label="GEX heatmapa"
        style={{
          cursor: axisHover === 'x' ? 'ew-resize' : axisHover === 'y' ? 'ns-resize' : undefined,
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
