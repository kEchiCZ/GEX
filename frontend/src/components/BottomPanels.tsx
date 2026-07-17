/** Spodní panely Vol / Opt Vol / Cum Δ (SPEC 7.3).

Tři panely se sdílenou osou X (minuty dne, stejná osa jako heatmapa),
individuálně vypínatelné checkboxy v horní liště (AppState toggles → props).
Opt Vol barevně C/P, Cum Δ plocha nad/pod nulou. Crosshair je sdílený —
pohyb v panelu hýbe svislou linkou v heatmapě a naopak.

Panely respektují pan/zoom osy X hlavního grafu (prop `time`): geometrie se
počítá v základním měřítku a transformuje <g>, takže crosshair i sloupce sedí
pod heatmapou pixel-přesně. SVG má viewBox + preserveAspectRatio="none" —
CSS roztažení škáluje obsah stejně jako canvas heatmapy.
*/
import { barHeights, cumDeltaAreas } from '../panels/geometry'
import { useCrosshair } from '../state/Crosshair'

export interface PanelSeries {
  vol: number[]
  optVolCall: number[]
  optVolPut: number[]
  cumDelta: number[]
  /** Delta-vážený opční tok per strana (|Δ| × přírůstek volume) — čtení C/P aktivity. */
  deltaFlowCall: number[]
  deltaFlowPut: number[]
}

export interface PanelsVisible {
  vol: boolean
  optVol: boolean
  delta: boolean
  deltaFlow: boolean
}

/** Časová část transformace hlavního grafu (sdílená osa X). */
export interface TimeTransform {
  offsetX: number
  zoomX: number
}

const IDENTITY_TIME: TimeTransform = { offsetX: 0, zoomX: 1 }

const PANEL_HEIGHT = 84
const COLORS = {
  vol: '#7d8596',
  call: '#14b8a6',
  put: '#ef4444',
  positive: 'rgba(62, 207, 142, 0.55)',
  negative: 'rgba(240, 97, 109, 0.55)',
}

function usePanelPointer(minutes: number, width: number, time: TimeTransform) {
  const { position, setPosition } = useCrosshair()
  const step = width / Math.max(1, minutes)
  const onPointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const cssScale = rect.width > 0 ? width / rect.width : 1
    const x = (event.clientX - rect.left) * cssScale
    // Inverzní transformace časové osy — stejné mapování jako heatmapa
    const baseX = (x - time.offsetX) / time.zoomX
    const minuteIdx = Math.floor(baseX / step)
    if (minuteIdx >= 0 && minuteIdx < minutes) {
      // Panel zná jen časovou osu — strike z předchozí pozice zůstává
      setPosition({ minuteIdx, strike: position?.strike ?? null })
    }
  }
  // Crosshair v základním měřítku — vykresluje se uvnitř transformované <g>
  const crosshairX = position === null ? null : (position.minuteIdx + 0.5) * step
  return { onPointerMove, crosshairX, clear: () => setPosition(null) }
}

function CrosshairLine({ x, height }: { x: number | null; height: number }) {
  if (x === null) return null
  return (
    <line
      x1={x}
      y1={0}
      x2={x}
      y2={height}
      stroke="rgba(215,220,230,0.55)"
      vectorEffect="non-scaling-stroke"
      data-testid="panel-crosshair"
    />
  )
}

