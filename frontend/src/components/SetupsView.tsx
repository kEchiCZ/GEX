/** Obrazovka Setupy (ADR-0004): historie analýz s výsledky a ručním hodnocením.

Predikce jsou neměnné — jediná mutace je rating (+1/−1) a poznámka; hodnocení
je kvalitativní vrstva a nevstupuje do automatické kalibrace confidence.
*/
import { useState } from 'react'
import { ACCOUNT_START_USD, CURRENT_MECHANICS_VERSION, STATUS_LABELS, dailyStats, formatPct, formatPnlUsd, reviewSetup, setupPnlPct, setupPnlUsd, setupRrr, templateLabel } from '../api/setups' // prettier-ignore
import { sessionDateIso } from '../instrument/tz'
import type { SetupRow } from '../api/setups'
import { formatLevel } from '../heatmap/overlays'
import { useSetups } from '../hooks/useSetups'
import { pointValue } from '../instrument/tick'
import { useAppState } from '../state/AppState'
import { usePersistentState } from '../state/persist'

function formatTs(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString()
}

function ReviewCell({
  row,
  symbol,
  onSaved,
}: {
  row: SetupRow
  symbol: string
  onSaved: () => void
}) {
  const [note, setNote] = useState(row.user_note ?? '')
  const [saving, setSaving] = useState(false)

  const save = async (rating: 1 | -1 | null) => {
    setSaving(true)
    const ok = await reviewSetup(symbol, row.id, rating, note.trim() === '' ? null : note.trim())
    setSaving(false)
    if (ok) onSaved()
  }

  if (row.status === 'active') return <span className="muted">běží</span>
  return (
    <div className="setup-review">
      <button
        className={row.user_rating === 1 ? 'chip active' : 'chip'}
        aria-label={`Setup ${row.id} vyšel`}
        title="Setup vyšel podle predikce"
        disabled={saving}
        onClick={() => void save(row.user_rating === 1 ? null : 1)}
      >
        👍
      </button>
      <button
        className={row.user_rating === -1 ? 'chip active' : 'chip'}
        aria-label={`Setup ${row.id} nevyšel`}
        title="Setup nevyšel / byl zavádějící"
        disabled={saving}
        onClick={() => void save(row.user_rating === -1 ? null : -1)}
      >
        👎
      </button>
      <input
        value={note}
        placeholder="Poznámka"
        aria-label={`Poznámka k setupu ${row.id}`}
        maxLength={500}
        onChange={(event) => setNote(event.target.value)}
        onBlur={() => {
          if ((row.user_note ?? '') !== note.trim()) void save((row.user_rating as 1 | -1) ?? null)
        }}
      />
    </div>
  )
}

