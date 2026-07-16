/** Canvas heatmapa s overlayi (SPEC 7.2): Gradient/Blobs, contours, pan/zoom,
cenová křivka, sessions, levels/walls linie, crosshair + tooltip.

Data se překreslují do offscreen bitmapy jen při změně gridu/stylu; pan/zoom
i overlaye kreslí hotový bitmap + vektory nad ním — 60 fps drží GPU drawImage.
Crosshair je sdílený kontext (SPEC: synchronizace se spodními panely a profilem).
*/
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { contourLevels, marchingSquares } from '../heatmap/contours'
import type { ContoursMode } from '../heatmap/contours'
import { gaussianBlur, renderGrid } from '../heatmap/render'
import type { HeatmapStyle } from '../heatmap/render'
import type { HeatmapGrid } from '../heatmap/grid'
import { candleGeometry, fractionalRow, pricePolyline } from '../heatmap/overlays'
import type { OverlayData, PriceStyle } from '../heatmap/overlays'
import { nearestAnnotationId } from '../annotations/model'
import type {
  ActiveTool,
  AnnotationPayload,
  AnnotationPoint,
  AnnotationTool,
  StoredAnnotation,
} from '../annotations/model'
import { useCrosshair } from '../state/Crosshair'

interface ViewTransform {
  offsetX: number
  offsetY: number
  zoom: number
}

const UP_COLOR = '#3ecf8e'
const DOWN_COLOR = '#f0616d'
const LEVEL_DEFAULT_COLOR = '#e8c14b'