export function BottomPanels({
  data,
  visible,
  width = 1200,
  time = IDENTITY_TIME,
}: {
  data: PanelSeries
  visible: PanelsVisible
  width?: number
  /** Pan/zoom osy X hlavního grafu — panely se roztahují synchronně. */
  time?: TimeTransform
}) {
  const minutes = data.vol.length
  const pointer = usePanelPointer(minutes, width, time)
  const step = width / Math.max(1, minutes)
  const barWidth = Math.max(0.5, step * 0.8)
  const transform = `translate(${time.offsetX} 0) scale(${time.zoomX} 1)`

  const panels: React.ReactNode[] = []

  if (visible.vol) {
    const heights = barHeights(data.vol, PANEL_HEIGHT - 4)
    panels.push(
      <section key="vol" className="bottom-panel" aria-label="Vol panel">
        <span className="panel-title muted">Vol</span>
        <svg
          width={width}
          height={PANEL_HEIGHT}
          viewBox={`0 0 ${width} ${PANEL_HEIGHT}`}
          preserveAspectRatio="none"
          onPointerMove={pointer.onPointerMove}
          onPointerLeave={pointer.clear}
        >
          <g transform={transform}>
            {heights.map((height, index) => (
              <rect
                key={index}
                x={(index + 0.5) * step - barWidth / 2}
                y={PANEL_HEIGHT - height}
                width={barWidth}
                height={height}
                fill={COLORS.vol}
              />
            ))}
            <CrosshairLine x={pointer.crosshairX} height={PANEL_HEIGHT} />
          </g>
        </svg>
      </section>,
    )
  }

  if (visible.optVol) {
    const callHeights = barHeights(data.optVolCall, PANEL_HEIGHT - 4)
    const putHeights = barHeights(data.optVolPut, PANEL_HEIGHT - 4)
    panels.push(
      <section key="optvol" className="bottom-panel" aria-label="Opt Vol panel">
        <span className="panel-title muted">Opt Vol</span>
        <svg
          width={width}
          height={PANEL_HEIGHT}
          viewBox={`0 0 ${width} ${PANEL_HEIGHT}`}
          preserveAspectRatio="none"
          onPointerMove={pointer.onPointerMove}
          onPointerLeave={pointer.clear}
        >
          <g transform={transform}>
            {callHeights.map((height, index) => (
              <rect
                key={`c${index}`}
                data-part="optvol-call"
                x={(index + 0.5) * step - barWidth / 2}
                y={PANEL_HEIGHT - height}
                width={barWidth / 2}
                height={height}
                fill={COLORS.call}
              />
            ))}
            {putHeights.map((height, index) => (
              <rect
                key={`p${index}`}
                data-part="optvol-put"
                x={(index + 0.5) * step}
                y={PANEL_HEIGHT - height}
                width={barWidth / 2}
                height={height}
                fill={COLORS.put}
              />
            ))}
            <CrosshairLine x={pointer.crosshairX} height={PANEL_HEIGHT} />
          </g>
        </svg>
      </section>,
    )
  }

  if (visible.deltaFlow) {
    const callHeights = barHeights(data.deltaFlowCall, PANEL_HEIGHT - 4)
    const putHeights = barHeights(data.deltaFlowPut, PANEL_HEIGHT - 4)
    panels.push(
      <section key="deltaflow" className="bottom-panel" aria-label="Δ Flow panel">
        <span className="panel-title muted">Δ Flow C/P</span>
        <svg
          width={width}
          height={PANEL_HEIGHT}
          viewBox={`0 0 ${width} ${PANEL_HEIGHT}`}
          preserveAspectRatio="none"
          onPointerMove={pointer.onPointerMove}
          onPointerLeave={pointer.clear}
        >
          <g transform={transform}>
            {callHeights.map((height, index) => (
              <rect
                key={`c${index}`}
                data-part="deltaflow-call"
                x={(index + 0.5) * step - barWidth / 2}
                y={PANEL_HEIGHT - height}
                width={barWidth / 2}
                height={height}
                fill={COLORS.call}
              />
            ))}
            {putHeights.map((height, index) => (
              <rect
                key={`p${index}`}
                data-part="deltaflow-put"
                x={(index + 0.5) * step}
                y={PANEL_HEIGHT - height}
                width={barWidth / 2}
                height={height}
                fill={COLORS.put}
              />
            ))}
            <CrosshairLine x={pointer.crosshairX} height={PANEL_HEIGHT} />
          </g>
        </svg>
      </section>,
    )
  }

  if (visible.delta) {
    const areas = cumDeltaAreas(data.cumDelta, width, PANEL_HEIGHT)
    panels.push(
      <section key="cumdelta" className="bottom-panel" aria-label="Cum Δ panel">
        <span className="panel-title muted">Cum Δ</span>
        <svg
          width={width}
          height={PANEL_HEIGHT}
          viewBox={`0 0 ${width} ${PANEL_HEIGHT}`}
          preserveAspectRatio="none"
          onPointerMove={pointer.onPointerMove}
          onPointerLeave={pointer.clear}
        >
          <line
            x1={0}
            y1={areas.zeroY}
            x2={width}
            y2={areas.zeroY}
            stroke="#2c3342"
            data-testid="cumdelta-zero"
          />
          <g transform={transform}>
            <polygon points={areas.positive} fill={COLORS.positive} data-part="cumdelta-positive" />
            <polygon points={areas.negative} fill={COLORS.negative} data-part="cumdelta-negative" />
            <CrosshairLine x={pointer.crosshairX} height={PANEL_HEIGHT} />
          </g>
        </svg>
      </section>,
    )
  }

  if (panels.length === 0) return null
  return (
    <div className="bottom-panels" aria-label="Spodní panely">
      {panels}
    </div>
  )
}
