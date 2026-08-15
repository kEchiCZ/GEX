/** Statistiky deníku (#714) — sekce na obrazovce Stats.

Zásada, kterou drží celý repo (Wilson gate #453, ADR-0021): pod prahem
vzorku se NEKRESLÍ graf, který svádí k závěru — napíše se, kolik dat je.
*/
import { useEffect, useState } from 'react'
import { FAILURE_MODE_LABELS, fetchJournal, mistakeLabel } from '../api/journal'
import type { JournalEntry } from '../api/journal'
import { MACRO_LABELS, VOL_BUCKET_LABELS } from '../journal/futures'
import {
  MIN_SAMPLE,
  contextKey,
  detectorComparison,
  groupBy,
  mistakeCost,
  plannedVsRealized,
  rHistogram,
} from '../journal/stats'
import type { GroupStats } from '../journal/stats'

const SEGMENT_LABELS: Record<string, string> = {
  globex: 'Globex noc',
  premarket: 'US premarket',
  open30: 'US open +30',
  dopoledne: 'RTH dopoledne',
  poledne: 'Poledne',
  power: 'Power hour',
  close30: 'Posledních 30 min',
}

function StatsTable({
  rows,
  label,
  labelOf,
}: {
  rows: GroupStats[]
  label: string
  labelOf?: (key: string) => string
}) {
  if (rows.length === 0) return <p className="muted">Zatím žádné uzavřené obchody v tomhle řezu</p>
  return (
    <table className="stats-table">
      <thead>
        <tr>
          <th>{label}</th>
          <th>n</th>
          <th>Úspěšnost</th>
          <th>Expectancy (R)</th>
          <th>Profit factor</th>
          <th>Σ bodů</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key} className={row.small ? 'stats-small' : undefined}>
            <td>
              {labelOf?.(row.key) ?? row.key}
              {row.small && (
                <span className="muted" title={`Pod prahem ${MIN_SAMPLE} obchodů`}>
                  {' '}
                  · málo dat
                </span>
              )}
            </td>
            <td>{row.n}</td>
            <td>{(row.winRate * 100).toFixed(0)} %</td>
            <td>
              {row.expectancy >= 0 ? '+' : ''}
              {row.expectancy.toFixed(2)}
            </td>
            <td>{row.profitFactor === null ? '—' : row.profitFactor.toFixed(2)}</td>
            <td>{row.sumPoints.toFixed(1)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function JournalStats({ symbol }: { symbol: string }) {
  const [entries, setEntries] = useState<JournalEntry[]>([])

  useEffect(() => {
    void fetchJournal({ symbol, limit: 2000 }).then(setEntries)
  }, [symbol])

  const bySetup = groupBy(entries, (_, trade) => trade.setup_key)
  const bySession = groupBy(entries, (entry) => contextKey(entry, 'session_segment'))
  const byRegime = groupBy(entries, (entry) => {
    const regime = contextKey(entry, 'regime')
    if (regime === null) return null
    // Text režimu je věta — pro řez stačí znaménko gammy
    return regime.startsWith('pozitivní') ? 'pozitivní gamma' : 'negativní gamma'
  })
  const byVol = groupBy(entries, (entry) => contextKey(entry, 'vol_bucket'))
  const byMacro = groupBy(entries, (entry) => contextKey(entry, 'macro_event'))
  const byWeekday = groupBy(entries, (entry) =>
    new Date(entry.ts_ref).toLocaleDateString('cs-CZ', { weekday: 'long' }),
  )
  const byFailure = groupBy(entries, (_, trade) => trade.failure_mode)

  const mistakes = mistakeCost(entries)
  const histogram = rHistogram(entries)
  const planned = plannedVsRealized(entries)
  const detector = detectorComparison(entries)
  const maxCount = Math.max(1, ...histogram.map((bucket) => bucket.count))

  if (entries.length === 0) {
    return (
      <section className="stats-section" aria-label="Deník">
        <h2>Deník — výkonnost podle setupů a podmínek</h2>
        <p className="muted">
          Deník je zatím prázdný. Statistiky se rozjedou s prvními uzavřenými obchody.
        </p>
      </section>
    )
  }

  return (
    <>
      <section className="stats-section" aria-label="Deník per setup">
        <h2>Deník — výkonnost per setup</h2>
        <p className="muted">
          Expectancy je průměrné R na obchod — podle něj se alokuje size. Pod {MIN_SAMPLE} obchody
          je každý závěr náhoda; takové řádky jsou označené.
        </p>
        <StatsTable rows={bySetup} label="Setup" />
        {planned !== null && (
          <p className="muted">
            Plánované R:R {planned.avgPlanned.toFixed(2)} vs. realizované{' '}
            {planned.avgRealized.toFixed(2)} R ({planned.n} obchodů) — rozdíl ukazuje systematický
            optimismus v plánu.
          </p>
        )}
      </section>

      <section className="stats-section" aria-label="Deník per GEX režim">
        <h2>Deník — per GEX režim</h2>
        <p className="muted">
          Řez, který žádný komerční deník nemá: bere režim ze snímku kontextu v čase vstupu (#711),
          ne z dnešního přepočtu.
        </p>
        <StatsTable rows={byRegime} label="Režim" />
        {byFailure.length > 0 && (
          <>
            <h3>Proč teze selhala</h3>
            <p className="muted">
              `map_moved` je chyba načasování, `customer_held_wall` chyba čtení mapy — řeší se úplně
              jinak.
            </p>
            <StatsTable
              rows={byFailure}
              label="Failure mode"
              labelOf={(key) => FAILURE_MODE_LABELS[key] ?? key}
            />
          </>
        )}
      </section>

      <section className="stats-section" aria-label="Deník futures řezy">
        <h2>Deník — futures řezy</h2>
        <p className="muted">
          Typický nález: zisk v RTH, ztráta v ETH. V denním souhrnu se to schová.
        </p>
        <StatsTable rows={bySession} label="Seance" labelOf={(key) => SEGMENT_LABELS[key] ?? key} />
        <h3>Volatilitní režim</h3>
        <p className="muted">Stejný stop v bodech je v jiném režimu jiný obchod (ADR-0028).</p>
        <StatsTable rows={byVol} label="Režim" labelOf={(key) => VOL_BUCKET_LABELS[key] ?? key} />
        <h3>Makro událost</h3>
        <StatsTable
          rows={byMacro}
          label="Událost"
          labelOf={(key) => MACRO_LABELS[key as keyof typeof MACRO_LABELS] ?? key}
        />
        <h3>Den v týdnu</h3>
        <StatsTable rows={byWeekday} label="Den" />
      </section>

      <section className="stats-section" aria-label="Deník rozdělení R">
        <h2>Deník — rozdělení R</h2>
        <p className="muted">Tvar rozdělení řekne víc než průměr — koše po 0,5 R.</p>
        {histogram.length === 0 ? (
          <p className="muted">Zatím žádné uzavřené obchody</p>
        ) : (
          <table className="stats-table">
            <tbody>
              {histogram.map((bucket) => (
                <tr key={bucket.bucket}>
                  <td>
                    {bucket.bucket >= 0 ? '+' : ''}
                    {bucket.bucket.toFixed(1)} R
                  </td>
                  <td>{bucket.count}</td>
                  <td>
                    <span
                      className={bucket.bucket >= 0 ? 'stats-bar win' : 'stats-bar loss'}
                      style={{ width: `${(bucket.count / maxCount) * 100}%` }}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="stats-section" aria-label="Deník cena chyb">
        <h2>Deník — co mě která chyba stojí</h2>
        <p className="muted">
          Σ net P/L obchodů s daným tagem. Proto je číselník uzavřený — z volného textu by to
          spočítat nešlo.
        </p>
        {mistakes.length === 0 ? (
          <p className="muted">Zatím žádné otagované chyby</p>
        ) : (
          <table className="stats-table">
            <thead>
              <tr>
                <th>Chyba</th>
                <th>n</th>
                <th>Σ P/L</th>
              </tr>
            </thead>
            <tbody>
              {mistakes.map((row) => (
                <tr key={row.tag}>
                  <td>{mistakeLabel(row.tag)}</td>
                  <td>{row.n}</td>
                  <td className={row.pnl < 0 ? 'stats-loss' : undefined}>{row.pnl.toFixed(0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="stats-section" aria-label="Deník detektor vs realita">
        <h2>Deník — detektor vs. realita</h2>
        <p className="muted">
          Přímá odpověď na #627: vzal jsem nabídnutý setup, nebo ho přeskočil? Zároveň zpětná vazba
          pro kalibraci detektoru.
        </p>
        <table className="stats-table">
          <tbody>
            <tr>
              <td>Nabídl a vzal jsem</td>
              <td>{detector.taken}</td>
            </tr>
            <tr>
              <td>Nabídl a přeskočil jsem</td>
              <td>{detector.skipped}</td>
            </tr>
            <tr>
              <td>Vzal jsem bez nabídky</td>
              <td>{detector.own}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </>
  )
}
