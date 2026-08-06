/** Záložka Stats (#297, SPEC 9.6): statistika vln, hit-raty, stav retro passu.

Čistě analytická obrazovka — nic z ní nevstupuje do live grafu. Histogramy se
počítají klientsky nad `/stats/waves` (vlny přepočítává noční/průběžný job,
tabulka má nízké stovky řádků).
*/
import { useEffect, useMemo, useState } from 'react'
import {
  fetchNewsStats,
  fetchSignals,
  fetchSourceLatency,
  fetchTrackRecord,
  fetchWaves,
} from '../api/news'
import { categoryLabel, GATE_MIN_SAMPLES, GATE_WILSON_LB } from '../api/news'
import type {
  ModelStatsRow,
  SignalRow,
  SourceLatencyRow,
  TrackRecordRow,
  WaveRow,
} from '../api/news'
import { fetchSettings } from '../api/settings'
import { CURRENT_MECHANICS_VERSION, fetchSetups, templateLabel } from '../api/setups'
import type { SetupRow } from '../api/setups'
import {
  STRATEGY_COLORS,
  STRATEGY_LABELS,
  cagr,
  groupCurves,
  maxDrawdown,
  signalHitRate,
} from '../stats/trackrecord'
import { currentWave, histogram, waveDirectionStats } from '../stats/waves'
import { useAppState } from '../state/AppState'

const REFRESH_MS = 300_000

/** Nález drift hlídky (#403) uložený v `settings` (klíč drift_state). */
interface DriftFinding {
  kind: string
  key: string
  label: string
  symbol: string
  longterm_rate: number
  recent_rate: number
  recent_n: number
  p_value: number
}

interface DriftState {
  computed_at: string
  findings: DriftFinding[]
}

function isDriftState(value: unknown): value is DriftState {
  return (
    typeof value === 'object' && value !== null && Array.isArray((value as DriftState).findings)
  )
}

/** Stav retro passu uložený news-enginem do `settings` (klíč retro_pass). */
interface RetroPassState {
  ran_at: string
  classified: number
  reactions: number
  index_points: number
}

function isRetroState(value: unknown): value is RetroPassState {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as RetroPassState).ran_at === 'string'
  )
}

const DIRECTION_COLORS: Record<string, string> = {
  RiskOn: '#3ecf8e',
  RiskOff: '#f0616d',
}

/** Histogram jako SVG sloupce; `marker` vyznačí hodnotu aktuální vlny. */
function HistogramChart({
  values,
  color,
  marker,
  ariaLabel,
  format,
}: {
  values: number[]
  color: string
  marker?: number | null
  ariaLabel: string
  format: (value: number) => string
}) {
  const width = 260
  const height = 90
  const bins = histogram(values, 8)
  if (bins.length === 0) return <p className="muted">Zatím žádné vlny</p>
  const peak = Math.max(...bins.map((bin) => bin.count))
  const barWidth = width / bins.length
  const min = bins[0].from
  const span = Math.max(1e-9, bins[bins.length - 1].to - min)
  return (
    <svg width={width} height={height + 16} role="img" aria-label={ariaLabel}>
      {bins.map((bin, index) => {
        const barHeight = (bin.count / peak) * height
        return (
          <rect
            key={index}
            x={index * barWidth + 1}
            y={height - barHeight}
            width={barWidth - 2}
            height={barHeight}
            fill={color}
            opacity={0.7}
          >
            <title>
              {format(bin.from)}–{format(bin.to)}: {bin.count}×
            </title>
          </rect>
        )
      })}
      {marker !== null && marker !== undefined && (
        <line
          x1={((marker - min) / span) * width}
          y1={0}
          x2={((marker - min) / span) * width}
          y2={height}
          stroke="#e8c14b"
          strokeWidth={2}
          data-testid="wave-marker"
        />
      )}
      <text x={0} y={height + 12} className="stats-axis-label">
        {format(bins[0].from)}
      </text>
      <text x={width} y={height + 12} textAnchor="end" className="stats-axis-label">
        {format(bins[bins.length - 1].to)}
      </text>
    </svg>
  )
}

const WINDOWS = [1, 5, 15, 60]

/** Režimové pohledy statistik (#402) — 'all' je nepodmíněný průměr. */
const REGIME_LABELS: Record<string, string> = {
  all: 'Vše',
  RiskOn: 'Risk On',
  RiskOff: 'Risk Off',
  Neutral: 'Neutral',
  gamma_positive: 'Pozitivní gamma',
  gamma_negative: 'Negativní gamma',
}

