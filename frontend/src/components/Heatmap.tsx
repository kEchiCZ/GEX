/** Canvas heatmapa (SPEC 7.2): Gradient/Blobs, contours, pan/zoom při 60 fps.

Data se překreslují do offscreen bitmapy jen při změně gridu/stylu; pan/zoom
je pouhé drawImage s transformací (GPU akcelerované) — proto drží 60 fps
i pro 180 × 1440. Bilineární interpolaci Gradient stylu dělá canvas
image smoothing při zvětšení bitmapy.
*/
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { contourLevels, marchingSquares } from '../heatmap/contours'
import type { ContoursMode } from '../heatmap/contours'
import { gaussianBlur, renderGrid } from '../heatmap/render'
import type { HeatmapStyle } from '../heatmap/render'
import type { HeatmapGrid } from '../heatmap/grid'

interface ViewTransform {
  offsetX: number
  offsetY: number
  zoom: number
}

export function Heatmap({
  grid,
  style,
  contours,
}: {
  grid: HeatmapGrid
  style: HeatmapStyle
  contours: ContoursMode
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const offscreenRef = useRef<HTMLCanvasElement | null>(null)
  const [view, setView] = useState<ViewTransform>({ offsetX: 0, offsetY: 0, zoom: 1 })
  const dragRef = useRef<{ x: number; y: number } | null>(null)

  // Kontury se počítají nad vyhlazeným polem dominantní vrstvy (SPEC 7.2)
  const contourSegments = useMemo(() => {
    if (contours === 'off') return []
    const field = grid.layers.signed ?? grid.layers.call ?? grid.layers.put
    if (!field) return []
    const smoothed = gaussianBlur(field, grid.minutes, grid.strikes.length)
    const magnitudes = Float32Array.from(smoothed, Math.abs)
    return contourLevels(magnitudes, contours).flatMap((level) =>
      marchingSquares(magnitudes, grid.minutes, grid.strikes.length, level),
    )
  }, [grid, contours])

  // 1) Data → offscreen bitmapa (jen při změně dat/stylu)
  useEffect(() => {
    const buffer = renderGrid(grid, style)
    const offscreen = document.createElement('canvas')
    offscreen.width = buffer.width
    offscreen.height = buffer.height
    const context = offscreen.getContext('2d')
    if (!context) return // jsdom v testech — kreslení přeskočíme
    context.putImageData(new ImageData(buffer.data, buffer.width, buffer.height), 0, 0)
    offscreenRef.current = offscreen
    draw()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grid, style])

  // 2) Bitmapa → viditelný canvas s pan/zoom transformací (každý pohyb)
  const draw = useCallback(() => {
    const canvas = canvasRef.current
    const offscreen = offscreenRef.current
    if (!canvas || !offscreen) return
    const context = canvas.getContext('2d')
    if (!context) return
    const { width, height } = canvas
    context.clearRect(0, 0, width, height)
    context.imageSmoothingEnabled = true // bilineární interpolace Gradient stylu
    const scaleX = (width / offscreen.width) * view.zoom
    const scaleY = (height / offscreen.height) * view.zoom
    context.setTransform(scaleX, 0, 0, scaleY, view.offsetX, view.offsetY)
    context.drawImage(offscreen, 0, 0)

    if (contourSegments.length > 0) {
      // Souřadnice segmentů jsou v buňkách vzestupně podle striku → převrátit osu Y
      context.strokeStyle = 'rgba(255, 255, 255, 0.8)'
      context.setLineDash([4 / scaleX, 3 / scaleX])
      context.lineWidth = 1 / scaleX
      const flipY = (y: number) => offscreen.height - 1 - y
      context.beginPath()
      for (const [x1, y1, x2, y2] of contourSegments) {
        context.moveTo(x1, flipY(y1))
        context.lineTo(x2, flipY(y2))
      }
      context.stroke()
      context.setLineDash([])
    }
    context.setTransform(1, 0, 0, 1, 0, 0)
  }, [view, contourSegments])

  useEffect(() => {
    draw()
  }, [draw])

  const onWheel = (event: React.WheelEvent<HTMLCanvasElement>) => {
    const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15
    setView((previous) => ({
      ...previous,
      zoom: Math.min(16, Math.max(1, previous.zoom * factor)),
    }))
  }

  const onPointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    dragRef.current = { x: event.clientX, y: event.clientY }
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  const onPointerMove = (event: React.PointerEvent<HTMLCanvasElement>) => {
    if (!dragRef.current) return
    const deltaX = event.clientX - dragRef.current.x
    const deltaY = event.clientY - dragRef.current.y
    dragRef.current = { x: event.clientX, y: event.clientY }
    setView((previous) => ({
      ...previous,
      offsetX: previous.offsetX + deltaX,
      offsetY: previous.offsetY + deltaY,
    }))
  }

  const onPointerUp = () => {
    dragRef.current = null
  }

  return (
    <canvas
      ref={canvasRef}
      className="heatmap-canvas"
      width={1200}
      height={640}
      role="img"
      aria-label="GEX heatmapa"
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
    />
  )
}
