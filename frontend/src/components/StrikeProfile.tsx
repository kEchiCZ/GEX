/** Pravý strike profil panel (SPEC 4.6/7.3): skládané pruhy C/P, zoom, cenová linka.

Orientace dle Moodix: call doprava (teal), put doleva (červená), symetrická osa
uprostřed. Složky Vol (sytý odstín) a OI Δ (světlejší) jsou skládané. Osa strikes
je sdílená s heatmapou (stejné pořadí, nejvyšší strike nahoře) a crosshair se
synchronizuje přes sdílený kontext.

S `yView` panel používá stejnou Y transformaci jako heatmapa (offsetY, zoomY nad
její výškou) — strike je v obou na stejné obrazovkové úrovni a při zoomu/panu
grafu se pruhy hýbou synchronně. Bez `yView` (testy) platí legacy rozložení.
*/
import { useMemo, useState } from 'react'
import { useElementSize } from '../hooks/useElementSize'
import { fractionalRow } from '../heatmap/overlays'
import { barGeometry, formatAmount, maxComponentSide } from '../profile/bars'
import type { ProfileRow } from '../profile/bars'
import { useCrosshair } from '../state/Crosshair'

/** Y transformace hlavního grafu: strike → obrazovková výška (sdílená osa). */
export interface ProfileYView {
  offsetY: number
  zoomY: number
  /** Logická výška heatmapy — základ měřítka (scaleY = baseHeight/n × zoomY). */
  baseHeight: number
}

const ROW_GAP = 1

/** Změna se znaménkem (+120 / −45) pro ΔOI tooltip. */
function formatSigned(value: number): string {
  const rounded = Math.round(value)
  return rounded > 0 ? `+${rounded}` : String(rounded)
}

const COLORS = {
  callVol: '#14b8a6',
  callOi: 'rgba(20, 184, 166, 0.45)',
  putVol: '#ef4444',
  putOi: 'rgba(239, 68, 68, 0.45)',
}

