/** Řádky timeframe a přepínačů vizualizace (SPEC 7.1). */
import { GEX_UNITS, GEX_UNIT_LABELS } from '../heatmap/units'
import type { GexUnits } from '../heatmap/units'
import { INTERVALS, useAppState } from '../state/AppState'
import type { NewsMarkerFilter, OiSource, SignalMode, Toggles, UnderlayPlane } from '../state/AppState' // prettier-ignore
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
  // Vrstva setupů (#399): na konci řádku vedle dropdownu Signály — skupina
  // „nápovědy". Skryje kartu i entry/cíl/stop linie; detektor běží dál
  setups: 'Setupy',
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

/** Zdroj OI (#232): měřený ranní archiv, nebo flow-adjusted odhad (opt-in). */
const OI_SOURCE_LABELS: Record<OiSource, string> = {
  measured: 'Měřené',
  fa: 'FA odhad',
}

const SIGNAL_MODE_LABELS: Record<SignalMode, string> = {
  off: 'Off',
  news: 'NEWS',
  combined: 'COMBINED',
}

const NEWS_FILTER_LABELS: Record<NewsMarkerFilter, string> = {
  all: 'Vše',
  important: 'Významné',
}

export function TogglesRow({ signalGate }: { signalGate?: SignalGateInfo | null }) {
  const {
    toggles,
    setToggle,
    signalMode,
    setSignalMode,
    underlayPlane,
    setUnderlayPlane,
    newsMarkerFilter,
    setNewsMarkerFilter,
    gexUnits,
    setGexUnits,
    oiSource,
    setOiSource,
  } = useAppState()
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
      {/* Jednotka Dyn ploch (#569) — jen zobrazovací přepočet, engine posílá $/bod */}
      {underlayPlane !== 'off' && (
        <label className="toggle">
          Jednotka
          <select
            value={gexUnits}
            onChange={(event) => setGexUnits(event.target.value as GexUnits)}
            aria-label="Jednotka Dyn plochy"
            title="$/1 % (výchozí) = Γ·OI·M·P²/100 s cenou hladiny — kolik dolarů podkladu dealeři přeobchodují při pohybu o 1 %; vyšší hladiny mají přirozeně větší váhu. $/bod = surové pole bez váhy (jednotka enginu). Týká se Dyn ploch a GEX křivky profilu; zdi, levels, flip a setupy se nemění."
          >
            {GEX_UNITS.map((value) => (
              <option key={value} value={value}>
                {GEX_UNIT_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
      )}
      {/* Zdroj OI (#232): default měřené; FA odhad je opt-in a chip má
          tečkovaný okraj — uživatel musí vždy poznat, že kouká na odhad */}
      <label className="toggle">
        OI
        <select
          className={oiSource === 'fa' ? 'fa-source-active' : undefined}
          value={oiSource}
          onChange={(event) => setOiSource(event.target.value as OiSource)}
          aria-label="Zdroj OI"
          title="Měřené = ranní OI archiv (mění se 1× denně). FA odhad = OI + α·klasifikovaný intradenní tok (ADR-0011) — vidí i dnes postavený positioning, ale je to model, ne měření. Volba se pamatuje per symbol."
        >
          {(Object.keys(OI_SOURCE_LABELS) as OiSource[]).map((value) => (
            <option key={value} value={value}>
              {OI_SOURCE_LABELS[value]}
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
      {/* Filtr news markerů (#408): jen významné zprávy (importance ≥ 2),
          ať plocha grafu nekřičí okrajovými titulky; jen když je News zapnuté */}
      {toggles.news && (
        <label className="toggle">
          <select
            value={newsMarkerFilter}
            onChange={(event) => setNewsMarkerFilter(event.target.value as NewsMarkerFilter)}
            aria-label="Filtr news markerů"
            title="Které zprávy kreslit do grafu: všechny, nebo jen významné (importance ≥ 2)"
          >
            {(Object.keys(NEWS_FILTER_LABELS) as NewsMarkerFilter[]).map((value) => (
              <option key={value} value={value}>
                {NEWS_FILTER_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
      )}
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
