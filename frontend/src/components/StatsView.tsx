/** Záložka Stats (#297, SPEC 9.6): statistika vln, hit-raty, stav retro passu.

Čistě analytická obrazovka — nic z ní nevstupuje do live grafu. Histogramy se
počítají klientsky nad `/stats/waves` (vlny přepočítává noční/průběžný job,
tabulka má nízké stovky řádků).
*/
import { useEffect, useMemo, useState } from 'react'
import { fetchNewsStats, fetchWaves } from '../api/news'
import { categoryLabel, GATE_MIN_SAMPLES, GATE_WILSON_LB } from '../api/news'
import type { ModelStatsRow, WaveRow } from '../api/news'
import { fetchSettings } from '../api/settings'
import { currentWave, histogram, waveDirectionStats } from '../stats/waves'
import { useAppState } from '../state/AppState'

const REFRESH_MS = 300_000

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

export function StatsView() {
  const { symbol } = useAppState()
  const [waves, setWaves] = useState<WaveRow[]>([])
  const [stats, setStats] = useState<ModelStatsRow[]>([])
  const [retro, setRetro] = useState<RetroPassState | null>(null)
  const [windowMin, setWindowMin] = useState(5)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      // fetchSettings hází při nedostupném API — Stats má zbytek ukázat i tak
      void Promise.all([
        fetchWaves(),
        fetchNewsStats(),
        fetchSettings().catch(() => ({}) as Record<string, unknown>),
      ]).then(([waveRows, statsRows, settings]) => {
        if (cancelled) return
        setWaves(waveRows)
        setStats(statsRows)
        const retroValue = settings.retro_pass
        setRetro(isRetroState(retroValue) ? retroValue : null)
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  const symbolWaves = useMemo(() => waves.filter((wave) => wave.symbol === symbol), [waves, symbol])
  const active = currentWave(symbolWaves, symbol)
  // Adaptivní práh potvrzení korekce (5.6) = průměrná hloubka RiskOff vln
  const riskOffStats = waveDirectionStats(symbolWaves, 'RiskOff')

  const bucketRows = useMemo(
    () =>
      stats
        .filter((row) => row.symbol === symbol && row.window_min === windowMin)
        .sort((a, b) => b.n - a.n),
    [stats, symbol, windowMin],
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
                return (
                  <tr key={index} className={gateOpen ? 'stats-gate-open' : undefined}>
                    <td>{categoryLabel(row.category)}</td>
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
