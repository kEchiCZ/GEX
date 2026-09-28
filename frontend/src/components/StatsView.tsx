/** Záložka Stats (#297, SPEC 9.6): statistika vln, hit-raty, stav retro passu.

Čistě analytická obrazovka — nic z ní nevstupuje do live grafu. Histogramy se
počítají klientsky nad `/stats/waves` (vlny přepočítává noční/průběžný job,
tabulka má nízké stovky řádků).
*/
import { useEffect, useMemo, useState } from 'react'
import {
  fetchEpisodes,
  fetchNewsStats,
  fetchSignals,
  fetchSourceLatency,
  fetchTrackRecord,
  fetchWaves,
} from '../api/news'
import { categoryLabel } from '../api/news'
import type {
  EpisodeRow,
  GateThresholds,
  ModelStatsRow,
  SignalRow,
  SourceLatencyRow,
  TrackRecordRow,
  WaveRow,
} from '../api/news'
import { fetchSettings } from '../api/settings'
import { evTooltip, formatPnlUsd, templateLabel } from '../api/setups'
import type { SetupsSummary, SharpeValue } from '../api/setups'
import { useSetupsSummary } from '../hooks/useSetupsSummary'
import { fetchVerdictStats } from '../api/briefing'
import { ReleaseHypothesesSection } from './ReleaseHypothesesSection'
import { ScenarioStatsSection } from './ScenarioStatsSection'
import type { VerdictStatBucket, VerdictStats } from '../api/briefing'
import { API_BASE } from '../config'
import {
  STRATEGY_COLORS,
  STRATEGY_LABELS,
  cagr,
  groupCurves,
  maxDrawdown,
  signalHitRate,
} from '../stats/trackrecord'
import { currentWave, depthReading, histogram, waveDirectionStats } from '../stats/waves'
import { episodeLabelText, episodeStats, recentEpisodes } from '../stats/episodes'
import { NewsVolSection } from './NewsVolSection'
import { useAppState } from '../state/AppState'
import { JournalStats } from './JournalStats'

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

