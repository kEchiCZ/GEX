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
import { memo, useRef, useState } from 'react'
import { baseBucketPx } from '../heatmap/view'
import { minuteLabel } from '../replay/useDayData'
import {
  CUM_DELTA_PAD,
  barHeights,
  cumDeltaAreas,
  cvdLinePoints,
  evoOiDisplay,
  evoOiStepPath,
  sentimentCandleGeometry,
  seriesPeak,
} from '../panels/geometry'
import type { SentimentCandle } from '../panels/geometry'
import { useCrosshair } from '../state/Crosshair'
import { oneOf, usePersistentState } from '../state/persist'

export interface PanelSeries {
  vol: number[]
  optVolCall: number[]
  optVolPut: number[]
  cumDelta: number[]
  /** CVD podkladu (#829): kumulativní tok ve futures (agresivní nákupy −
  prodeje), druhá řada téhož panelu. null v minutě = data chybí (bez tasty
  větve); prázdné/chybějící pole = řada se nekreslí vůbec. */
  futuresCvd?: (number | null)[]
  /** Delta-vážený opční tok per strana (|Δ| × přírůstek volume) — čtení C/P aktivity. */
  deltaFlowCall: number[]
  deltaFlowPut: number[]
  /** Evo OI (#573): Σ OI přes striky per sloupec, C/P zvlášť; prázdné = starší data. */
  evoOiCall?: number[]
  evoOiPut?: number[]
  /** SentIndex po minutách (#288); prázdné = modul zatím data nemá. */
  sentiment?: number[]
  /** Daily pohled (#296, SPEC 7.1): OHLC svíčka per sloupec-den; null = den
  bez dat. Když je přítomné, panel Sentiment kreslí svíčky místo plochy. */
  sentimentCandles?: (SentimentCandle | null)[]
  /** ISO čas první měřené minuty, když den nezačíná od začátku seance (#518,
  ADR-0024) — engine startoval uprostřed dne a tok před tímto časem chybí.
  Panel Cum Δ to přizná popiskem „od HH:MM". Null/chybí = den je celý. */
  cumDeltaFromIso?: string | null
}

export interface PanelsVisible {
  vol: boolean
  optVol: boolean
  delta: boolean
  deltaFlow: boolean
  /** Evo OI (#573): vývoj celkového call/put OI — budování vs. zavírání pozic. */
  evoOi: boolean
  sentiment: boolean
}

/** Klíč panelu — jednotný pro viditelnost i individuální výšky (#792). */
export type PanelKey = keyof PanelsVisible

