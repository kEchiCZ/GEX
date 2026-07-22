/** Pravý strike profil panel (SPEC 4.6/7.3): skládané pruhy C/P, zoom, cenová linka.

Orientace dle Moodix: call doprava (teal), put doleva (červená), symetrická osa
uprostřed. Složky Vol (sytý odstín) a OI Δ (světlejší) jsou skládané. Osa strikes
je sdílená s heatmapou (stejné pořadí, nejvyšší strike nahoře) a crosshair se
synchronizuje přes sdílený kontext.

S `yView` panel používá stejnou Y transformaci jako heatmapa (offsetY, zoomY nad
její výškou) — strike je v obou na stejné obrazovkové úrovni a při zoomu/panu
grafu se pruhy hýbou synchronně. Bez `yView` (testy) platí legacy rozložení.
*/
import { memo, useMemo, useRef, useState } from 'react'
import { useElementSize } from '../hooks/useElementSize'
import { zoomAxis } from '../heatmap/view'
import type { ViewTransform } from '../heatmap/view'
import { fractionalRow } from '../heatmap/overlays'
import { barGeometry, formatAmount, gexCurvePaths, maxComponentSide, niceCeil } from '../profile/bars' // prettier-ignore
import type { ProfileRow } from '../profile/bars'
import type { GexProfileRow } from '../replay/loader'
import { usePersistentState } from '../state/persist'
import { useCrosshair } from '../state/Crosshair'

/** Y transformace hlavního grafu: strike → obrazovková výška (sdílená osa). */
export interface ProfileYView {
  offsetY: number
  zoomY: number
  /** Logická výška heatmapy — základ měřítka (scaleY = baseHeight/n × zoomY). */
  baseHeight: number
}

const ROW_GAP = 1
// Rezerva na každé straně pro číselný popisek hodnoty — pruhy nekončí až u okraje
const LABEL_SPACE = 40
// Levý pruh profilu s hodnotami strikes = úchop pro roztahování Y osy (#181);
// stejná šířka jako AXIS_Y_WIDTH heatmapy, ať se osy chovají konzistentně
const PROFILE_AXIS_ZONE = 48
// Odhad šířky znaku popisku hodnoty (font 9 px) — kolizní logika popisků (#181)
const VALUE_CHAR_PX = 5.5
// Prostor strike popisků u levého okraje — hodnoty do něj nesmí zasáhnout
const STRIKE_LABEL_RESERVE = 34

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