export function SetupsView() {
  const { symbol } = useAppState()
  const { setups, refresh } = useSetups()
  // Statistiky defaultně jen z aktuální mechaniky (#311) — setupy staré verze
  // mají jinou sémantiku stopů a cílů (Ø RRR 25–47), míchat je do jedné bilance
  // by znamenalo počítat výkonnost systému, který už neexistuje
  const [allVersions, setAllVersions] = usePersistentState<boolean>(
    'setupsAllVersions',
    false,
    (value) => (typeof value === 'boolean' ? value : false),
  )
  const legacyCount = setups.filter(
    (row) => (row.mechanics_version ?? 1) !== CURRENT_MECHANICS_VERSION,
  ).length
  const visible = allVersions
    ? setups
    : setups.filter((row) => (row.mechanics_version ?? 1) === CURRENT_MECHANICS_VERSION)

  const closed = visible.filter((row) => row.status !== 'active')
  const wins = closed.filter((row) => (row.outcome_r ?? 0) > 0).length
  const totalR = closed.reduce((sum, row) => sum + (row.outcome_r ?? 0), 0)
  // P/L v USD na 1 kontrakt (#185) — CME hodnota bodu instrumentu
  const pointUsd = pointValue(symbol)
  const totalPnl = closed.reduce((sum, row) => sum + (setupPnlUsd(row, pointUsd) ?? 0), 0)
  // % P/L vůči startovnímu účtu 5 000 $ na ticker (#191) — s fixní bází je
  // součet procent setupů roven celkovému zhodnocení účtu
  const totalPct = (totalPnl / ACCOUNT_START_USD) * 100
  const averageR = closed.length > 0 ? totalR / closed.length : 0
  const pnlClass = totalPnl >= 0 ? 'r-positive' : 'r-negative'
  // Bilance dnešní seance (#748) — nad `visible`, aby ctila přepínač verze
  // mechaniky; jinak by si horní a spodní blok odporovaly
  const day = dailyStats(visible, pointUsd, sessionDateIso(), sessionDateIso)

  return (
    <section className="setups-view" aria-label="Setupy">
      <header className="setups-summary">
        <h2>Setupy — {symbol}</h2>
        {legacyCount > 0 && (
          <label className="setups-version-toggle">
            <input
              type="checkbox"
              checked={allVersions}
              onChange={(event) => setAllVersions(event.target.checked)}
            />
            Včetně starší mechaniky ({legacyCount})
          </label>
        )}
      </header>
      {/* Zvýrazněné souhrnné statistiky (#189); P/L vždy na 1 kontrakt */}
      <div className="setups-stats" role="group" aria-label="Souhrnné statistiky">
        <div className="stat">
          <span className="stat-label muted">Aktivní</span>
          <span className="stat-value">{visible.length - closed.length}</span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Uzavřené</span>
          <span className="stat-value">{closed.length}</span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Úspěšnost</span>
          <span className="stat-value">
            {closed.length > 0 ? `${Math.round((wins / closed.length) * 100)} %` : '—'}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Ø R</span>
          <span className={`stat-value ${averageR >= 0 ? 'r-positive' : 'r-negative'}`}>
            {closed.length > 0 ? `${averageR >= 0 ? '+' : ''}${averageR.toFixed(2)}` : '—'}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Σ P/L (1 kontrakt)</span>
          <span className={`stat-value ${pnlClass}`} data-testid="setups-total-pnl">
            {closed.length > 0 ? formatPnlUsd(totalPnl) : '—'}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">% P/L (účet 5k)</span>
          <span className={`stat-value ${pnlClass}`} data-testid="setups-total-pct">
            {closed.length > 0 ? formatPct(totalPct) : '—'}
          </span>
        </div>
      </div>
      {/* Bilance dnešní SEANCE (#748) — oddělená od celkové historie výše.
          Den je Globex seance (#512), ne kalendářní datum. */}
      <div className="setups-stats setups-stats-day" role="group" aria-label="Dnešní seance">
        <div className="stat">
          <span className="stat-label muted">Dnes obchodů</span>
          <span className="stat-value" data-testid="day-trades">
            {day.trades > 0 ? day.trades : '—'}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Úspěšné / ztrátové</span>
          <span className="stat-value">
            {day.closed > 0 ? (
              <>
                <span className="r-positive">{day.wins}</span>
                {' / '}
                <span className="r-negative">{day.losses}</span>
              </>
            ) : (
              '—'
            )}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Úspěšnost dne</span>
          {/* null ≠ 0 %: den bez uzavřeného obchodu není neúspěšný, jen nedokončený */}
          <span className="stat-value" data-testid="day-winrate">
            {day.winRate === null ? '—' : `${Math.round(day.winRate)} %`}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Největší zisk</span>
          <span className="stat-value r-positive">
            {day.bestUsd !== null && day.bestUsd > 0 ? formatPnlUsd(day.bestUsd) : '—'}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Největší ztráta</span>
          <span className="stat-value r-negative">
            {day.worstUsd !== null && day.worstUsd < 0 ? formatPnlUsd(day.worstUsd) : '—'}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">% účtu dnes</span>
          <span
            className={`stat-value ${day.pnlUsd >= 0 ? 'r-positive' : 'r-negative'}`}
            data-testid="day-pct"
          >
            {day.closed > 0 ? formatPct(day.pnlPct) : '—'}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Riskováno (max / celkem)</span>
          {/* Dvě čtení rizika: největší jednotlivá sázka a celkové nasazení dne.
              Počítá se i z aktivních — „co je v sázce" je otázka o vstupu. */}
          <span
            className="stat-value"
            data-testid="day-risk"
            title="Max riziko v jednom obchodě / součet rizik všech dnešních obchodů, v % startovního účtu"
          >
            {' '}
            {/* prettier-ignore */}
            {day.trades > 0
              ? `${day.maxRiskPct.toFixed(1)} % / ${day.totalRiskPct.toFixed(1)} %`
              : '—'}
          </span>
        </div>
      </div>
      {day.trades === 0 && (
        <p className="muted setups-day-empty">
          Dnešní seance zatím bez obchodu — detektor běží, jen nenastaly podmínky šablon.
        </p>
      )}
      {visible.length === 0 && (
        <p className="muted">
          Zatím žádné setupy — detektor běží nad živými daty a čeká na podmínky šablon (odraz od
          zdi, neúspěšný průraz, Max Pain pin, gamma momentum).
        </p>
      )}
      {visible.length > 0 && (
        <div className="setups-table-wrap">
          <table className="setups-table">
            <thead>
              <tr>
                <th>Vznik</th>
                <th>Šablona</th>
                <th>Směr</th>
                <th>Entry</th>
                <th>Cíl</th>
                <th>Stop</th>
                <th>RRR</th>
                <th>Důvěra</th>
                <th>Stav</th>
                <th>Uzavřeno</th>
                <th>R</th>
                <th>P/L (1 ks)</th>
                <th>Hodnocení</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((row) => {
                const pnl = setupPnlUsd(row, pointUsd)
                const pct = setupPnlPct(row, pointUsd)
                return (
                  <tr key={row.id} title={row.reason}>
                    <td>{formatTs(row.created_ts)}</td>
                    <td>{templateLabel(row.template)}</td>
                    <td className={row.direction}>{row.direction === 'long' ? 'LONG' : 'SHORT'}</td>
                    <td>{formatLevel(row.entry)}</td>
                    <td>{formatLevel(row.target)}</td>
                    <td>{formatLevel(row.stop)}</td>
                    <td>{setupRrr(row).toFixed(1)}</td>
                    <td>{row.confidence} %</td>
                    <td>
                      <span className={`setup-status ${row.status}`}>
                        {STATUS_LABELS[row.status] ?? row.status}
                      </span>
                    </td>
                    <td data-part="closed-ts">{formatTs(row.closed_ts)}</td>
                    <td className={(row.outcome_r ?? 0) >= 0 ? 'r-positive' : 'r-negative'}>
                      {row.outcome_r === null
                        ? '—'
                        : `${row.outcome_r >= 0 ? '+' : ''}${row.outcome_r.toFixed(2)}`}
                    </td>
                    <td className={(pnl ?? 0) >= 0 ? 'r-positive' : 'r-negative'} data-part="pnl">
                      {pnl === null ? '—' : formatPnlUsd(pnl)}
                      {pct !== null && <span className="pnl-pct muted"> {formatPct(pct)}</span>}
                    </td>
                    <td>
                      <ReviewCell row={row} symbol={symbol} onSaved={refresh} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted setups-disclaimer">
        Setupy jsou podpora rozhodování, ne obchodní signály. Confidence se kalibruje až s dostatkem
        uzavřených výsledků (Fáze 2).
      </p>
    </section>
  )
}