/** Úspěšnost setup šablon per GEX režim (#402) — jen aktuální mechanika. */
function setupRegimeRows(
  setups: SetupRow[],
): { template: string; regime: string; n: number; winRate: number }[] {
  const groups = new Map<string, { template: string; regime: string; n: number; wins: number }>()
  for (const setup of setups) {
    if ((setup.mechanics_version ?? 1) !== CURRENT_MECHANICS_VERSION) continue
    if (setup.status !== 'closed_target' && setup.status !== 'closed_stop') continue
    const regime = String(setup.context?.gex_regime ?? 'neznámý')
    const key = `${setup.template}|${regime}`
    const group = groups.get(key) ?? { template: setup.template, regime, n: 0, wins: 0 }
    group.n += 1
    if (setup.status === 'closed_target') group.wins += 1
    groups.set(key, group)
  }
  return [...groups.values()]
    .map((group) => ({ ...group, winRate: group.wins / group.n }))
    .sort((a, b) =>
      a.template === b.template
        ? a.regime.localeCompare(b.regime)
        : a.template.localeCompare(b.template),
    )
}

/** Sekundy → lidský zápis: `42 s`, `4 m 14 s`, `1 h 7 m`. */
export function formatSeconds(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—'
  const total = Math.round(value)
  if (total < 60) return `${total} s`
  if (total < 3600) return `${Math.floor(total / 60)} m ${total % 60} s`
  return `${Math.floor(total / 3600)} h ${Math.floor((total % 3600) / 60)} m`
}

