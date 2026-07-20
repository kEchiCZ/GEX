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
import { memo, useState } from 'react'
import { baseBucketPx } from '../heatmap/view'
import { barHeights, cumDeltaAreas, seriesPeak } from '../panels/geometry'
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

const fmtInt = (value: number): string => Math.round(value).toLocaleString('cs-CZ')
const fmtSigned = (value: number): string =>
  (value > 0 ? '+' : '') + Math.round(value).toLocaleString('cs-CZ')

/** Hodnota ukazatele vpravo nahoře (HTML overlay — SVG by text roztáhl). */
function PanelValue({ children }: { children: React.ReactNode }) {
  return (
    <span className="panel-value" data-testid="panel-value">
      {children}
    </span>
  )
}

/** Hodnota na pravé ose Y dle výškové úrovně kurzoru (HTML overlay). */
function PanelAxisValue({ y, children }: { y: number; children: React.ReactNode }) {
  return (
    <span className="panel-axis-value" style={{ top: `${y}px` }} data-testid="panel-axis-value">
      {children}
    </span>
  )
}
const COLORS = {
  vol: '#7d8596',
  call: '#14b8a6',
  put: '#ef4444',
  positive: 'rgba(62, 207, 142, 0.55)',
  negative: 'rgba(240, 97, 109, 0.55)',
}

