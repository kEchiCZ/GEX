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
import {
  CUM_DELTA_PAD,
  barHeights,
  cumDeltaAreas,
  sentimentCandleGeometry,
  seriesPeak,
} from '../panels/geometry'
import type { SentimentCandle } from '../panels/geometry'
import { useCrosshair } from '../state/Crosshair'

export interface PanelSeries {
  vol: number[]
  optVolCall: number[]
  optVolPut: number[]
  cumDelta: number[]
  /** Delta-vážený opční tok per strana (|Δ| × přírůstek volume) — čtení C/P aktivity. */
  deltaFlowCall: number[]
  deltaFlowPut: number[]
  /** SentIndex po minutách (#288); prázdné = modul zatím data nemá. */
  sentiment?: number[]
  /** Daily pohled (#296, SPEC 7.1): OHLC svíčka per sloupec-den; null = den
  bez dat. Když je přítomné, panel Sentiment kreslí svíčky místo plochy. */
  sentimentCandles?: (SentimentCandle | null)[]
}

export interface PanelsVisible {
  vol: boolean
  optVol: boolean
  delta: boolean
  deltaFlow: boolean
  sentiment: boolean
}

/** Časová část transformace hlavního grafu (sdílená osa X). */
export interface TimeTransform {
  offsetX: number
  zoomX: number
}

const IDENTITY_TIME: TimeTransform = { offsetX: 0, zoomX: 1 }

const DEFAULT_height = 84