/** Výkon setupů (#794 fáze 0, ADR-0030): Sharpe, equity R + USD simulace.

Portfolio = uzavřené setupy aktuální mechaniky přes VŠECHNY symboly watchlistu
(rovným dílem 1R). Sharpe je anualizovaný z denních ΣR per seance; do ~60
seancí je to ukazatel trendu, ne splněný cíl (potvrzení SR > 2 chce 400+
seancí — SE ≈ √252/√N). USD větev simuluje exekuci micro kontrakty (#679)
včetně nákladů a přeskočených obchodů. Vše počítá server z celé historie
(#1319) — sekce jen vykresluje. */
function SetupsPerformanceSection({
  summary,
  failed,
  stale,
}: {
  summary: SetupsSummary | null
  failed: boolean
  /** Souhrn předchozí simulace týchž symbolů — nový se načítá (#1319). */
  stale: boolean
}) {
  if (failed) {
    return (
      <p className="muted" role="alert">
        Souhrn setupů se nepodařilo načíst ze serveru (API nebo databáze).
      </p>
    )
  }
  if (summary === null) return <p className="muted">Načítám…</p>
  const perf = summary.performance
  const curve = perf.daily
  if (curve.length === 0) {
    return <p className="muted">Zatím žádné uzavřené setupy aktuální mechaniky</p>
  }
  const totalR = curve[curve.length - 1].cum_r
  const trades = curve.reduce((sum, point) => sum + point.trades, 0)
  // EV na obchod (#911) v R: identické s Ø R, karta ukazuje ROZKLAD složek
  const ev = summary.all.ev_r
  const usd = perf.simulation
  // Bilance účtu ze serverového sizingu (#1185): jen obchodovatelné setupy,
  // kontrakty × R × stop × bod − poplatky; null před prvním setupem s pravidly
  const account = summary.account

  const width = 560
  const height = 160
  const equities = curve.map((point) => point.cum_r)
  const minEq = Math.min(0, ...equities)
  const maxEq = Math.max(0, ...equities)
  const spanEq = Math.max(1e-9, maxEq - minEq)
  const xOf = (index: number) => (index / Math.max(1, curve.length - 1)) * width
  const yOf = (equity: number) => height - ((equity - minEq) / spanEq) * (height - 8) - 4
  const formatSharpe = (result: SharpeValue) =>
    result.sharpe === null ? '—' : result.sharpe.toFixed(2)
  const sharpeAll = perf.sharpe_all

  return (
    <div className={stale ? 'summary-stale' : undefined} aria-busy={stale}>
      <div className="stats-grid">
        <div className="stats-card">
          <h3>Sharpe (anualiz.)</h3>
          <p>
            {formatSharpe(sharpeAll)}{' '}
            <span className="muted">
              celkem · {formatSharpe(perf.sharpe_30)} posledních 30 seancí
            </span>
          </p>
        </div>
        <div className="stats-card">
          <h3>Bilance</h3>
          <p data-testid="stats-balance">
            {totalR > 0 ? '+' : ''}
            {totalR.toFixed(1)} R{' '}
            <span className="muted">
              · {sharpeAll.days} seancí · {trades} obchodů · max DD {perf.max_drawdown_r.toFixed(1)}{' '}
              R
            </span>
          </p>
        </div>
        <div className="stats-card">
          <h3>EV / obchod (#911)</h3>
          {ev ? (
            <p data-testid="stats-ev" className="stats-ev" title={evTooltip(ev, 'R')}>
              {ev.ev >= 0 ? '+' : ''}
              {ev.ev.toFixed(2)} R{' '}
              <span className="muted">
                = {Math.round(100 * ev.win_rate)} % × {ev.avg_win.toFixed(2)} R −{' '}
                {Math.round(100 * ev.loss_rate)} % × {ev.avg_loss.toFixed(2)} R · n={ev.n} ·{' '}
                {ev.ev >= 0 ? 'dlouhodobě vydělává' : 'dlouhodobě ztrácí'}
              </span>
            </p>
          ) : (
            <p className="muted">Zatím žádné uzavřené obchody</p>
          )}
        </div>
        <div className="stats-card">
          <h3>Všechny setupy — 1 kontrakt</h3>
          <p
            data-testid="stats-one-contract"
            title={[
              'Každý uzavřený setup rovným dílem 1 kontrakt (obchodovatelný i stínový):',
              '• hrubě = R × stop × hodnota bodu',
              `• poplatky = ${summary.fee_per_contract_usd} $ za kontrakt a obchod`,
            ].join('\n')}
          >
            {formatPnlUsd(summary.all.net_usd)}{' '}
            <span className="muted">
              čistě · hrubě {formatPnlUsd(summary.all.gross_usd)} · poplatky{' '}
              {Math.round(summary.all.fees_usd)} $
            </span>
          </p>
        </div>
        <div className="stats-card">
          <h3>USD simulace (#679)</h3>
          {usd ? (
            <p>
              {formatPnlUsd(usd.total_usd)}{' '}
              <span className="muted">
                · Sharpe {formatSharpe(usd.sharpe)} · {usd.traded} obchodů
                {usd.skipped > 0 ? ` · ${usd.skipped} přeskočeno (0 kontraktů)` : ''}
              </span>
            </p>
          ) : (
            <p className="muted">Vyplň účet a % rizika v Settings → Trading</p>
          )}
        </div>
        <div className="stats-card">
          <h3>Účet {Math.round(summary.account_usd / 1000)}k — obchodovatelné (#1185)</h3>
          {account && account.trades > 0 ? (
            <p
              data-testid="stats-account"
              title={[
                'Co by reálně vydělal účet: serverový sizing (1 % rizika, brzdy, brána šablon).',
                '• jen setupy označené jako obchodovatelné',
                '• reálně na MES/MNQ jsou dolary ÷ 10',
              ].join('\n')}
            >
              {formatPnlUsd(account.net_usd)}{' '}
              <span className="muted">
                · {account.trades} obchodů · stín {summary.shadow.count} · poplatky{' '}
                {Math.round(account.fees_usd)} $ · max DD {Math.round(account.max_drawdown_usd)} $
              </span>
            </p>
          ) : (
            <p className="muted">
              {account
                ? `Zatím bez uzavřeného obchodovatelného setupu (stín ${summary.shadow.count})`
                : 'Zatím žádný setup s risk pravidly'}
            </p>
          )}
        </div>
      </div>
      <svg width={width} height={height} role="img" aria-label="Equity setupů v R">
        <line x1={0} y1={yOf(0)} x2={width} y2={yOf(0)} stroke="#3a4150" strokeDasharray="4 4" />
        <polyline
          fill="none"
          stroke="#4cc38a"
          strokeWidth={1.5}
          points={curve.map((point, index) => `${xOf(index)},${yOf(point.cum_r)}`).join(' ')}
        />
      </svg>
      <p className="muted">
        {curve[0].session} – {curve[curve.length - 1].session} · mechanika v
        {summary.mechanics_version} · rovným dílem 1R na setup, symboly {summary.symbols.join(', ')}{' '}
        · celá historie.
        {sharpeAll.days < 60
          ? ` Vzorek ${sharpeAll.days} seancí je na Sharpe MALÝ — číslo je orientační;`
          : ''}{' '}
        statistické potvrzení cíle SR &gt; 2 vyžaduje 400+ seancí (ADR-0030).
      </p>
    </div>
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
/** Track record verdiktů dne (#1091): tabulky per verdikt a per složka hlasování. */
function VerdictStatsSection({ stats }: { stats: VerdictStats | null }) {
  if (stats === null) return <p className="muted">Načítám…</p>
  if (stats.evaluated === 0) {
    return (
      <p className="muted">Zatím žádná vyhodnocená seance — výsledek doplní engine po settle.</p>
    )
  }
  const pct = (value: number | null) => (value === null ? '—' : `${Math.round(value * 100)} %`)
  const row = (name: string, bucket: VerdictStatBucket) => (
    <tr key={name} className={bucket.gate_open ? undefined : 'muted'}>
      <td>{name}</td>
      <td>{bucket.n}</td>
      <td>{pct(bucket.hit_rate)}</td>
      <td>{pct(bucket.wilson_lb)}</td>
      <td>{bucket.gate_open ? 'ano' : `sběr (${bucket.n}/${stats.min_samples})`}</td>
    </tr>
  )
  return (
    <div className="verdict-stats" data-testid="verdict-stats">
      <table className="briefing-table">
        <thead>
          <tr>
            <th>verdikt</th>
            <th>n</th>
            <th>zásah</th>
            <th>Wilson LB</th>
            <th>brána</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(stats.by_verdict).map(([name, bucket]) =>
            row(VERDICT_NAMES[name] ?? name, bucket),
          )}
        </tbody>
      </table>
      <table className="briefing-table">
        <thead>
          <tr>
            <th>složka hlasování</th>
            <th>n</th>
            <th>souhlas se seancí</th>
            <th>Wilson LB</th>
            <th>brána</th>
          </tr>
        </thead>
        <tbody>{Object.entries(stats.by_vote).map(([name, bucket]) => row(name, bucket))}</tbody>
      </table>
    </div>
  )
}

const VERDICT_NAMES: Record<string, string> = {
  long: 'spíše long',
  short: 'spíše short',
  none: 'bez převahy',
  wait_news: 'počkat na tisk',
}

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
  const { symbol, riskAccountUsd, riskPct } = useAppState()
  const [waves, setWaves] = useState<WaveRow[]>([])
  // Korekční epizody (#565) — per symbol, plní WavesJob
  const [episodes, setEpisodes] = useState<EpisodeRow[]>([])
  const [stats, setStats] = useState<ModelStatsRow[]>([])
  const [gate, setGate] = useState<GateThresholds | null>(null)
  const [retro, setRetro] = useState<RetroPassState | null>(null)
  const [track, setTrack] = useState<TrackRecordRow[]>([])
  const [signals, setSignals] = useState<SignalRow[]>([])
  const [latency, setLatency] = useState<SourceLatencyRow[]>([])
  const [windowMin, setWindowMin] = useState(5)
  const [regime, setRegime] = useState('all')
  // Portfolio pro sekci Výkon (#794): symboly watchlistu — Sharpe se dle
  // ADR-0030 počítá nad celou simulací, ne per aktivní symbol
  const [watchlistSymbols, setWatchlistSymbols] = useState<string[]>([])
  const [drift, setDrift] = useState<DriftState | null>(null)
  const [verdictStats, setVerdictStats] = useState<VerdictStats | null>(null)

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
        fetchVerdictStats(),
      ]).then(
        ([waveRows, statsRows, settings, trackRows, signalRows, latencyPayload, verdicts]) => {
          // prettier-ignore
          if (cancelled) return
          setVerdictStats(verdicts)
          setWaves(waveRows)
          setStats(statsRows.rows)
          setGate(statsRows.gate)
          const retroValue = settings.retro_pass
          setRetro(isRetroState(retroValue) ? retroValue : null)
          const driftValue = settings.drift_state
          setDrift(isDriftState(driftValue) ? driftValue : null)
          setTrack(trackRows)
          setSignals(signalRows)
          setLatency(latencyPayload.latency)
        },
      )
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  // Epizody (#565) závisí na symbolu — vlastní efekt, aby přepnutí symbolu
  // refetchlo tabulky (#500)
  useEffect(() => {
    let cancelled = false
    const load = () => {
      void fetchEpisodes(symbol).then((episodeRows) => {
        if (!cancelled) setEpisodes(episodeRows)
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol])

  // Portfolio Výkonu (#794): symboly z watchlistu; při nedostupném watchlistu
  // aspoň aktivní symbol, ať sekce neukazuje prázdno kvůli vedlejší chybě
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const response = await fetch(`${API_BASE}/watchlist`)
        if (!response.ok) return
        const payload = (await response.json()) as { watchlist?: { symbol: string }[] }
        if (!cancelled) setWatchlistSymbols((payload.watchlist ?? []).map((item) => item.symbol))
      } catch {
        // watchlist nedostupný — portfolio drží aktivní symbol (viz níže)
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])
  const portfolioSymbols = useMemo(
    () => [...new Set([...watchlistSymbols, symbol])].sort(),
    [watchlistSymbols, symbol],
  )
  // Souhrny setupů z CELÉ historie počítá server (#1319): portfolio pro Výkon
  // (s USD simulací z kalkulačky) a aktivní symbol pro režimovou tabulku
  const portfolio = useSetupsSummary(portfolioSymbols, {
    simulation: { accountUsd: riskAccountUsd, riskPct },
    pollMs: REFRESH_MS,
  })
  const symbolSummary = useSetupsSummary([symbol], { pollMs: REFRESH_MS })
  const setupRegime = symbolSummary.summary?.regimes ?? []

  const symbolWaves = useMemo(() => waves.filter((wave) => wave.symbol === symbol), [waves, symbol])
  const active = currentWave(symbolWaves, symbol)
  const episodeSummary = useMemo(() => episodeStats(episodes), [episodes])
  const lastEpisodes = useMemo(() => recentEpisodes(episodes, 10), [episodes])
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
            {active.start_date}, hloubka{' '}
            {active.depth_z != null
              ? `${active.depth_z.toFixed(2)} σ (surově ${active.depth.toFixed(2)})`
              : active.depth.toFixed(2)}{' '}
            ({active.length_days} d).
            {riskOffStats.count > 0 && (
              <span className="muted">
                {' '}
                Práh potvrzení (Ø hloubka RiskOff): {riskOffStats.meanDepth.toFixed(2)}
                {riskOffStats.inSigma ? ' σ' : ''}.
              </span>
            )}
          </p>
        )}
        <div className="stats-grid">
          {(['RiskOn', 'RiskOff'] as const).map((direction) => {
            const directionStats = waveDirectionStats(symbolWaves, direction)
            // Hloubky v σ (#640): éry řady mají různá měřítka, σ je sjednocuje —
            // ale jen když σ mají všechny vlny (míchat jednotky nelze)
            const reading = depthReading(symbolWaves)
            const depths = symbolWaves
              .filter((wave) => wave.direction === direction)
              .map(reading.value)
            const lengths = symbolWaves
              .filter((wave) => wave.direction === direction)
              .map((wave) => wave.length_days)
            const activeDepth = active?.direction === direction ? reading.value(active) : null
            return (
              <div key={direction} className="stats-card">
                <h3 style={{ color: DIRECTION_COLORS[direction] }}>{direction}</h3>
                <p className="muted">
                  {directionStats.count} vln · hloubka {directionStats.meanDepth.toFixed(2)}
                  {directionStats.inSigma ? ' σ' : ''} ± {directionStats.sigmaDepth.toFixed(2)} ·
                  délka Ø {directionStats.meanLength.toFixed(1)} d
                </p>
                <h4 className="muted">Hloubky{reading.inSigma ? ' (v σ škály, #640)' : ''}</h4>
                <HistogramChart
                  values={depths}
                  color={DIRECTION_COLORS[direction]}
                  marker={activeDepth}
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

      {/* Korekční epizody SentIndexu (#565, ADR-0037) — vrstva vedle vln */}
      <section className="stats-section" aria-label="Korekční epizody">
        <h2>Korekční epizody sentimentu — {symbol}</h2>
        <p className="muted">
          Epizoda = pokles denního close_z pod 20denní maximum o ≥ 1 σ; <b>pokus</b> = zahlazeno
          (zpět nad maximum) do 10 obchodních dní, <b>negace</b> = korekce pokračuje.{' '}
          {episodeSummary.preliminary && (
            <span className="tendency-uncalibrated" data-testid="episodes-preliminary">
              předběžné ({episodeSummary.resolved} rozhodnutých, kalibrace od 20; parametry v
              {episodeSummary.paramsVersion ?? 1} = placeholder z měření 14. 9. 2026)
            </span>
          )}
        </p>
        {episodes.length === 0 ? (
          <p className="muted">Zatím žádná epizoda (řada potřebuje σ škálu a 20 dní historie).</p>
        ) : (
          <>
            <div className="stats-grid">
              {(['attempts', 'negations'] as const).map((key) => {
                const block = episodeSummary[key]
                const color =
                  key === 'attempts' ? DIRECTION_COLORS.RiskOn : DIRECTION_COLORS.RiskOff
                return (
                  <div key={key} className="stats-card">
                    <h3 style={{ color }}>{key === 'attempts' ? 'Pokusy' : 'Negace'}</h3>
                    <p className="muted">
                      {block.count} epizod · hloubka Ø {block.meanDepth.toFixed(2)} σ · délka Ø{' '}
                      {block.meanLength.toFixed(1)} d
                    </p>
                  </div>
                )
              })}
            </div>
            {episodeSummary.open > 0 && (
              <p className="muted">Probíhá: {episodeSummary.open} (třída až po rozhodnutí).</p>
            )}
            <table className="stats-table" data-testid="episodes-table">
              <thead>
                <tr>
                  <th>Start</th>
                  <th>Rozhodnutí</th>
                  <th>Třída</th>
                  <th>Hloubka σ</th>
                  <th>Dní</th>
                  <th>Ref. σ</th>
                </tr>
              </thead>
              <tbody>
                {lastEpisodes.map((row) => (
                  <tr key={row.id}>
                    <td>{row.start_date}</td>
                    <td>{row.end_date ?? '—'}</td>
                    <td>{episodeLabelText(row.label)}</td>
                    <td>{row.depth_z.toFixed(2)}</td>
                    <td>{row.length_days}</td>
                    <td>{row.ref_level_z.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>

      {/* Index volatility zpráv (#567): velikost reakcí — směr říká SentIndex */}
      <NewsVolSection symbol={symbol} />
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
          Gate signálů (6.2)
          {gate &&
            `: n ≥ ${gate.min_samples} ∧ Wilson LB > ${gate.wilson_lb.toFixed(2)} ∧ |Ø bp| ≥ ${gate.min_effect_bp}`}
          . Zvýrazněné řádky gate splňují.
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
                const gateOpen = row.gate_open
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

      <section className="stats-section" aria-label="Výkon setupů">
        <h2>Setupy — výkon a Sharpe (#794 fáze 0)</h2>
        <SetupsPerformanceSection
          summary={portfolio.summary}
          failed={portfolio.failed}
          stale={portfolio.stale}
        />
      </section>

      <section className="stats-section" aria-label="Setupy per režim">
        <h2>Setupy — úspěšnost šablon per GEX režim</h2>
        <p className="muted">
          Uzavřené setupy aktuální mechaniky
          {symbolSummary.summary ? ` (v${symbolSummary.summary.mechanics_version})` : ''} z celé
          historie, rozdělené režimem vzniku (#402); jen cíl vs. stop, timeouty mimo. Tentýž vzorec
          se v pozitivní a negativní gamě chová jinak.
        </p>
        {symbolSummary.failed ? (
          <p className="muted" role="alert">
            Souhrn setupů se nepodařilo načíst ze serveru (API nebo databáze).
          </p>
        ) : setupRegime.length === 0 ? (
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
                  <td>{(row.win_rate * 100).toFixed(0)} %</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <JournalStats symbol={symbol} />

      {/* Verdikt dne (#1091): track record heuristiky z Briefingu — per verdikt
          i per složka hlasování; pod branou n ≥ 30 jen počty, žádné závěry */}
      <section className="stats-section" aria-label="Verdikt dne">
        <h2>Verdikt dne — track record (#1091)</h2>
        <p className="muted">
          Zásah verdiktu z Briefingu proti pohybu US open → settle (bez převahy = close do ±0,5 EM).
          Složka „nese informaci", když její hlas souhlasí se směrem seance. Brána n ≥{' '}
          {verdictStats?.min_samples ?? 30} jako u signálů; do té doby jen sběr.
        </p>
        <VerdictStatsSection stats={verdictStats} />
      </section>

      <ScenarioStatsSection symbol={symbol} />

      <ReleaseHypothesesSection />

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
