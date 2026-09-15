/** Stats → Scénáře dne (#1173): track record vyhodnocených scénářů + poslední karty.
Brána n ≥ 30 jako u verdiktu dne — pod ní jen sběr, žádné závěry. */
import { useEffect, useState } from 'react'
import { fetchScenarioStats, fetchScenarios } from '../api/scenarios'
import type { Scenario, ScenarioStats } from '../api/scenarios'
import { ScenarioCard } from './ScenarioCard'

function pct(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 100)} %`
}

export function ScenarioStatsSection({ symbol }: { symbol: string }) {
  const [stats, setStats] = useState<ScenarioStats | null>(null)
  const [rows, setRows] = useState<Scenario[]>([])
  useEffect(() => {
    let cancelled = false
    void Promise.all([fetchScenarioStats(symbol), fetchScenarios(symbol, 10)]).then(
      ([statsRow, items]) => {
        if (cancelled) return
        setStats(statsRow)
        setRows(items)
      },
    )
    return () => {
      cancelled = true
    }
  }, [symbol])
  return (
    <section className="stats-section" aria-label="Scénáře dne">
      <h2>Scénáře dne — track record (#1173)</h2>
      <p className="muted">
        Nakreslená očekávaná cesta se po termínu porovná se skutečností: zásah cíle 1 a 2 v pořadí,
        největší odchylka close od cesty (body i EM). Závěr až od n ≥ 30 — do té doby jen sběr;
        scénáře vznikají výhradně dopředu.
      </p>
      {stats === null ? (
        <p className="muted">Statistiky nejsou k dispozici.</p>
      ) : (
        <table className="briefing-table" data-testid="scenario-stats">
          <thead>
            <tr>
              <th>n</th>
              <th>cíl 1</th>
              <th>cíl 2 (z n₂)</th>
              <th>pořadí (z n₀)</th>
              <th>medián odchylky</th>
              <th>stav</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>{stats.n}</td>
              <td>{pct(stats.hit1_rate)}</td>
              <td>
                {pct(stats.hit2_rate)} ({stats.n_second})
              </td>
              <td>
                {pct(stats.order_rate)} ({stats.n_order})
              </td>
              <td>{stats.median_dev_em === null ? '—' : `${stats.median_dev_em.toFixed(2)} EM`}</td>
              <td>{stats.preliminary ? `sběr (${stats.n}/30)` : 'platné'}</td>
            </tr>
          </tbody>
        </table>
      )}
      {rows.map((row) => (
        <ScenarioCard key={row.id} scenario={row} compact />
      ))}
    </section>
  )
}
