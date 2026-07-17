/** Pravý strike profil panel (SPEC 4.6/7.3): skládané pruhy C/P, zoom, cenová linka.

Orientace dle Moodix: call doprava (teal), put doleva (červená), symetrická osa
uprostřed. Složky Vol (sytý odstín) a OI Δ (světlejší) jsou skládané. Osa strikes
je sdílená s heatmapou (stejné pořadí, nejvyšší strike nahoře) a crosshair se
synchronizuje přes sdílený kontext.
*/
import { useMemo, useState } from 'react'
import { fractionalRow } from '../heatmap/overlays'
import { barGeometry } from '../profile/bars'
import type { ProfileRow } from '../profile/bars'
import { useCrosshair } from '../state/Crosshair'

const ROW_GAP = 1

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
}: {
  rows: ProfileRow[]
  spot: number | null
  height?: number
  /** Šířka panelu — tažitelný předěl v App ji mění za běhu. */
  width?: number
}) {
  const [zoom, setZoom] = useState<1 | 2 | 4>(1)
  const { position: crosshair, setPosition: setCrosshair } = useCrosshair()

  // Nejvyšší strike nahoře — stejná orientace jako heatmapa
  const ordered = useMemo(() => [...rows].sort((a, b) => b.strike - a.strike), [rows])
  const strikesAscending = useMemo(() => ordered.map((row) => row.strike).reverse(), [ordered])
  const rowHeight = ordered.length > 0 ? height / ordered.length : 0
  const halfWidth = width / 2
  const geometry = useMemo(
    () => new Map(barGeometry(ordered, halfWidth, zoom).map((bar) => [bar.strike, bar])),
    [ordered, halfWidth, zoom],
  )

  const spotRow = spot === null ? null : fractionalRow(strikesAscending, spot)
  const spotY =
    spotRow === null || ordered.length === 0
      ? null
      : (ordered.length - 1 - spotRow + 0.5) * rowHeight

  const hovered = crosshair
    ? (ordered.find((row) => row.strike === crosshair.strike) ?? null)
    : null

  return (
    <aside className="strike-profile" aria-label="Strike profil" style={{ width }}>
      <div className="profile-header">
        <span className="muted">Vol + OI Δ</span>
        <div role="toolbar" aria-label="Zoom profilu">
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
      <svg
        width={width}
        height={height}
        role="img"
        aria-label="Skládané pruhy strike profilu"
        onPointerLeave={() => setCrosshair(null)}
      >
        {/* symetrická osa */}
        <line x1={halfWidth} y1={0} x2={halfWidth} y2={height} stroke="#2c3342" />
        {/* popisky strikes (každý k-tý, ať se nepřekrývají) */}
        {ordered.map((row, index) => {
          const labelEvery = Math.max(1, Math.ceil(16 / Math.max(1, rowHeight)))
          if (index % labelEvery !== 0) return null
          return (
            <text
              key={`label-${row.strike}`}
              x={4}
              y={index * rowHeight + rowHeight / 2 + 3}
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
          const y = index * rowHeight
          const barHeight = Math.max(1, rowHeight - ROW_GAP)
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
                <rect x={0} y={y} width={width} height={barHeight} fill="rgba(215,220,230,0.08)" />
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
      {hovered && (
        <div className="profile-tooltip" role="tooltip">
          <strong>{hovered.strike}</strong>
          <span>
            OI C/P: {hovered.callOi.toFixed(0)} / {hovered.putOi.toFixed(0)}
          </span>
          <span>
            Vol C/P: {hovered.callVolume.toFixed(0)} / {hovered.putVolume.toFixed(0)}
          </span>
          <span>Δ od spotu: {hovered.distanceFromSpot.toFixed(1)}</span>
        </div>
      )}
    </aside>
  )
}