/** Equity a drawdown křivky + souhrn (CAGR, max DD, hit-rate) — SPEC 7.3. */
function TrackRecordSection({
  curves,
  signals,
}: {
  curves: Map<string, import('../api/news').TrackRecordRow[]>
  signals: SignalRow[]
}) {
  const width = 560
  const height = 160
  const ddHeight = 60
  const strategies = [...curves.keys()].sort()
  if (strategies.length === 0) {
    return (
      <p className="muted">Zatím prázdné — křivky počítá noční job po nasbírání historie vln</p>
    )
  }
  // Společná osa X = sjednocení dat; osy Y přes rozsah všech křivek
  const dates = [...new Set(strategies.flatMap((s) => curves.get(s)!.map((r) => r.date)))].sort()
  const dateIndex = new Map(dates.map((date, index) => [date, index]))
  const xOf = (date: string) => ((dateIndex.get(date) ?? 0) / Math.max(1, dates.length - 1)) * width
  const equities = strategies.flatMap((s) => curves.get(s)!.map((r) => r.equity))
  const minEq = Math.min(...equities)
  const maxEq = Math.max(...equities)
  const spanEq = Math.max(1e-9, maxEq - minEq)
  const yOf = (equity: number) => height - ((equity - minEq) / spanEq) * (height - 8) - 4
  const worstDd = Math.min(-1e-9, ...strategies.map((s) => maxDrawdown(curves.get(s)!)))
  const yDd = (dd: number) => (dd / worstDd) * (ddHeight - 4)

  return (
    <div>
      <div className="stats-legend">
        {strategies.map((strategy) => (
          <span key={strategy} style={{ color: STRATEGY_COLORS[strategy] ?? '#d7dce6' }}>
            ● {STRATEGY_LABELS[strategy] ?? strategy}
          </span>
        ))}
      </div>
      <svg width={width} height={height} role="img" aria-label="Equity křivky">
        <line x1={0} y1={yOf(1)} x2={width} y2={yOf(1)} stroke="#2c3342" strokeDasharray="3 3" />
        {strategies.map((strategy) => (
          <polyline
            key={strategy}
            data-part={`equity-${strategy}`}
            fill="none"
            stroke={STRATEGY_COLORS[strategy] ?? '#d7dce6'}
            strokeWidth={1.5}
            points={curves
              .get(strategy)!
              .map((row) => `${xOf(row.date).toFixed(1)},${yOf(row.equity).toFixed(1)}`)
              .join(' ')}
          />
        ))}
      </svg>
      <h4 className="muted">Drawdown</h4>
      <svg width={width} height={ddHeight} role="img" aria-label="Drawdown křivky">
        {strategies.map((strategy) => (
          <polyline
            key={strategy}
            fill="none"
            stroke={STRATEGY_COLORS[strategy] ?? '#d7dce6'}
            strokeWidth={1}
            points={curves
              .get(strategy)!
              .map((row) => `${xOf(row.date).toFixed(1)},${yDd(row.drawdown ?? 0).toFixed(1)}`)
              .join(' ')}
          />
        ))}
      </svg>
      <table className="stats-table">
        <thead>
          <tr>
            <th>Strategie</th>
            <th>Equity</th>
            <th>CAGR</th>
            <th>Max DD</th>
            <th>Hit-rate (+5 min)</th>
          </tr>
        </thead>
        <tbody>
          {strategies.map((strategy) => {
            const curve = curves.get(strategy)!
            const last = curve[curve.length - 1]
            const growth = cagr(curve)
            const mode =
              strategy === 'signals_news'
                ? ('NEWS' as const)
                : strategy === 'signals_combined'
                  ? ('COMBINED' as const)
                  : null
            const hitRate = mode ? signalHitRate(signals, mode) : null
            return (
              <tr key={strategy}>
                <td style={{ color: STRATEGY_COLORS[strategy] ?? undefined }}>
                  {STRATEGY_LABELS[strategy] ?? strategy}
                </td>
                <td>{last.equity.toFixed(3)}</td>
                <td>{growth === null ? '—' : `${(growth * 100).toFixed(1)} %`}</td>
                <td>{`${(maxDrawdown(curve) * 100).toFixed(1)} %`}</td>
                <td>
                  {hitRate === null || hitRate.total === 0
                    ? '—'
                    : `${((hitRate.hits / hitRate.total) * 100).toFixed(0)} % (${hitRate.hits}/${hitRate.total})`}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function StatsView() {
  const { symbol } = useAppState()
  const [waves, setWaves] = useState<WaveRow[]>([])
  const [stats, setStats] = useState<ModelStatsRow[]>([])
  const [retro, setRetro] = useState<RetroPassState | null>(null)
  const [track, setTrack] = useState<TrackRecordRow[]>([])
  const [signals, setSignals] = useState<SignalRow[]>([])
  const [latency, setLatency] = useState<SourceLatencyRow[]>([])
  const [windowMin, setWindowMin] = useState(5)
  const [regime, setRegime] = useState('all')
  const [setups, setSetups] = useState<SetupRow[]>([])
  const [drift, setDrift] = useState<DriftState | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      // fetchSettings hází při nedostupném API — Stats má zbytek ukázat i tak
      void Promise.all([
        fetchWaves(),
        fetchNewsStats(),
        fetchSettings().catch(() => ({}) as Record<string, unknown>),
        fetchTrackRecord(),
        fetchSignals(1000),
        fetchSourceLatency(),
      ]).then(([waveRows, statsRows, settings, trackRows, signalRows, latencyPayload]) => {
        if (cancelled) return
        setWaves(waveRows)
        setStats(statsRows)
        const retroValue = settings.retro_pass
        setRetro(isRetroState(retroValue) ? retroValue : null)
        const driftValue = settings.drift_state
        setDrift(isDriftState(driftValue) ? driftValue : null)
        setTrack(trackRows)
        setSignals(signalRows)
        setLatency(latencyPayload.latency)
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  // Setupy závisí na symbolu — vlastní efekt, aby přepnutí symbolu refetchlo tabulku (#500)
  useEffect(() => {
    let cancelled = false
    const load = () => {
      void fetchSetups(symbol).then((setupRows) => {
        if (cancelled) return
        setSetups(setupRows)
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol])

  const symbolWaves = useMemo(() => waves.filter((wave) => wave.symbol === symbol), [waves, symbol])
  const active = currentWave(symbolWaves, symbol)
  // Adaptivní práh potvrzení korekce (5.6) = průměrná hloubka RiskOff vln
  const riskOffStats = waveDirectionStats(symbolWaves, 'RiskOff')

  const bucketRows = useMemo(
    () =>
      stats
        .filter(
          (row) =>
            row.symbol === symbol &&
            row.window_min === windowMin &&
            (row.regime ?? 'all') === regime,
        )
        .sort((a, b) => b.n - a.n),
    [stats, symbol, windowMin, regime],
  )
  const setupRegime = useMemo(() => setupRegimeRows(setups), [setups])
  const driftKeys = useMemo(
    () => new Set((drift?.findings ?? []).map((finding) => finding.key)),
    [drift],
  )

  return (
    <div className="stats-view" aria-label="Statistiky">
      <section className="stats-section" aria-label="Statistika vln">
        <h2>Vlny sentimentu — {symbol}</h2>
        {active && (
          <p>
            Aktuální vlna:{' '}
            <span style={{ color: DIRECTION_COLORS[active.direction] }}>{active.direction}</span> od{' '}
            {active.start_date}, hloubka {active.depth.toFixed(2)} ({active.length_days} d).
            {riskOffStats.count > 0 && (
              <span className="muted">
                {' '}
                Práh potvrzení (Ø hloubka RiskOff): {riskOffStats.meanDepth.toFixed(2)}.
              </span>
            )}
          </p>
        )}
        <div className="stats-grid">
          {(['RiskOn', 'RiskOff'] as const).map((direction) => {
            const directionStats = waveDirectionStats(symbolWaves, direction)
            const depths = symbolWaves
              .filter((wave) => wave.direction === direction)
              .map((wave) => wave.depth)
            const lengths = symbolWaves
              .filter((wave) => wave.direction === direction)
              .map((wave) => wave.length_days)
            return (
              <div key={direction} className="stats-card">
                <h3 style={{ color: DIRECTION_COLORS[direction] }}>{direction}</h3>
                <p className="muted">
                  {directionStats.count} vln · hloubka {directionStats.meanDepth.toFixed(2)} ±{' '}
                  {directionStats.sigmaDepth.toFixed(2)} · délka Ø{' '}
                  {directionStats.meanLength.toFixed(1)} d
                </p>
                <h4 className="muted">Hloubky</h4>
                <HistogramChart
                  values={depths}
                  color={DIRECTION_COLORS[direction]}
                  marker={active?.direction === direction ? active.depth : null}
                  ariaLabel={`Histogram hloubek ${direction}`}
                  format={(value) => value.toFixed(1)}
                />
                <h4 className="muted">Délky (dny)</h4>
                <HistogramChart
                  values={lengths}
                  color={DIRECTION_COLORS[direction]}
                  marker={active?.direction === direction ? active.length_days : null}
                  ariaLabel={`Histogram délek ${direction}`}
                  format={(value) => value.toFixed(0)}
                />
              </div>
            )
          })}
        </div>
      </section>

      <section className="stats-section" aria-label="Hit-raty bucketů">
        <h2>
          Empirický model — hit-raty bucketů ({symbol},{' '}
          <label className="toggle">
            okno
            <select
              value={windowMin}
              onChange={(event) => setWindowMin(Number(event.target.value))}
              aria-label="Okno reakce"
            >
              {WINDOWS.map((value) => (
                <option key={value} value={value}>
                  +{value} min
                </option>
              ))}
            </select>
          </label>{' '}
          <label className="toggle">
            režim
            <select
              value={regime}
              onChange={(event) => setRegime(event.target.value)}
              aria-label="Režim statistik"
              title="Podmíněné pohledy (#402): tentýž vzorec se v jiném režimu chová jinak — z režimů se učíme, nezapomínáme je"
            >
              {Object.entries(REGIME_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          )
        </h2>
        <p className="muted">
          Gate signálů (6.2): n ≥ {GATE_MIN_SAMPLES} ∧ Wilson LB &gt; {GATE_WILSON_LB.toFixed(2)}.
          Zvýrazněné řádky gate splňují.
        </p>
        {bucketRows.length === 0 ? (
          <p className="muted">Žádné buckety pro tuto kombinaci</p>
        ) : (
          <table className="stats-table">
            <thead>
              <tr>
                <th>Kategorie</th>
                <th>Imp</th>
                <th>Překvapení</th>
                <th>Deferred</th>
                <th>n</th>
                <th>Ø bp</th>
                <th>Hit-rate</th>
                <th>Wilson LB</th>
              </tr>
            </thead>
            <tbody>
              {bucketRows.map((row, index) => {
                const gateOpen =
                  row.n >= GATE_MIN_SAMPLES && (row.hit_rate_lb ?? 0) > GATE_WILSON_LB
                const driftKey = `news:${row.category}|${row.importance}|${row.surprise_bucket}|${row.deferred}|${row.symbol}`
                const hasDrift = driftKeys.has(driftKey)
                return (
                  <tr key={index} className={gateOpen ? 'stats-gate-open' : undefined}>
                    <td>
                      {categoryLabel(row.category)}
                      {hasDrift && (
                        <span
                          className="stats-drift-badge"
                          title="Drift (#403): poslední výsledky se rozešly s historií — viz sekce Drift hlídka"
                        >
                          {' '}
                          ⚠
                        </span>
                      )}
                    </td>
                    <td>{row.importance}</td>
                    <td>{row.surprise_bucket}</td>
                    <td>{row.deferred ? 'ano' : '—'}</td>
                    <td>{row.n}</td>
                    <td>{row.ret_mean_bp.toFixed(1)}</td>
                    <td>{row.hit_rate === null ? '—' : `${(row.hit_rate * 100).toFixed(0)} %`}</td>
                    <td>
                      {row.hit_rate_lb === null ? '—' : `${(row.hit_rate_lb * 100).toFixed(0)} %`}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </section>

      <section className="stats-section" aria-label="Setupy per režim">
        <h2>Setupy — úspěšnost šablon per GEX režim</h2>
        <p className="muted">
          Uzavřené setupy aktuální mechaniky (v{CURRENT_MECHANICS_VERSION}) rozdělené režimem vzniku
          (#402). Tentýž vzorec se v pozitivní a negativní gamě chová jinak.
        </p>
        {setupRegime.length === 0 ? (
          <p className="muted">Zatím žádné uzavřené setupy aktuální mechaniky</p>
        ) : (
          <table className="stats-table">
            <thead>
              <tr>
                <th>Šablona</th>
                <th>Režim</th>
                <th>n</th>
                <th>Úspěšnost</th>
              </tr>
            </thead>
            <tbody>
              {setupRegime.map((row) => (
                <tr key={`${row.template}|${row.regime}`}>
                  <td>{templateLabel(row.template)}</td>
                  <td>{REGIME_LABELS[`gamma_${row.regime}`] ?? row.regime}</td>
                  <td>{row.n}</td>
                  <td>{(row.winRate * 100).toFixed(0)} %</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="stats-section" aria-label="Track record">
        <h2>Track record — mechanické equity křivky</h2>
        <p className="muted">
          Bez exekučních nákladů, point-in-time (S11); kalibrační období vyloučeno (ADR-0021).
          Sebe-kontrola systému, ne obchodní signál.
        </p>
        <TrackRecordSection curves={groupCurves(track, symbol)} signals={signals} />
      </section>

      <section className="stats-section" aria-label="Latence zdrojů">
        <h2>Latence zdrojů zpráv (7 dní)</h2>
        <p className="muted">
          ts_ingested − ts_event: zpoždění ZDROJE, ne naší cesty (event-driven od #335). Scheduled
          eventy se neměří; latence nad 6 h (staré články z prvního fetche, backfill) jdou zvlášť do
          „mimo".
        </p>
        {latency.length === 0 ? (
          <p className="muted">Zatím žádná data</p>
        ) : (
          <table className="stats-table">
            <thead>
              <tr>
                <th>Zdroj</th>
                <th>n</th>
                <th>Medián</th>
                <th>p90</th>
                <th>Dávky</th>
                <th>Mimo</th>
              </tr>
            </thead>
            <tbody>
              {latency.map((row) => (
                <tr key={row.source}>
                  <td>{row.source}</td>
                  <td>{row.n}</td>
                  <td>{formatSeconds(row.median_s)}</td>
                  <td>{formatSeconds(row.p90_s)}</td>
                  <td>
                    {row.batch_share === null ? '—' : `${Math.round(row.batch_share * 100)} %`}
                  </td>
                  <td>{row.n_over_cutoff}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="stats-section" aria-label="Drift hlídka">
        <h2>Drift hlídka</h2>
        <p className="muted">
          Noční test (#403): klouzavá úspěšnost posledních výsledků vs. dlouhodobá. Nález znamená
          „model v tomto vzorci přestává platit" — gate se zavře sám, tohle jen zkracuje dobu, po
          kterou bys věřil číslům, která už neplatí.
        </p>
        {!drift || drift.findings.length === 0 ? (
          <p className="muted">
            Žádný drift{drift ? ` (kontrola ${new Date(drift.computed_at).toLocaleString()})` : ''}
          </p>
        ) : (
          <table className="stats-table">
            <thead>
              <tr>
                <th>Model</th>
                <th>Symbol</th>
                <th>Posledních n</th>
                <th>Klouzavá</th>
                <th>Dlouhodobá</th>
                <th>p</th>
              </tr>
            </thead>
            <tbody>
              {drift.findings.map((finding) => (
                <tr key={finding.key} className="stats-drift-row">
                  <td>{finding.label}</td>
                  <td>{finding.symbol}</td>
                  <td>{finding.recent_n}</td>
                  <td>{(finding.recent_rate * 100).toFixed(0)} %</td>
                  <td>{(finding.longterm_rate * 100).toFixed(0)} %</td>
                  <td>{finding.p_value.toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="stats-section" aria-label="Retro pass">
        <h2>Ranní retro pass</h2>
        {retro === null ? (
          <p className="muted">Zatím neproběhl (běží před EU open)</p>
        ) : (
          <p>
            Naposledy {new Date(retro.ran_at).toLocaleString()} — zpracováno{' '}
            {retro.classified + retro.reactions} položek ({retro.classified} klasifikací,{' '}
            {retro.reactions} reakčních oken, {retro.index_points} bodů indexu).
          </p>
        )}
      </section>
    </div>
  )
}
