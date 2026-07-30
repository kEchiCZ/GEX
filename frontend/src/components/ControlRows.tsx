/** Řádky timeframe a přepínačů vizualizace (SPEC 7.1). */
import { INTERVALS, useAppState } from '../state/AppState'
import type { SignalMode, Toggles, UnderlayPlane } from '../state/AppState'
import type { SignalGateInfo } from '../api/news'

const TOGGLE_LABELS: Record<keyof Toggles, string> = {
  // Historicky „Dyn GEX", ale přepínač ukazuje zdi — název teď patří
  // modelované vrstvě, ať se nepletou
  dynGex: 'Zdi',
  secondaryWall: '2. zeď',
  gexLevels: 'GEX Levels',
  ladder: 'GEX žebřík',
  flowAdjusted: 'FA levels',
  sessions: 'Sessions',
  vol: 'Vol',
  optVol: 'Opt Vol',
  delta: 'Delta',
  deltaFlow: 'Δ Flow C/P',
  volOiDelta: 'Vol + OI Δ',
  projection: 'Projekce',
  news: 'News',
}

export function TimeframeRow() {
  const { timeframe, setTimeframe, interval, setInterval } = useAppState()
  return (
    <div className="row timeframe-row" role="toolbar" aria-label="Timeframe">
      {(['intraday', 'daily'] as const).map((value) => (
        <button
          key={value}
          className={timeframe === value ? 'chip active' : 'chip'}
          onClick={() => setTimeframe(value)}
        >
          {value === 'intraday' ? 'Intraday' : 'Daily'}
        </button>
      ))}
      <span className="separator" />
      {INTERVALS.map((value) => (
        <button
          key={value}
          className={interval === value ? 'chip active' : 'chip'}
          onClick={() => setInterval(value)}
          disabled={timeframe === 'daily'} // Daily: sloupec = den, intraday koše nedávají smysl
          title={timeframe === 'daily' ? 'V režimu Daily je sloupec vždy 1 den' : undefined}
        >
          {value}
        </button>
      ))}
    </div>
  )
}

/** Podkladová plocha (#204): dřív checkbox Dyn GEX, teď dropdown tří ploch. */
const PLANE_LABELS: Record<UnderlayPlane, string> = {
  off: 'Off',
  gex: 'Dyn GEX',
  charm: 'Dyn Charm',
  vanna: 'Dyn Vanna',
}

const SIGNAL_MODE_LABELS: Record<SignalMode, string> = {
  off: 'Off',
  news: 'NEWS',
  combined: 'COMBINED',
}

export function TogglesRow({ signalGate }: { signalGate?: SignalGateInfo | null }) {
  const { toggles, setToggle, signalMode, setSignalMode, underlayPlane, setUnderlayPlane } =
    useAppState()
  // „Collecting data" (SPEC 9.0): režim zapnutý, ale žádný bucket neprošel
  // Wilson gate (6.2) → místo šipek se ukazuje progres nejlepšího bucketu
  const collecting = signalMode !== 'off' && signalGate != null && signalGate.open === 0
  return (
    <div className="row toggles-row" role="toolbar" aria-label="Přepínače vizualizace">
      {/* Modelovaná podkladová vrstva (#242/#204): Dyn GEX / Charm / Vanna */}
      <label className="toggle">
        Dyn plocha
        <select
          value={underlayPlane}
          onChange={(event) => setUnderlayPlane(event.target.value as UnderlayPlane)}
          aria-label="Podkladová plocha"
          title="Modelovaná dealer expozice pod heatmapou: gamma (brzdy/plyn), charm (toky od času), vanna (toky od volatility)"
        >
          {(Object.keys(PLANE_LABELS) as UnderlayPlane[]).map((value) => (
            <option key={value} value={value}>
              {PLANE_LABELS[value]}
            </option>
          ))}
        </select>
      </label>
      {(Object.keys(TOGGLE_LABELS) as (keyof Toggles)[]).map((key) => (
        <label key={key} className="toggle">
          <input
            type="checkbox"
            checked={toggles[key]}
            onChange={(event) => setToggle(key, event.target.checked)}
          />
          {TOGGLE_LABELS[key]}
        </label>
      ))}
      {/* Dropdown režimu signálů vedle News checkboxu (#295, SPEC 9.0) */}
      <label className="toggle">
        Signály
        <select
          value={signalMode}
          onChange={(event) => setSignalMode(event.target.value as SignalMode)}
          aria-label="Režim signálů"
          title="Long/Short nápověda ze zpráv (NEWS) nebo se souhlasem GEX kontextu (COMBINED)"
        >
          {(Object.keys(SIGNAL_MODE_LABELS) as SignalMode[]).map((value) => (
            <option key={value} value={value}>
              {SIGNAL_MODE_LABELS[value]}
            </option>
          ))}
        </select>
      </label>
      {collecting && (
        <span
          className="muted signal-gate"
          data-testid="signal-gate"
          title="Žádný bucket zatím neprošel gate (n ≥ 30 ∧ Wilson LB > 0.50) — signály se objeví, až model nasbírá dost reakcí"
        >
          ⏳ sběr dat {Math.round(signalGate.progress * 100)} %
        </span>
      )}
    </div>
  )
}