export function StrikeProfile({
  rows,
  spot,
  height = 640,
  width = 260,
  yView = null,
  aggregate = null,
  onAggregateToggle,
}: {
  rows: ProfileRow[]
  spot: number | null
  height?: number
  /** Šířka panelu — tažitelný předěl v App ji mění za běhu. */
  width?: number
  /** Sdílená Y transformace s heatmapou; null = vlastní statické rozložení. */
  yView?: ProfileYView | null
  /** Σ souhrn přes expirace: null = přepínač skrytý, jinak stav zapnuto/vypnuto. */
  aggregate?: boolean | null
  onAggregateToggle?: () => void
}) {
  const [zoom, setZoom] = useState<1 | 2 | 4>(1)
  const { position: crosshair, setPosition: setCrosshair } = useCrosshair()
  // Se sdílenou osou svg vyplní celý panel (řádky mimo výřez se přirozeně oříznou)
  const { ref: bodyRef, size: bodySize } = useElementSize<HTMLDivElement>({
    width,
    height,
  })
  const svgHeight = yView ? bodySize.height : height

  // Nejvyšší strike nahoře — stejná orientace jako heatmapa
  const ordered = useMemo(() => [...rows].sort((a, b) => b.strike - a.strike), [rows])
  const strikesAscending = useMemo(() => ordered.map((row) => row.strike).reverse(), [ordered])
  // Krok řádku: se sdílenou osou přesně kopíruje heatmapu (baseHeight/n × zoomY)
  const rowHeight =
    ordered.length > 0
      ? yView
        ? (yView.baseHeight / ordered.length) * yView.zoomY
        : height / ordered.length
      : 0
  const offsetY = yView?.offsetY ?? 0
  /** Střed řádku i (descending pořadí) — shodný vzorec s heatmap rowToY. */
  const rowCenterY = (index: number): number => (index + 0.5) * rowHeight + offsetY
  const halfWidth = width / 2
  const geometry = useMemo(
    () => new Map(barGeometry(ordered, halfWidth, zoom).map((bar) => [bar.strike, bar])),
    [ordered, halfWidth, zoom],
  )
  // Osa množství: plná šířka strany = maxSide/zoom (Δ-vážené kontrakty)
  const axisFull = ordered.length > 0 ? maxComponentSide(ordered) / zoom : 0

  const spotRow = spot === null ? null : fractionalRow(strikesAscending, spot)
  const spotY =
    spotRow === null || ordered.length === 0
      ? null
      : (ordered.length - 1 - spotRow + 0.5) * rowHeight + offsetY

  const hovered = crosshair
    ? (ordered.find((row) => row.strike === crosshair.strike) ?? null)
    : null

  return (
    <aside className="strike-profile" aria-label="Strike profil" style={{ width }}>
      <div className="profile-header">
        <span className="muted">{aggregate ? 'Vol + OI Δ · Σ expirací' : 'Vol + OI Δ'}</span>
        <div role="toolbar" aria-label="Zoom profilu">
          {aggregate !== null && (
            <button
              className={aggregate ? 'chip active' : 'chip'}
              onClick={onAggregateToggle}
              aria-label="Souhrn přes expirace"
              title="Σ = součet OI + volume přes všechny sbírané expirace"
            >
              Σ
            </button>
          )}
          {([1, 2, 4] as const).map((value) => (
            <button
              key={value}
              className={zoom === value ? 'chip active' : 'chip'}
              onClick={() => setZoom(value)}
            >
              {value}×
            </button>
          ))}
        </div>
      </div>
      <div className="profile-body" ref={bodyRef}>
        <svg
          width={width}
          height={svgHeight}
          role="img"
          aria-label="Skládané pruhy strike profilu"
          onPointerLeave={() => setCrosshair(null)}
        >
          {/* symetrická osa */}
          <line x1={halfWidth} y1={0} x2={halfWidth} y2={svgHeight} stroke="#2c3342" />
          {/* popisky strikes (každý k-tý, ať se nepřekrývají) */}
          {ordered.map((row, index) => {
            const labelEvery = Math.max(1, Math.ceil(16 / Math.max(1, rowHeight)))
            if (index % labelEvery !== 0) return null
            return (
              <text
                key={`label-${row.strike}`}
                x={4}
                y={rowCenterY(index) + 3}
                fontSize={10}
                fill="#7d8596"
                data-part="strike-label"
              >
                {row.strike}
              </text>
            )
          })}
          {ordered.map((row, index) => {
            const bar = geometry.get(row.strike)
            if (!bar) return null
            const barHeight = Math.max(1, rowHeight - ROW_GAP)
            const y = rowCenterY(index) - barHeight / 2
            const highlighted = crosshair?.strike === row.strike
            return (
              <g
                key={row.strike}
                data-testid={`profile-row-${row.strike}`}
                opacity={highlighted ? 1 : 0.88}
                onPointerEnter={() =>
                  setCrosshair({ minuteIdx: crosshair?.minuteIdx ?? 0, strike: row.strike })
                }
              >
                {highlighted && (
                  <rect
                    x={0}
                    y={y}
                    width={width}
                    height={barHeight}
                    fill="rgba(215,220,230,0.08)"
                  />
                )}
                {/* call: doprava — Vol sytě, OI Δ světleji (skládané) */}
                <rect
                  x={halfWidth}
                  y={y}
                  width={bar.callVolWidth}
                  height={barHeight}
                  fill={COLORS.callVol}
                  data-part="call-vol"
                />
                <rect
                  x={halfWidth + bar.callVolWidth}
                  y={y}
                  width={bar.callOiWidth}
                  height={barHeight}
                  fill={COLORS.callOi}
                  data-part="call-oi"
                />
                {/* put: doleva */}
                <rect
                  x={halfWidth - bar.putVolWidth}
                  y={y}
                  width={bar.putVolWidth}
                  height={barHeight}
                  fill={COLORS.putVol}
                  data-part="put-vol"
                />
                <rect
                  x={halfWidth - bar.putVolWidth - bar.putOiWidth}
                  y={y}
                  width={bar.putOiWidth}
                  height={barHeight}
                  fill={COLORS.putOi}
                  data-part="put-oi"
                />
              </g>
            )
          })}
          {/* Osa množství (Δ-vážené kontrakty) + strany Put/Call — dole nad okrajem */}
          {axisFull > 0 && (
            <g data-part="amount-axis" fontSize={9} fill="#7d8596">
              <text x={6} y={12} fill="#ef4444">
                Put
              </text>
              <text x={width - 26} y={12} fill="#14b8a6">
                Call
              </text>
              {(
                [
                  { x: 2, value: axisFull, anchor: 'start' },
                  { x: halfWidth / 2, value: axisFull / 2, anchor: 'middle' },
                  { x: halfWidth, value: 0, anchor: 'middle' },
                  { x: halfWidth + halfWidth / 2, value: axisFull / 2, anchor: 'middle' },
                  { x: width - 2, value: axisFull, anchor: 'end' },
                ] as Array<{ x: number; value: number; anchor: 'start' | 'middle' | 'end' }>
              ).map((tick, index) => (
                <text
                  key={index}
                  x={tick.x}
                  y={svgHeight - 4}
                  textAnchor={tick.anchor}
                  data-part="amount-tick"
                >
                  {formatAmount(tick.value)}
                </text>
              ))}
            </g>
          )}
          {/* cenová linka */}
          {spotY !== null && (
            <line
              x1={0}
              y1={spotY}
              x2={width}
              y2={spotY}
              stroke="#e8c14b"
              strokeDasharray="4 3"
              data-testid="profile-price-line"
            />
          )}
        </svg>
      </div>
      {hovered && (
        <div className="profile-tooltip" role="tooltip">
          <strong>{hovered.strike}</strong>
          <span>
            OI C/P: {hovered.callOi.toFixed(0)} / {hovered.putOi.toFixed(0)}
          </span>
          <span>
            Vol C/P: {hovered.callVolume.toFixed(0)} / {hovered.putVolume.toFixed(0)}
          </span>
          {hovered.callOiChange != null && hovered.putOiChange != null && (
            <span data-testid="oi-change">
              ΔOI vs. včera C/P: {formatSigned(hovered.callOiChange)} /{' '}
              {formatSigned(hovered.putOiChange)}
            </span>
          )}
          <span>Δ od spotu: {hovered.distanceFromSpot.toFixed(1)}</span>
        </div>
      )}
    </aside>
  )
}