function usePanelPointer(minutes: number, width: number, time: TimeTransform) {
  const { position, setPosition } = useCrosshair()
  const step = baseBucketPx(minutes, width)
  const onPointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const cssScale = rect.width > 0 ? width / rect.width : 1
    const x = (event.clientX - rect.left) * cssScale
    // Inverzní transformace časové osy — stejné mapování jako heatmapa
    const baseX = (x - time.offsetX) / time.zoomX
    const minuteIdx = Math.floor(baseX / step)
    // Crosshair drží i mimo data (budoucí/prázdná plocha po posunu) — bez horní meze;
    // panel zná jen časovou osu, strike z předchozí pozice zůstává
    if (minuteIdx >= 0) {
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

function BottomPanelsBase({
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
  const { position } = useCrosshair()
  // Index pod crosshairem (sdílený napříč panely) — hodnoty vpravo nahoře
  const idx =
    position && position.minuteIdx >= 0 && position.minuteIdx < minutes ? position.minuteIdx : null
  // Výšková úroveň kurzoru v konkrétním panelu — hodnota na pravé ose Y
  const [hoverY, setHoverY] = useState<{ key: string; y: number } | null>(null)
  // Stejné základní měřítko jako heatmapa — málo dat se neroztahuje na šířku
  const step = baseBucketPx(minutes, width)
  const barWidth = Math.max(0.5, step * 0.8)
  const transform = `translate(${time.offsetX} 0) scale(${time.zoomX} 1)`

  // Vrcholy pro škály os Y; Opt Vol a Δ Flow sdílí škálu C/P (jednoznačná osa)
  const volPeak = seriesPeak(data.vol)
  const optPeak = Math.max(seriesPeak(data.optVolCall), seriesPeak(data.optVolPut))
  const flowPeak = Math.max(seriesPeak(data.deltaFlowCall), seriesPeak(data.deltaFlowPut))
  const cumPeak = seriesPeak(data.cumDelta)

  // Pohyb v panelu: crosshair (osa X) + výšková úroveň (osa Y) pro daný panel
  const handleMove = (key: string) => (event: React.PointerEvent<SVGSVGElement>) => {
    pointer.onPointerMove(event)
    const rect = event.currentTarget.getBoundingClientRect()
    const cssScale = rect.height > 0 ? PANEL_HEIGHT / rect.height : 1
    setHoverY({ key, y: (event.clientY - rect.top) * cssScale })
  }
  const handleLeave = () => {
    pointer.clear()
    setHoverY(null)
  }
  /** Hodnota na ose Y podle výšky kurzoru (signed = symetrická škála kolem nuly). */
  const axisValue = (key: string, peak: number, signed: boolean): React.ReactNode => {
    if (!hoverY || hoverY.key !== key) return null
    const y = Math.min(PANEL_HEIGHT, Math.max(0, hoverY.y))
    const value = signed
      ? ((PANEL_HEIGHT / 2 - y) / (PANEL_HEIGHT / 2)) * peak
      : ((PANEL_HEIGHT - y) / (PANEL_HEIGHT - 4)) * peak
    return <PanelAxisValue y={y}>{signed ? fmtSigned(value) : fmtInt(value)}</PanelAxisValue>
  }
  /** Vodorovná crosshair linka na úrovni kurzoru (jen v najetém panelu, mimo transform). */
  const axisLineH = (key: string): React.ReactNode => {
    if (!hoverY || hoverY.key !== key) return null
    const y = Math.min(PANEL_HEIGHT, Math.max(0, hoverY.y))
    return (
      <line
        x1={0}
        y1={y}
        x2={width}
        y2={y}
        stroke="rgba(215,220,230,0.55)"
        vectorEffect="non-scaling-stroke"
        data-testid="panel-crosshair-h"
      />
    )
  }

  const panels: React.ReactNode[] = []

  if (visible.vol) {
    const heights = barHeights(data.vol, PANEL_HEIGHT - 4, volPeak)
    panels.push(
      <section key="vol" className="bottom-panel" aria-label="Vol panel">
        <span className="panel-title muted">Vol</span>
        {idx !== null && <PanelValue>{fmtInt(data.vol[idx])}</PanelValue>}
        {axisValue('vol', volPeak, false)}
        <svg
          width={width}
          height={PANEL_HEIGHT}
          viewBox={`0 0 ${width} ${PANEL_HEIGHT}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('vol')}
          onPointerLeave={handleLeave}
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
          {axisLineH('vol')}
        </svg>
      </section>,
    )
  }

  if (visible.optVol) {
    const callHeights = barHeights(data.optVolCall, PANEL_HEIGHT - 4, optPeak)
    const putHeights = barHeights(data.optVolPut, PANEL_HEIGHT - 4, optPeak)
    panels.push(
      <section key="optvol" className="bottom-panel" aria-label="Opt Vol panel">
        <span className="panel-title muted">Opt Vol</span>
        {idx !== null && (
          <PanelValue>
            <span style={{ color: COLORS.call }}>C {fmtInt(data.optVolCall[idx])}</span>
            {' / '}
            <span style={{ color: COLORS.put }}>P {fmtInt(data.optVolPut[idx])}</span>
          </PanelValue>
        )}
        {axisValue('optvol', optPeak, false)}
        <svg
          width={width}
          height={PANEL_HEIGHT}
          viewBox={`0 0 ${width} ${PANEL_HEIGHT}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('optvol')}
          onPointerLeave={handleLeave}
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
          {axisLineH('optvol')}
        </svg>
      </section>,
    )
  }

  if (visible.deltaFlow) {
    const callHeights = barHeights(data.deltaFlowCall, PANEL_HEIGHT - 4, flowPeak)
    const putHeights = barHeights(data.deltaFlowPut, PANEL_HEIGHT - 4, flowPeak)
    panels.push(
      <section key="deltaflow" className="bottom-panel" aria-label="Δ Flow panel">
        <span className="panel-title muted">Δ Flow C/P</span>
        {idx !== null && (
          <PanelValue>
            <span style={{ color: COLORS.call }}>C {fmtInt(data.deltaFlowCall[idx])}</span>
            {' / '}
            <span style={{ color: COLORS.put }}>P {fmtInt(data.deltaFlowPut[idx])}</span>
          </PanelValue>
        )}
        {axisValue('deltaflow', flowPeak, false)}
        <svg
          width={width}
          height={PANEL_HEIGHT}
          viewBox={`0 0 ${width} ${PANEL_HEIGHT}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('deltaflow')}
          onPointerLeave={handleLeave}
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
          {axisLineH('deltaflow')}
        </svg>
      </section>,
    )
  }

  if (visible.delta) {
    const areas = cumDeltaAreas(data.cumDelta, minutes * step, PANEL_HEIGHT)
    panels.push(
      <section key="cumdelta" className="bottom-panel" aria-label="Cum Δ panel">
        <span className="panel-title muted">Cum Δ</span>
        {idx !== null && <PanelValue>{fmtSigned(data.cumDelta[idx])}</PanelValue>}
        {axisValue('cumdelta', cumPeak, true)}
        <svg
          width={width}
          height={PANEL_HEIGHT}
          viewBox={`0 0 ${width} ${PANEL_HEIGHT}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('cumdelta')}
          onPointerLeave={handleLeave}
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
          {axisLineH('cumdelta')}
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

// Memoizace: živý spot (rozdělaná svíčka) překresluje jen graf, ne tyto SVG panely
export const BottomPanels = memo(BottomPanelsBase)