export function Heatmap({
  grid,
  style,
  contours,
  overlays = {},
  priceStyle = 'line',
  priceOpacity = 1,
  annotations = [],
  annotationTool = null,
  annotationColor = '#e8c14b',
  onAnnotationCreate,
  onAnnotationErase,
}: {
  grid: HeatmapGrid
  style: HeatmapStyle
  contours: ContoursMode
  overlays?: OverlayData
  priceStyle?: PriceStyle
  /** Viditelnost cenové vrstvy nad heatmapou (0–1). */
  priceOpacity?: number
  annotations?: StoredAnnotation[]
  annotationTool?: ActiveTool
  annotationColor?: string
  onAnnotationCreate?: (payload: AnnotationPayload) => void
  onAnnotationErase?: (id: number) => void
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const overlayRef = useRef<HTMLCanvasElement>(null)
  const offscreenRef = useRef<HTMLCanvasElement | null>(null)
  const [view, setView] = useState<ViewTransform>({ offsetX: 0, offsetY: 0, zoom: 1 })
  const dragRef = useRef<{ x: number; y: number } | null>(null)
  const [draft, setDraft] = useState<AnnotationPoint[] | null>(null)
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

  /** Převod dat → obrazovka (sdílený pro data i overlay canvas). */
  const mapping = useCallback(
    (canvas: HTMLCanvasElement) => {
      const scaleX = (canvas.width / grid.minutes) * view.zoom
      const scaleY = (canvas.height / strikeCount) * view.zoom
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
    },
    [grid.minutes, grid.strikes, strikeCount, view],
  )

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
    context.clearRect(0, 0, canvas.width, canvas.height)
    context.imageSmoothingEnabled = true // bilineární interpolace Gradient stylu
    const scaleX = (canvas.width / offscreen.width) * view.zoom
    const scaleY = (canvas.height / offscreen.height) * view.zoom
    context.setTransform(scaleX, 0, 0, scaleY, view.offsetX, view.offsetY)
    context.drawImage(offscreen, 0, 0)
    context.setTransform(1, 0, 0, 1, 0, 0)
  }, [view])

  // 3) Overlay canvas: kontury, cena, sessions, levels/walls, crosshair, timestamp
  const drawOverlay = useCallback(() => {
    const canvas = overlayRef.current
    if (!canvas) return
    const context = canvas.getContext('2d')
    if (!context) return
    const { minuteToX, rowToY, scaleX, scaleY } = mapping(canvas)
    context.clearRect(0, 0, canvas.width, canvas.height)

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
      context.lineTo(x, canvas.height)
      context.stroke()
      context.setLineDash([])
      context.fillStyle = 'rgba(125,133,150,0.9)'
      context.font = '11px sans-serif'
      context.fillText(session.label, x + 4, 12)
    }

    // Levels a walls linie (dle módu; barva per linie)
    for (const line of [...(overlays.levels ?? []), ...(overlays.walls ?? [])]) {
      context.strokeStyle = line.color || LEVEL_DEFAULT_COLOR
      context.lineWidth = 1.5
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
    }

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
      context.lineTo(canvas.width, y)
      context.stroke()
      context.setLineDash([])
      const price = overlays.price?.at(-1)?.close
      if (price !== undefined) {
        context.fillStyle = lastPoint.up ? UP_COLOR : DOWN_COLOR
        context.fillRect(canvas.width - 56, y - 9, 56, 18)
        context.fillStyle = '#12151c'
        context.font = 'bold 11px sans-serif'
        context.fillText(price.toFixed(2), canvas.width - 52, y + 4)
      }
    }

    // Crosshair synchronizovaný napříč panely (bez striku jen svislá čára)
    if (crosshair) {
      const x = minuteToX(crosshair.minuteIdx)
      context.strokeStyle = 'rgba(215,220,230,0.55)'
      context.lineWidth = 1
      context.beginPath()
      context.moveTo(x, 0)
      context.lineTo(x, canvas.height)
      const row = crosshair.strike === null ? -1 : grid.strikes.indexOf(crosshair.strike)
      if (row >= 0) {
        const y = rowToY(row)
        context.moveTo(0, y)
        context.lineTo(canvas.width, y)
        context.stroke()
        context.strokeStyle = 'rgba(215,220,230,0.9)'
        context.strokeRect(x - 0.5 * scaleX, y - 0.5 * scaleY, scaleX, scaleY)
      } else {
        context.stroke()
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

    // Timestamp dat (SPEC 7.2)
    if (overlays.timestamp) {
      context.fillStyle = 'rgba(125,133,150,0.9)'
      context.font = '11px sans-serif'
      context.fillText(overlays.timestamp, canvas.width - 150, canvas.height - 8)
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
  ])

  useEffect(() => {
    drawData()
  }, [drawData])

  useEffect(() => {
    drawOverlay()
  }, [drawOverlay])

  const onWheel = (event: React.WheelEvent<HTMLCanvasElement>) => {
    const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15
    setView((previous) => ({
      ...previous,
      zoom: Math.min(16, Math.max(1, previous.zoom * factor)),
    }))
  }

  const eventDataPoint = (event: React.PointerEvent<HTMLCanvasElement>): AnnotationPoint | null => {
    const canvas = overlayRef.current
    if (!canvas) return null
    const rect = canvas.getBoundingClientRect()
    const cssScale = rect.width > 0 ? canvas.width / rect.width : 1
    const x = (event.clientX - rect.left) * cssScale
    const y = (event.clientY - rect.top) * cssScale
    return mapping(canvas).screenToDataPoint(x, y)
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
    dragRef.current = { x: event.clientX, y: event.clientY }
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
      dragRef.current = { x: event.clientX, y: event.clientY }
      setView((previous) => ({
        ...previous,
        offsetX: previous.offsetX + deltaX,
        offsetY: previous.offsetY + deltaY,
      }))
      return
    }
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const cssScale = rect.width > 0 ? canvas.width / rect.width : 1
    const x = (event.clientX - rect.left) * cssScale
    const y = (event.clientY - rect.top) * cssScale
    const { minuteIdx, strikeIdx } = mapping(canvas).screenToCell(x, y)
    if (minuteIdx >= 0 && minuteIdx < grid.minutes && strikeIdx >= 0 && strikeIdx < strikeCount) {
      setCrosshair({ minuteIdx, strike: grid.strikes[strikeIdx] })
    } else {
      setCrosshair(null)
    }
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
    <div className="heatmap-stack">
      <canvas ref={canvasRef} className="heatmap-canvas" width={1200} height={640} />
      <canvas
        ref={overlayRef}
        className="heatmap-overlay"
        width={1200}
        height={640}
        role="img"
        aria-label="GEX heatmapa"
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={() => setCrosshair(null)}
      />
      {tooltip && (
        <div className="heatmap-tooltip" role="tooltip">
          {tooltip}
        </div>
      )}
    </div>
  )
}
