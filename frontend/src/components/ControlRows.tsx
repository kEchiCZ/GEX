/** Řádky timeframe a přepínačů vizualizace (SPEC 7.1). */
import { INTERVALS, useAppState } from '../state/AppState'
import type { Toggles } from '../state/AppState'

const TOGGLE_LABELS: Record<keyof Toggles, string> = {
  dynGex: 'Dyn GEX',
  gexLevels: 'GEX Levels',
  sessions: 'Sessions',
  vol: 'Vol',
  optVol: 'Opt Vol',
  delta: 'Delta',
  deltaFlow: 'Δ Flow C/P',
  volOiDelta: 'Vol + OI Δ',
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

export function TogglesRow() {
  const { toggles, setToggle } = useAppState()
  return (
    <div className="row toggles-row" role="toolbar" aria-label="Přepínače vizualizace">
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
    </div>
  )
}