const fmtInt = (value: number): string => Math.round(value).toLocaleString('cs-CZ')
/** Znaménkový formát; malé škály (|peak| < 10, např. sentiment −1…+1) by
Math.round srazil na „±0", proto 2 desetinná místa (#418). */
const fmtSigned = (value: number, peak = Infinity): string => {
  const text =
    peak < 10
      ? value.toLocaleString('cs-CZ', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
      : Math.round(value).toLocaleString('cs-CZ')
  return (value > 0 ? '+' : '') + text
}

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
  // Svíčky sentimentu (#296): plné barvy shodné s cenovými svíčkami (SPEC 7.1)
  candleUp: '#3ecf8e',
  candleDown: '#f0616d',
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
  totalMinutes,
  height = DEFAULT_height,
}: {
  data: PanelSeries
  visible: PanelsVisible
  width?: number
  /** Pan/zoom osy X hlavního grafu — panely se roztahují synchronně. */
  time?: TimeTransform
  /** Počet sloupců osy X heatmapy včetně projekce (ADR-0006). Panely kreslí
  jen svá data, ale měřítko musí být shodné, jinak se časové osy rozjedou. */
  totalMinutes?: number
  /** Výška jednoho panelu — tažitelný předěl v App ji mění za běhu (#169). */
  height?: number
}) {
  const minutes = data.vol.length
  const axisMinutes = Math.max(minutes, totalMinutes ?? minutes)
  const pointer = usePanelPointer(axisMinutes, width, time)
  const { position } = useCrosshair()
  // Index pod crosshairem (sdílený napříč panely) — hodnoty vpravo nahoře
  const idx =
    position && position.minuteIdx >= 0 && position.minuteIdx < minutes ? position.minuteIdx : null
  // Výšková úroveň kurzoru v konkrétním panelu — hodnota na pravé ose Y
  const [hoverY, setHoverY] = useState<{ key: string; y: number } | null>(null)
  // Stejné základní měřítko jako heatmapa — málo dat se neroztahuje na šířku
  const step = baseBucketPx(axisMinutes, width)
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
    const cssScale = rect.height > 0 ? height / rect.height : 1
    setHoverY({ key, y: (event.clientY - rect.top) * cssScale })
  }
  const handleLeave = () => {
    pointer.clear()
    setHoverY(null)
  }
  /** Hodnota na ose Y podle výšky kurzoru (signed = symetrická škála kolem nuly,
  se stejnou rezervou od okrajů jako plocha Cum Δ). */
  const axisValue = (key: string, peak: number, signed: boolean): React.ReactNode => {
    if (!hoverY || hoverY.key !== key) return null
    const y = Math.min(height, Math.max(0, hoverY.y))
    const value = signed
      ? ((height / 2 - y) / Math.max(1, height / 2 - CUM_DELTA_PAD)) * peak
      : ((height - y) / (height - 4)) * peak
    return <PanelAxisValue y={y}>{signed ? fmtSigned(value, peak) : fmtInt(value)}</PanelAxisValue>
  }
  /** Vodorovná crosshair linka na úrovni kurzoru (jen v najetém panelu, mimo transform). */
  const axisLineH = (key: string): React.ReactNode => {
    if (!hoverY || hoverY.key !== key) return null
    const y = Math.min(height, Math.max(0, hoverY.y))
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
    const heights = barHeights(data.vol, height - 4, volPeak)
    panels.push(
      <section key="vol" className="bottom-panel" aria-label="Vol panel">
        <span className="panel-title muted">Vol</span>
        {idx !== null && <PanelValue>{fmtInt(data.vol[idx])}</PanelValue>}
        {axisValue('vol', volPeak, false)}
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('vol')}
          onPointerLeave={handleLeave}
        >
          <g transform={transform}>
            {heights.map((barHeight, index) => (
              <rect
                key={index}
                x={(index + 0.5) * step - barWidth / 2}
                y={height - barHeight}
                width={barWidth}
                height={barHeight}
                fill={COLORS.vol}
              />
            ))}
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('vol')}
        </svg>
      </section>,
    )
  }

  if (visible.optVol) {
    const callHeights = barHeights(data.optVolCall, height - 4, optPeak)
    const putHeights = barHeights(data.optVolPut, height - 4, optPeak)
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
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('optvol')}
          onPointerLeave={handleLeave}
        >
          <g transform={transform}>
            {callHeights.map((barHeight, index) => (
              <rect
                key={`c${index}`}
                data-part="optvol-call"
                x={(index + 0.5) * step - barWidth / 2}
                y={height - barHeight}
                width={barWidth / 2}
                height={barHeight}
                fill={COLORS.call}
              />
            ))}
            {putHeights.map((barHeight, index) => (
              <rect
                key={`p${index}`}
                data-part="optvol-put"
                x={(index + 0.5) * step}
                y={height - barHeight}
                width={barWidth / 2}
                height={barHeight}
                fill={COLORS.put}
              />
            ))}
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('optvol')}
        </svg>
      </section>,
    )
  }

  if (visible.deltaFlow) {
    const callHeights = barHeights(data.deltaFlowCall, height - 4, flowPeak)
    const putHeights = barHeights(data.deltaFlowPut, height - 4, flowPeak)
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
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('deltaflow')}
          onPointerLeave={handleLeave}
        >
          <g transform={transform}>
            {callHeights.map((barHeight, index) => (
              <rect
                key={`c${index}`}
                data-part="deltaflow-call"
                x={(index + 0.5) * step - barWidth / 2}
                y={height - barHeight}
                width={barWidth / 2}
                height={barHeight}
                fill={COLORS.call}
              />
            ))}
            {putHeights.map((barHeight, index) => (
              <rect
                key={`p${index}`}
                data-part="deltaflow-put"
                x={(index + 0.5) * step}
                y={height - barHeight}
                width={barWidth / 2}
                height={barHeight}
                fill={COLORS.put}
              />
            ))}
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('deltaflow')}
        </svg>
      </section>,
    )
  }

  if (visible.delta) {
    const areas = cumDeltaAreas(data.cumDelta, minutes * step, height)
    panels.push(
      <section key="cumdelta" className="bottom-panel" aria-label="Cum Δ panel">
        <span className="panel-title muted">Cum Δ</span>
        {idx !== null && <PanelValue>{fmtSigned(data.cumDelta[idx])}</PanelValue>}
        {axisValue('cumdelta', cumPeak, true)}
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
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
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('cumdelta')}
        </svg>
      </section>,
    )
  }

  // Daily pohled (#296): svíčky místo plochy — viditelný intradenní rozkmit
  // sentimentu; barvy shodné s cenovými svíčkami (SPEC 7.1)
  if (visible.sentiment && data.sentimentCandles && data.sentimentCandles.length > 0) {
    const candles = data.sentimentCandles
    const { geoms, zeroY } = sentimentCandleGeometry(candles, step, height)
    const peak = Math.max(
      1e-9,
      ...candles.flatMap((candle) => (candle ? [Math.abs(candle.high), Math.abs(candle.low)] : [])),
    )
    const hovered = idx !== null ? candles[idx] : null
    panels.push(
      <section key="sentiment" className="bottom-panel" aria-label="Sentiment panel">
        <span className="panel-title muted">Sentiment</span>
        {hovered && (
          <PanelValue>
            O {hovered.open.toFixed(2)} · H {hovered.high.toFixed(2)} · L {hovered.low.toFixed(2)} ·
            C {hovered.close.toFixed(2)}
          </PanelValue>
        )}
        {axisValue('sentiment', peak, true)}
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('sentiment')}
          onPointerLeave={handleLeave}
        >
          <line
            x1={0}
            y1={zeroY}
            x2={width}
            y2={zeroY}
            stroke="#2c3342"
            data-testid="sentiment-zero"
          />
          <g transform={transform}>
            {geoms.map((geom) => (
              <g key={geom.index} data-part="sentiment-candle">
                <line
                  x1={geom.x}
                  y1={geom.wickY1}
                  x2={geom.x}
                  y2={geom.wickY2}
                  stroke={geom.up ? COLORS.candleUp : COLORS.candleDown}
                />
                <rect
                  x={geom.x - barWidth / 2}
                  y={geom.bodyY}
                  width={barWidth}
                  height={geom.bodyHeight}
                  fill={geom.up ? COLORS.candleUp : COLORS.candleDown}
                />
              </g>
            ))}
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('sentiment')}
        </svg>
      </section>,
    )
  } else if (visible.sentiment && data.sentiment && data.sentiment.length > 0) {
    const sentiment = data.sentiment
    const areas = cumDeltaAreas(sentiment, minutes * step, height)
    const peak = seriesPeak(sentiment)
    panels.push(
      <section key="sentiment" className="bottom-panel" aria-label="Sentiment panel">
        <span className="panel-title muted">Sentiment</span>
        {idx !== null && sentiment[idx] !== undefined && (
          <PanelValue>{sentiment[idx].toFixed(2)}</PanelValue>
        )}
        {axisValue('sentiment', peak, true)}
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('sentiment')}
          onPointerLeave={handleLeave}
        >
          <g transform={transform}>
            {/* Vizuálně shodné s Cum Δ: trader čte flow a sentiment vedle sebe */}
            {/* cumDeltaAreas vrací body polygonu, ne path `d` — jako u Cum Δ */}
            <polygon
              points={areas.positive}
              fill={COLORS.positive}
              opacity={0.75}
              data-part="sentiment-pos"
            />
            <polygon
              points={areas.negative}
              fill={COLORS.negative}
              opacity={0.75}
              data-part="sentiment-neg"
            />
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('sentiment')}
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
