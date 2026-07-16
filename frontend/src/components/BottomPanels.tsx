/** Spodní panely Vol / Opt Vol / Cum Δ (SPEC 7.3).

Tři panely se sdílenou osou X (minuty dne, stejná osa jako heatmapa),
individuálně vypínatelné checkboxy v horní liště (AppState toggles → props).
Opt Vol barevně C/P, Cum Δ plocha nad/pod nulou. Crosshair je sdílený —
pohyb v panelu hýbe svislou linkou v heatmapě a naopak.
*/
import { barHeights, cumDeltaAreas } from '../panels/geometry'
import { useCrosshair } from '../state/Crosshair'

export interface PanelSeries {
  vol: number[]
  optVolCall: number[]
  optVolPut: number[]
  cumDelta: number[]
}

export interface PanelsVisible {
  vol: boolean
  optVol: boolean
  delta: boolean
}

const PANEL_HEIGHT = 84
const COLORS = {
  vol: '#7d8596',
  call: '#14b8a6',
  put: '#ef4444',
  positive: 'rgba(62, 207, 142, 0.55)',
  negative: 'rgba(240, 97, 109, 0.55)',
}

function usePanelPointer(minutes: number, width: number) {
  const { position, setPosition } = useCrosshair()
  const step = width / Math.max(1, minutes)
  const onPointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const cssScale = rect.width > 0 ? width / rect.width : 1
    const x = (event.clientX - rect.left) * cssScale
    const minuteIdx = Math.floor(x / step)
    if (minuteIdx >= 0 && minuteIdx < minutes) {
      // Panel zná jen časovou osu — strike z předchozí pozice zůstává
      setPosition({ minuteIdx, strike: position?.strike ?? null })
    }
  }
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
      data-testid="panel-crosshair"
    />
  )
}

export function BottomPanels({
  data,
  visible,
  width = 1200,
}: {
  data: PanelSeries
  visible: PanelsVisible
  width?: number
}) {
  const minutes = data.vol.length
  const pointer = usePanelPointer(minutes, width)
  const step = width / Math.max(1, minutes)
  const barWidth = Math.max(0.5, step * 0.8)

  const panels: React.ReactNode[] = []

  if (visible.vol) {
    const heights = barHeights(data.vol, PANEL_HEIGHT - 4)
    panels.push(
      <section key="vol" className="bottom-panel" aria-label="Vol panel">
        <span className="panel-title muted">Vol</span>
        <svg
          width={width}
          height={PANEL_HEIGHT}
          onPointerMove={pointer.onPointerMove}
          onPointerLeave={pointer.clear}
        >
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
          onPointerMove={pointer.onPointerMove}
          onPointerLeave={pointer.clear}
        >
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
          <polygon points={areas.positive} fill={COLORS.positive} data-part="cumdelta-positive" />
          <polygon points={areas.negative} fill={COLORS.negative} data-part="cumdelta-negative" />
          <CrosshairLine x={pointer.crosshairX} height={PANEL_HEIGHT} />
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