function StrikeProfileBase({
  rows,
  spot,
  height = 640,
  width = 260,
  yView = null,
  onYViewChange,
  aggregate = null,
  onAggregateToggle,
  gexProfile = null,
  axisStrikes = null,
}: {
  rows: ProfileRow[]
  spot: number | null
  height?: number
  /** Šířka panelu — tažitelný předěl v App ji mění za běhu. */
  width?: number
  /** Sdílená Y transformace s heatmapou; null = vlastní statické rozložení. */
  yView?: ProfileYView | null
  /** Úprava Y osy grafu tažením/kolečkem na profilu (jako levá osa heatmapy). */
  onYViewChange?: (next: { offsetY: number; zoomY: number }) => void
  /** Σ souhrn přes expirace: null = přepínač skrytý, jinak stav zapnuto/vypnuto. */
  aggregate?: boolean | null
  onAggregateToggle?: () => void
  /** Dyn GEX profil aktuální minuty (ADR-0009); null = vrstva nedostupná. */
  gexProfile?: GexProfileRow | null
  /** Strikes HEATMAPY vzestupně — sdílená osa Y (#213). Řádky panelu (Σ souhrn
  = sjednocení expirací) můžou mít jinou sadu než graf; bez kotvení k této ose
  by se cenové osy obou panelů rozjely. Null = osa z vlastních řádků (legacy). */
  axisStrikes?: number[] | null
}) {
  const [zoom, setZoom] = useState<1 | 2 | 4>(1)
  const [scaleMode, setScaleMode] = useState<'rel' | 'abs'>('rel')
  // Dyn GEX křivka (ADR-0009) — přepínatelná, volba přežívá refresh (ADR-0007)
  const [gexOn, setGexOn] = usePersistentState('profileGex', true, (value, fallback) =>
    typeof value === 'boolean' ? value : fallback,
  )
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
  // Osa Y (#213): se sdíleným pohledem VŽDY strikes heatmapy — vlastní řádky
  // (Σ souhrn přes expirace) můžou mít jinou sadu/počet a osa by se rozjela
  const axis = yView && axisStrikes && axisStrikes.length > 0 ? axisStrikes : strikesAscending
  // Krok řádku: se sdílenou osou přesně kopíruje heatmapu (baseHeight/n × zoomY)
  const rowHeight =
    axis.length > 0
      ? yView
        ? (yView.baseHeight / axis.length) * yView.zoomY
        : height / axis.length
      : 0
  const offsetY = yView?.offsetY ?? 0

  // Úprava Y osy grafu tažením/kolečkem na profilu (stejná matematika jako heatmap
  // osa) — jen nad pruhem s hodnotami strikes, zbytek panelu osu nehýbe (#181)
  const dragYRef = useRef<number | null>(null)
  const [axisHover, setAxisHover] = useState(false)
  const yInteractive = Boolean(yView && onYViewChange)
  const inAxisZone = (event: { clientX: number; currentTarget: Element }): boolean =>
    event.clientX - event.currentTarget.getBoundingClientRect().left < PROFILE_AXIS_ZONE
  const yBase = (): ViewTransform => ({ offsetX: 0, offsetY, zoomX: 1, zoomY: yView?.zoomY ?? 1 })
  const applyY = (next: ViewTransform) =>
    onYViewChange?.({ offsetY: next.offsetY, zoomY: next.zoomY })
  const onProfileWheel = (event: React.WheelEvent<SVGSVGElement>) => {
    if (!yInteractive || !inAxisZone(event)) return
    const rect = event.currentTarget.getBoundingClientRect()
    const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15
    applyY(zoomAxis(yBase(), 'y', factor, event.clientY - rect.top))
  }
  const onProfilePointerDown = (event: React.PointerEvent<SVGSVGElement>) => {
    if (!yInteractive || !inAxisZone(event)) return
    dragYRef.current = event.clientY
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const onProfilePointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
    if (dragYRef.current === null || !yInteractive) {
      setAxisHover(yInteractive && inAxisZone(event))
      return
    }
    const deltaY = event.clientY - dragYRef.current
    dragYRef.current = event.clientY
    // Kotva = střed (jako scale-y na levé ose): stlačení/roztažení cenové osy
    applyY(zoomAxis(yBase(), 'y', Math.exp(-deltaY * 0.005), (yView?.baseHeight ?? 0) / 2))
  }
  const onProfilePointerUp = () => {
    dragYRef.current = null
  }
  const halfWidth = width / 2
  // Pruhy končí LABEL_SPACE před okrajem — nepřetékají a je místo na číslo
  const barHalf = Math.max(10, halfWidth - LABEL_SPACE)
  // Referenční strana měřítka: Rel = max ve výřezu, Abs = zaokrouhlený „nice" strop
  const maxSide = ordered.length > 0 ? maxComponentSide(ordered) : 0
  const scaleMax = scaleMode === 'abs' ? niceCeil(maxSide) : maxSide
  const geometry = useMemo(
    () => new Map(barGeometry(ordered, barHalf, zoom, scaleMax).map((bar) => [bar.strike, bar])),
    [ordered, barHalf, zoom, scaleMax],
  )
  // Osa množství: plná strana = scaleMax/zoom (Δ-vážené kontrakty)
  const axisFull = ordered.length > 0 ? scaleMax / zoom : 0
  // Popisky (strike i hodnoty) jen na každém k-tém řádku, ať se nepřekrývají
  const labelEvery = Math.max(1, Math.ceil(16 / Math.max(1, rowHeight)))

  /** Cena → Y v souřadnicích profilu (shodné s heatmap rowToY, osa = `axis`). */
  const priceToY = (price: number): number | null => {
    const row = fractionalRow(axis, price)
    if (row === null || axis.length === 0) return null
    return (axis.length - 1 - row + 0.5) * rowHeight + offsetY
  }
  /** Střed řádku pro strike — mimo obálku osy null (fractionalRow by přilepil
  hodnotu na kraj a Σ řádky cizí expirace by se vršily na okrajích). */
  const strikeCenterY = (strike: number): number | null => {
    if (axis.length === 0 || strike < axis[0] || strike > axis[axis.length - 1]) return null
    return priceToY(strike)
  }
  const spotY = spot === null ? null : priceToY(spot)

  // Dyn GEX křivka (ADR-0009): kladná doprava (tlumení), záporná doleva
  const gexCurve = useMemo(() => {
    if (!gexOn || !gexProfile || ordered.length === 0) return null
    return gexCurvePaths(
      gexProfile,
      (price) => priceToY(price) ?? -100,
      halfWidth,
      Math.max(10, halfWidth - LABEL_SPACE) * 0.95,
    )
    // priceToY závisí na axis/rowHeight/offsetY — pokryto závislostmi níže
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gexOn, gexProfile, ordered, axis, rowHeight, offsetY, halfWidth])

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
          {gexProfile !== null && (
            <button
              className={gexOn ? 'chip active' : 'chip'}
              onClick={() => setGexOn((value) => !value)}
              aria-label="Dyn GEX profil"
              title="Modelovaný NetGEX přes cenové pásmo (ADR-0009): zelená doprava = dealeři tlumí, červená doleva = zesilují; žlutá = dynamický flip"
            >
              GEX
            </button>
          )}
          <button
            className={scaleMode === 'abs' ? 'chip active' : 'chip'}
            onClick={() => setScaleMode((mode) => (mode === 'abs' ? 'rel' : 'abs'))}
            aria-label="Absolutní / relativní škála"
            title="Rel = normalizace na max ve výřezu; Abs = zaokrouhlený strop (kulaté hodnoty)"
          >
            {scaleMode === 'abs' ? 'Abs' : 'Rel'}
          </button>
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
          style={{ cursor: axisHover ? 'ns-resize' : undefined }}
          onWheel={onProfileWheel}
          onPointerDown={onProfilePointerDown}
          onPointerMove={onProfilePointerMove}
          onPointerUp={onProfilePointerUp}
          onPointerLeave={() => {
            setCrosshair(null)
            setAxisHover(false)
          }}
        >
          {/* symetrická osa */}
          <line x1={halfWidth} y1={0} x2={halfWidth} y2={svgHeight} stroke="#2c3342" />
          {/* popisky strikes (každý k-tý, ať se nepřekrývají) */}
          {ordered.map((row, index) => {
            if (index % labelEvery !== 0) return null
            const centerY = strikeCenterY(row.strike)
            if (centerY === null) return null
            return (
              <text
                key={`label-${row.strike}`}
                x={4}
                y={centerY + 3}
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
            const centerY = strikeCenterY(row.strike)
            if (!bar || centerY === null) return null
            const barHeight = Math.max(1, rowHeight - ROW_GAP)
            const y = centerY - barHeight / 2
            const highlighted = crosshair?.strike === row.strike
            // Kolizní logika popisků hodnot (#181): když se číslo nevejde vedle
            // pruhu (put by zasáhl do strike popisků, call za pravý okraj),
            // překlopí se DOVNITŘ pruhu tmavým textem — nikdy se nepřekrývá
            const callText = formatAmount(row.callVolComponent + row.callOiComponent)
            const callEnd = halfWidth + bar.callVolWidth + bar.callOiWidth
            const callOutside = callEnd + 3 + callText.length * VALUE_CHAR_PX <= width - 2
            const putText = formatAmount(row.putVolComponent + row.putOiComponent)
            const putEnd = halfWidth - bar.putVolWidth - bar.putOiWidth
            const putOutside = putEnd - 3 - putText.length * VALUE_CHAR_PX >= STRIKE_LABEL_RESERVE
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
                {/* Číselné hodnoty (Δ-vážené kontrakty) u konce pruhů — každý k-tý řádek */}
                {index % labelEvery === 0 && row.callVolComponent + row.callOiComponent > 0 && (
                  <text
                    x={callOutside ? callEnd + 3 : callEnd - 3}
                    y={centerY + 3}
                    fontSize={9}
                    fill={callOutside ? COLORS.callVol : '#12151c'}
                    textAnchor={callOutside ? 'start' : 'end'}
                    data-part="value-call"
                  >
                    {callText}
                  </text>
                )}
                {index % labelEvery === 0 && row.putVolComponent + row.putOiComponent > 0 && (
                  <text
                    x={putOutside ? putEnd - 3 : putEnd + 3}
                    y={centerY + 3}
                    fontSize={9}
                    fill={putOutside ? COLORS.putVol : '#12151c'}
                    textAnchor={putOutside ? 'end' : 'start'}
                    data-part="value-put"
                  >
                    {putText}
                  </text>
                )}
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
                  { x: halfWidth - barHalf, value: axisFull, anchor: 'start' },
                  { x: halfWidth - barHalf / 2, value: axisFull / 2, anchor: 'middle' },
                  { x: halfWidth, value: 0, anchor: 'middle' },
                  { x: halfWidth + barHalf / 2, value: axisFull / 2, anchor: 'middle' },
                  { x: halfWidth + barHalf, value: axisFull, anchor: 'end' },
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
          {/* Dyn GEX křivka (ADR-0009): model NetGEX přes pásmo, žlutá = flip */}
          {gexCurve && (
            <g data-part="gex-curve" opacity={0.9}>
              {gexCurve.flipYs.map((y, index) => (
                <line
                  key={index}
                  x1={0}
                  y1={y}
                  x2={width}
                  y2={y}
                  stroke="#e8c14b"
                  strokeDasharray="3 3"
                  opacity={0.65}
                  data-part="gex-flip"
                />
              ))}
              {gexCurve.positive && (
                <path
                  d={gexCurve.positive}
                  stroke="#3ecf8e"
                  fill="none"
                  strokeWidth={1.6}
                  data-part="gex-positive"
                />
              )}
              {gexCurve.negative && (
                <path
                  d={gexCurve.negative}
                  stroke="#f0616d"
                  fill="none"
                  strokeWidth={1.6}
                  data-part="gex-negative"
                />
              )}
            </g>
          )}
          {/* cenová linka — neutrální šedá, žlutá patří flipům (#213) */}
          {spotY !== null && (
            <line
              x1={0}
              y1={spotY}
              x2={width}
              y2={spotY}
              stroke="#d7dce6"
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

// Memoizace: živý spot překresluje jen graf, ne tento SVG profil
export const StrikeProfile = memo(StrikeProfileBase)