/** Meze výšky panelu (#792): minimum je poloviční proti původním 50 px (#169),
maximum zůstává. Klampuje App (jediný vlastník stavu) — komponenta hodnoty
jen kreslí a hlásí surové tažení. */
export const PANEL_HEIGHT_MIN = 25
export const PANEL_HEIGHT_MAX = 320

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
  // CVD podkladu (#829): studený odstín, ať se linka neplete s C/P barvami
  // opčního toku — jsou to různé veličiny, ne dvě strany téže
  cvd: '#8ab4f8',
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
  heights,
  onHeightChange,
  range = null,
  rangeB = null,
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
  /** Individuální výšky per panel (#792) — mají přednost před sdílenou. */
  heights?: Partial<Record<PanelKey, number>>
  /** Tažení úchytu na spodní hraně panelu (#792): hlásí SUROVOU cílovou
  výšku, klampování dělá vlastník stavu (App). Bez handleru se úchyty nekreslí. */
  onHeightChange?: (key: PanelKey, height: number) => void
  /** Aktivní range (#484) v koších osy — panely mimo okno se ztlumí. */
  range?: { startBucket: number; endBucket: number } | null
  /** Druhé okno B (#489) — dim je komplement sjednocení obou. */
  rangeB?: { startBucket: number; endBucket: number } | null
}) {
  const minutes = data.vol.length
  const axisMinutes = Math.max(minutes, totalMinutes ?? minutes)
  const pointer = usePanelPointer(axisMinutes, width, time)
  // Režim Evo OI (#573): Δ od začátku osy (výchozí) vs. absolutní úroveň
  const [evoOiMode, setEvoOiMode] = usePersistentState<'delta' | 'abs'>(
    'evoOiMode',
    'delta',
    oneOf(['delta', 'abs']),
  )
  const { position } = useCrosshair()
  // Index pod crosshairem (sdílený napříč panely) — hodnoty vpravo nahoře
  const idx =
    position && position.minuteIdx >= 0 && position.minuteIdx < minutes ? position.minuteIdx : null
  // Výšková úroveň kurzoru v konkrétním panelu — hodnota na pravé ose Y
  const [hoverY, setHoverY] = useState<{ key: string; y: number } | null>(null)
  // Stejné základní měřítko jako heatmapa — málo dat se neroztahuje na šířku
  const step = baseBucketPx(axisMinutes, width)
  const barWidth = Math.max(0.5, step * 0.8)
  // Individuální výška panelu (#792): override má přednost před sdílenou (#169)
  const heightOf = (key: PanelKey): number => heights?.[key] ?? height
  // Úchyt na spodní hraně panelu (#792) — mění výšku JEN toho jednoho panelu.
  // Jeden sdílený drag ref: naráz jde táhnout jediný úchyt.
  const resizeRef = useRef<{ key: PanelKey; y: number; start: number } | null>(null)
  const resizeHandle = (key: PanelKey, label: string): React.ReactNode => {
    if (!onHeightChange) return null
    return (
      <div
        className="panel-resize-h"
        role="separator"
        aria-orientation="horizontal"
        aria-label={`Výška panelu ${label}`}
        onPointerDown={(event) => {
          resizeRef.current = { key, y: event.clientY, start: heightOf(key) }
          event.currentTarget.setPointerCapture(event.pointerId)
        }}
        onPointerMove={(event) => {
          const drag = resizeRef.current
          if (!drag || drag.key !== key) return
          // Úchyt sedí na spodní hraně — tažení dolů panel zvětšuje 1:1
          onHeightChange(key, Math.round(drag.start + (event.clientY - drag.y)))
        }}
        onPointerUp={() => {
          resizeRef.current = null
        }}
      />
    )
  }
  // Ztlumení mimo range (#484): dva recty v datovém prostoru — transform <g>
  // je posune/roztáhne shodně s bary, žádná pixelová aritmetika. Výška per
  // panel (#792), zbytek geometrie je pro všechny panely shodný.
  const rangeDimFor = (panelHeight: number): React.ReactNode => {
    if (range === null || step <= 0) return null
    // Komplement sjednocení oken A a B (#489) — jas znamená „vybráno"
    const intervals = [range, ...(rangeB ? [rangeB] : [])]
      .map((target) => ({ start: target.startBucket * step, end: (target.endBucket + 1) * step }))
      .sort((a, b) => a.start - b.start)
    const rects: Array<{ x: number; w: number }> = []
    let cursor = 0
    for (const interval of intervals) {
      if (interval.start > cursor) rects.push({ x: cursor, w: interval.start - cursor })
      cursor = Math.max(cursor, interval.end)
    }
    if (cursor < width) rects.push({ x: cursor, w: width - cursor })
    return (
      <>
        {rects.map((rect, index) => (
          <rect
            key={index}
            x={rect.x}
            y={0}
            width={Math.max(0, rect.w)}
            height={panelHeight}
            fill="rgba(8,10,15,0.72)"
          />
        ))}
      </>
    )
  }
  const transform = `translate(${time.offsetX} 0) scale(${time.zoomX} 1)`

  // Vrcholy pro škály os Y; Opt Vol a Δ Flow sdílí škálu C/P (jednoznačná osa)
  const volPeak = seriesPeak(data.vol)
  const optPeak = Math.max(seriesPeak(data.optVolCall), seriesPeak(data.optVolPut))
  const flowPeak = Math.max(seriesPeak(data.deltaFlowCall), seriesPeak(data.deltaFlowPut))
  const cumPeak = seriesPeak(data.cumDelta)

  // Pohyb v panelu: crosshair (osa X) + výšková úroveň (osa Y) pro daný panel
  const handleMove =
    (key: string, panelHeight: number) => (event: React.PointerEvent<SVGSVGElement>) => {
      pointer.onPointerMove(event)
      const rect = event.currentTarget.getBoundingClientRect()
      const cssScale = rect.height > 0 ? panelHeight / rect.height : 1
      setHoverY({ key, y: (event.clientY - rect.top) * cssScale })
    }
  const handleLeave = () => {
    pointer.clear()
    setHoverY(null)
  }
  /** Hodnota na ose Y podle výšky kurzoru (signed = symetrická škála kolem nuly,
  se stejnou rezervou od okrajů jako plocha Cum Δ). */
  const axisValue = (
    key: string,
    peak: number,
    signed: boolean,
    panelHeight: number,
  ): React.ReactNode => {
    if (!hoverY || hoverY.key !== key) return null
    const y = Math.min(panelHeight, Math.max(0, hoverY.y))
    const value = signed
      ? ((panelHeight / 2 - y) / Math.max(1, panelHeight / 2 - CUM_DELTA_PAD)) * peak
      : ((panelHeight - y) / (panelHeight - 4)) * peak
    return <PanelAxisValue y={y}>{signed ? fmtSigned(value, peak) : fmtInt(value)}</PanelAxisValue>
  }
  /** Vodorovná crosshair linka na úrovni kurzoru (jen v najetém panelu, mimo transform). */
  const axisLineH = (key: string, panelHeight: number): React.ReactNode => {
    if (!hoverY || hoverY.key !== key) return null
    const y = Math.min(panelHeight, Math.max(0, hoverY.y))
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
    const height = heightOf('vol')
    const rangeDim = rangeDimFor(height)
    const heights = barHeights(data.vol, height - 4, volPeak)
    panels.push(
      <section key="vol" className="bottom-panel" aria-label="Vol panel">
        <span className="panel-title muted">Vol</span>
        {idx !== null && <PanelValue>{fmtInt(data.vol[idx])}</PanelValue>}
        {axisValue('vol', volPeak, false, height)}
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('vol', height)}
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
            {rangeDim}
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('vol', height)}
        </svg>
        {resizeHandle('vol', 'Vol')}
      </section>,
    )
  }

  if (visible.optVol) {
    const height = heightOf('optVol')
    const rangeDim = rangeDimFor(height)
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
        {axisValue('optvol', optPeak, false, height)}
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('optvol', height)}
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
            {rangeDim}
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('optvol', height)}
        </svg>
        {resizeHandle('optVol', 'Opt Vol')}
      </section>,
    )
  }

  if (visible.deltaFlow) {
    const height = heightOf('deltaFlow')
    const rangeDim = rangeDimFor(height)
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
        {axisValue('deltaflow', flowPeak, false, height)}
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('deltaflow', height)}
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
            {rangeDim}
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('deltaflow', height)}
        </svg>
        {resizeHandle('deltaFlow', 'Δ Flow C/P')}
      </section>,
    )
  }

  if (visible.evoOi && (data.evoOiCall?.length ?? 0) > 0) {
    const height = heightOf('evoOi')
    const rangeDim = rangeDimFor(height)
    const callSeries = evoOiDisplay(data.evoOiCall ?? [], evoOiMode)
    const putSeries = evoOiDisplay(data.evoOiPut ?? [], evoOiMode)
    const evoPeak = Math.max(1, seriesPeak(callSeries), seriesPeak(putSeries))
    const signed = evoOiMode === 'delta'
    const toY = signed
      ? (value: number) => height / 2 - (value / evoPeak) * (height / 2 - CUM_DELTA_PAD)
      : (value: number) => height - (value / evoPeak) * (height - 4)
    panels.push(
      <section
        key="evooi"
        className="bottom-panel"
        aria-label="Evo OI panel"
        title={
          'Evo OI (#573): vývoj celkového OI callů (teal) a putů (červená) přes striky. ' +
          'Objem roste + OI roste = pozice se budují; objem roste + OI klesá = zavírání, ' +
          'zeď se rozpouští. POZOR: OI na futures chodí přes tick 101 jen občas (ADR-0001) — ' +
          'plochá schodovitá čára znamená „bez aktualizace", ne „nic se neděje".'
        }
      >
        <span className="panel-title muted">
          Evo OI
          <button
            className={evoOiMode === 'delta' ? 'chip active' : 'chip'}
            onClick={() => setEvoOiMode(evoOiMode === 'delta' ? 'abs' : 'delta')}
            aria-label="Režim Evo OI"
            title="Δ = přírůstek od začátku osy (výchozí — absolutní OI se mění málo); abs = absolutní úroveň"
          >
            {evoOiMode === 'delta' ? 'Δ' : 'abs'}
          </button>
        </span>
        {idx !== null && (
          <PanelValue>
            <span style={{ color: COLORS.call }}>
              C {signed ? fmtSigned(callSeries[idx]) : fmtInt(callSeries[idx])}
            </span>{' '}
            <span style={{ color: COLORS.put }}>
              P {signed ? fmtSigned(putSeries[idx]) : fmtInt(putSeries[idx])}
            </span>
          </PanelValue>
        )}
        {axisValue('evooi', evoPeak, signed, height)}
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('evooi', height)}
          onPointerLeave={handleLeave}
        >
          {
            signed &&
            <line x1={0} y1={height / 2} x2={width} y2={height / 2} stroke="#2c3342" data-testid="evooi-zero" /> // prettier-ignore
          }
          <g transform={transform}>
            <path
              d={evoOiStepPath(callSeries, minutes * step, toY)}
              fill="none"
              stroke={COLORS.call}
              strokeWidth={1.5}
              vectorEffect="non-scaling-stroke"
              data-part="evooi-call"
            />
            <path
              d={evoOiStepPath(putSeries, minutes * step, toY)}
              fill="none"
              stroke={COLORS.put}
              strokeWidth={1.5}
              vectorEffect="non-scaling-stroke"
              data-part="evooi-put"
            />
            {rangeDim}
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('evooi', height)}
        </svg>
        {resizeHandle('evoOi', 'Evo OI')}
      </section>,
    )
  }

  if (visible.delta) {
    const height = heightOf('delta')
    const rangeDim = rangeDimFor(height)
    const areas = cumDeltaAreas(data.cumDelta, minutes * step, height)
    const cvdSeries = data.futuresCvd ?? []
    const cvdAt = (index: number) => cvdSeries[index] ?? null
    const cvdPoints = cvdLinePoints(cvdSeries, minutes * step, height)
    const hasCvd = cvdPoints.length > 0
    // Kotva seance (#829): kumulativ se resetuje na Globex open (22:00 UTC),
    // ale osa začíná půlnocí — na levém okraji je proto už nasčítaný tok.
    // Bez přiznání by se výchylka četla proti nule, která na grafu není.
    const anchor = data.cumDelta.length > 0 ? data.cumDelta[0] : 0
    panels.push(
      <section key="cumdelta" className="bottom-panel" aria-label="Cum Δ panel">
        <span className="panel-title muted">
          <span title="Delta-vážený tok OPČNÍCH obchodů (Σ znaménko × size × Δ × multiplikátor) — ne tok v podkladu">
            Cum Δ
          </span>
          {hasCvd && (
            <span
              className="panel-title-cvd"
              data-testid="cvd-legend"
              title="CVD podkladu: agresivní nákupy − prodeje ve futures (#829). Vlastní měřítko — čte se tvar a strana od nuly, ne výška."
            >
              {' '}
              · CVD podkladu
            </span>
          )}
          {/* Kotva seance (#829): levý okraj osy není nula — kumulativ běží
          od Globex open (22:00 UTC), zatímco osa začíná půlnocí */}
          {Math.abs(anchor) > 0.5 && (
            <span
              data-testid="cumdelta-anchor"
              title="Kumulativ se resetuje na open Globex seance (22:00 UTC), ale osa grafu začíná půlnocí — na levém okraji už je nasčítaný tok. Výchylku čtěte proti této hodnotě, ne proti nule."
            >
              {' '}
              · osa od {fmtSigned(anchor)}
            </span>
          )}
          {/* Den nezačíná od začátku seance (#518): měření běží až od startu
          enginu — decentní přiznání v legendě, detail v tooltipu */}
          {data.cumDeltaFromIso && (
            <span
              data-testid="cumdelta-from"
              title="Engine startoval uprostřed seance — tok před tímto časem chybí a nelze ho rekonstruovat"
            >
              {' '}
              · od {minuteLabel(data.cumDeltaFromIso)}
            </span>
          )}
        </span>
        {idx !== null && (
          <PanelValue>
            {fmtSigned(data.cumDelta[idx])}
            {hasCvd && cvdAt(idx) !== null && (
              <span className="panel-value-cvd"> · CVD {fmtSigned(cvdAt(idx) as number)}</span>
            )}
          </PanelValue>
        )}
        {axisValue('cumdelta', cumPeak, true, height)}
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('cumdelta', height)}
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
            {/* CVD podkladu (#829) přes plochu opčního toku — rozchod obou
            řad je informace, kterou ani jedna sama nedá */}
            {hasCvd && (
              <polyline
                points={cvdPoints}
                fill="none"
                stroke={COLORS.cvd}
                strokeWidth={1.5}
                data-part="cumdelta-cvd"
                data-testid="cumdelta-cvd"
              />
            )}
            {rangeDim}
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('cumdelta', height)}
        </svg>
        {resizeHandle('delta', 'Cum Δ')}
      </section>,
    )
  }

  // Daily pohled (#296): svíčky místo plochy — viditelný intradenní rozkmit
  // sentimentu; barvy shodné s cenovými svíčkami (SPEC 7.1)
  if (visible.sentiment && data.sentimentCandles && data.sentimentCandles.length > 0) {
    const height = heightOf('sentiment')
    const rangeDim = rangeDimFor(height)
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
        {axisValue('sentiment', peak, true, height)}
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('sentiment', height)}
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
            {rangeDim}
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('sentiment', height)}
        </svg>
        {resizeHandle('sentiment', 'Sentiment')}
      </section>,
    )
  } else if (visible.sentiment && data.sentiment && data.sentiment.length > 0) {
    const height = heightOf('sentiment')
    const rangeDim = rangeDimFor(height)
    const sentiment = data.sentiment
    const areas = cumDeltaAreas(sentiment, minutes * step, height)
    const peak = seriesPeak(sentiment)
    panels.push(
      <section key="sentiment" className="bottom-panel" aria-label="Sentiment panel">
        <span className="panel-title muted">Sentiment</span>
        {idx !== null && sentiment[idx] !== undefined && (
          <PanelValue>{sentiment[idx].toFixed(2)}</PanelValue>
        )}
        {axisValue('sentiment', peak, true, height)}
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onPointerMove={handleMove('sentiment', height)}
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
            {rangeDim}
            <CrosshairLine x={pointer.crosshairX} height={height} />
          </g>
          {axisLineH('sentiment', height)}
        </svg>
        {resizeHandle('sentiment', 'Sentiment')}
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
