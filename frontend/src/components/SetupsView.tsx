/** Obrazovka Setupy (ADR-0004): historie analýz s výsledky a ručním hodnocením.

Predikce jsou neměnné — jediná mutace je rating (+1/−1) a poznámka; hodnocení
je kvalitativní vrstva a nevstupuje do automatické kalibrace confidence.
*/
import { useState } from 'react'
import type { ReactNode } from 'react'
import { STATUS_LABELS, TRADE_BLOCK_LABELS, accountPnlUsd, bandInfo, bandLabel, bandTooltip, confidenceTooltip, evTooltip, formatGateBucket, formatPct, formatPnlUsd, reviewSetup, riskInfo, riskLabel, riskTooltip, setupPnlPct, setupPnlUsd, setupRrr, templateLabel } from '../api/setups' // prettier-ignore
import type { SetupRow, SummaryGroup } from '../api/setups'
import { formatLevel } from '../heatmap/overlays'
import { useSetups } from '../hooks/useSetups'
import { useSessionDate } from '../hooks/useSessionDate'
import { useSetupsSummary } from '../hooks/useSetupsSummary'
import { pointValue } from '../instrument/tick'
import { useAppState } from '../state/AppState'
import { CoachSetupsBlock } from './CoachSetupsBlock'
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

/** Dlaždice souhrnu: popisek, hodnota, volitelně barva znaménka a tooltip. */
function Stat({
  label,
  value,
  tone,
  testId,
  title,
}: {
  label: string
  value: ReactNode
  tone?: number | null
  testId?: string
  title?: string
}) {
  const toneClass =
    tone === undefined || tone === null ? '' : tone >= 0 ? ' r-positive' : ' r-negative'
  return (
    <div className="stat">
      <span className="stat-label muted">{label}</span>
      <span className={`stat-value${toneClass}`} data-testid={testId} title={title}>
        {value}
      </span>
    </div>
  )
}

function formatRate(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 100)} %`
}

function formatR(value: number | null, digits = 2): string {
  return value === null ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`
}

/** Poplatky jako odečet („-3140 $"); nula bez znaménka. */
function formatFees(value: number): string {
  return value > 0 ? formatPnlUsd(-value) : '0 $'
}

/** Den seance ze serveru („2026-09-28" → „28. 9."), bez závislosti na zóně prohlížeče. */
function formatSessionDay(iso: string): string {
  const [, month, day] = iso.split('-').map(Number)
  return `${day}. ${month}.`
}

const SHADOW_REASON_LABELS: Record<string, string> = {
  ...TRADE_BLOCK_LABELS,
  unknown: 'neznámý důvod',
}

/** Řádek rozdělení obchodovatelné / stínové / bez pravidel (1 kontrakt na setup). */
function SplitRow({
  label,
  title,
  group,
  testId,
}: {
  label: string
  title: string
  group: SummaryGroup
  testId: string
}) {
  const tone = (value: number) => (value >= 0 ? 'r-positive' : 'r-negative')
  return (
    <tr data-testid={testId} title={title}>
      <td>{label}</td>
      <td>{group.count}</td>
      <td>{group.closed}</td>
      <td>{formatRate(group.win_rate)}</td>
      <td className={tone(group.sum_r)}>{formatR(group.sum_r, 1)}</td>
      <td className={tone(group.avg_r ?? 0)}>{formatR(group.avg_r)}</td>
      <td className={tone(group.gross_usd)}>{formatPnlUsd(group.gross_usd)}</td>
      <td className="r-negative">{formatFees(group.fees_usd)}</td>
      <td className={tone(group.net_usd)}>{formatPnlUsd(group.net_usd)}</td>
    </tr>
  )
}

export function SetupsView() {
  const { symbol, setupsVersion } = useAppState()
  const { setups, totalCount, refresh } = useSetups()
  // Statistiky defaultně jen z aktuální mechaniky (#311) — setupy staré verze
  // mají jinou sémantiku stopů a cílů (Ø RRR 25–47), míchat je do jedné bilance
  // by znamenalo počítat výkonnost systému, který už neexistuje
  const [allVersions, setAllVersions] = usePersistentState<boolean>(
    'setupsAllVersions',
    false,
    (value) => (typeof value === 'boolean' ? value : false),
  )
  // Jen obchodovatelné (#1185): skryje stínové setupy (stop nad rozpočtem,
  // brzda, brána) v TABULCE; řádky bez risk kontextu (před pravidly) zůstávají
  const [tradeableOnly, setTradeableOnly] = usePersistentState<boolean>(
    'setupsTradeableOnly',
    false,
    (value) => (typeof value === 'boolean' ? value : false),
  )
  // Souhrn z CELÉ historie počítá server (#1319) — tabulka níž je jen stránka
  // posledních 200 setupů a agregace nad ní byla klouzavé okno. Blok „Dnes"
  // je snímek serveru: nová seance (po 17:00 CT) ho musí přenačíst i bez
  // WS události setups.* — přes noc na Globexu nový setup nemusí přijít hodiny
  const sessionDay = useSessionDate()
  const { summary, failed, stale } = useSetupsSummary([symbol], {
    allVersions,
    refreshKey: `${setupsVersion}|${sessionDay}`,
  })
  // Po přepnutí mechaniky zůstává poslední souhrn ztlumeně do odpovědi —
  // přepínač ani bloky nezmizí pod kurzorem a stránka neposkočí
  const summaryClass = (base: string) => (stale ? `${base} summary-stale` : base)
  const legacyCount = summary?.legacy_count ?? null
  const shadowCount = summary?.shadow.count ?? null
  // Aktuální mechanika ze serveru (engine SETUP_MECHANICS_VERSION); bez
  // souhrnu tabulka nefiltruje — radši víc řádků než tichý výpadek
  const mechanicsVersion = summary?.mechanics_version ?? null
  const byVersion =
    allVersions || mechanicsVersion === null
      ? setups
      : setups.filter((row) => (row.mechanics_version ?? 1) === mechanicsVersion)
  const visible = tradeableOnly
    ? byVersion.filter((row) => riskInfo(row)?.tradeable !== false)
    : byVersion
  const pointUsd = pointValue(symbol)
  const all = summary?.all ?? null
  const account = summary?.account ?? null
  const day = summary?.today ?? null
  const gates = summary?.band_gates ?? null
  const shadowTitle = summary
    ? [
        'Setup vznikl a měří se, ale NEobchoduje se (risk pravidla #1185):',
        ...Object.entries(summary.shadow_reasons).map(
          ([reason, count]) => `• ${SHADOW_REASON_LABELS[reason] ?? reason}: ${count}`,
        ),
      ].join('\n')
    : ''

  return (
    <section className="setups-view" aria-label="Setupy">
      <header className="setups-summary">
        <h2>Setupy — {symbol}</h2>
        {/* Zapnutý přepínač zůstává i bez souhrnu (chyba serveru) — jinak by
            filtr nešel vypnout */}
        {((legacyCount ?? 0) > 0 || allVersions) && (
          <label className="setups-version-toggle">
            <input
              type="checkbox"
              checked={allVersions}
              onChange={(event) => setAllVersions(event.target.checked)}
              data-testid="setups-all-versions"
            />
            Včetně starší mechaniky{legacyCount !== null && ` (${legacyCount})`}
          </label>
        )}
        {((shadowCount ?? 0) > 0 || tradeableOnly) && (
          <label className="setups-version-toggle">
            <input
              type="checkbox"
              checked={tradeableOnly}
              onChange={(event) => setTradeableOnly(event.target.checked)}
              data-testid="setups-tradeable-only"
            />
            Jen obchodovatelné v tabulce{shadowCount !== null && ` (stín ${shadowCount})`}
          </label>
        )}
      </header>
      {failed && (
        <p className="muted setups-summary-error" role="alert" data-testid="setups-summary-error">
          Souhrn setupů se nepodařilo načíst ze serveru (API nebo databáze). Čísla se nedopočítávají
          z tabulky — ta ukazuje jen posledních 200 setupů.
        </p>
      )}
      {summary !== null && all !== null && (
        <>
          {/* Celá historie (#1319): každý setup rovným dílem 1 kontrakt, i stínový */}
          <div
            className={summaryClass('setups-stats')}
            role="group"
            aria-label="Souhrnné statistiky"
            aria-busy={stale}
            title={[
              `Celá historie ${symbol}, mechanika ${summary.all_versions ? 'všechny verze' : `v${summary.mechanics_version}`} (spočítal server):`,
              '• každý setup = 1 kontrakt, obchodovatelný i stínový',
              '• hrubý výsledek = R × stop × hodnota bodu',
              `• poplatky = ${summary.fee_per_contract_usd} $ za kontrakt a obchod`,
              '• čistý = hrubý − poplatky',
              '• co by reálně vydělal účet, ukazuje řádek Účet níž',
            ].join('\n')}
          >
            <Stat label="Aktivní" value={all.active} />
            <Stat label="Uzavřené" value={all.closed} testId="setups-closed" />
            <Stat label="Úspěšnost" value={formatRate(all.win_rate)} />
            <Stat label="Ø R" value={formatR(all.avg_r)} tone={all.avg_r} />
            <Stat
              label="Σ R"
              value={formatR(all.closed > 0 ? all.sum_r : null, 1)}
              tone={all.sum_r}
            />
            <Stat
              label="Hrubý výsledek (1 kontrakt)"
              value={all.closed > 0 ? formatPnlUsd(all.gross_usd) : '—'}
              tone={all.gross_usd}
              testId="setups-total-pnl"
            />
            <Stat
              label="Poplatky"
              value={all.closed > 0 ? formatFees(all.fees_usd) : '—'}
              testId="setups-fees"
              title={`${all.closed} uzavřených × ${summary.fee_per_contract_usd} $ (1 kontrakt, obchod tam i zpět)`}
            />
            <Stat
              label="Čistý výsledek"
              value={all.closed > 0 ? formatPnlUsd(all.net_usd) : '—'}
              tone={all.net_usd}
              testId="setups-net-pnl"
            />
            {/* EV na obchod (#911): hrubě v USD + rozklad v tooltipu; v R ≡ Ø R */}
            <Stat
              label="EV / obchod (hrubě)"
              value={all.ev_usd ? formatPnlUsd(all.ev_usd.ev) : '—'}
              tone={all.ev_usd?.ev ?? null}
              testId="setups-ev"
              title={all.ev_usd ? evTooltip(all.ev_usd, '$') : undefined}
            />
          </div>
          {/* Rozdělení (#1319): co se obchoduje vs. co se jen měří */}
          <div className={summaryClass('setups-table-wrap')} aria-busy={stale}>
            <table className="setups-table setups-split" aria-label="Obchodovatelné a stínové">
              <thead>
                <tr>
                  <th>Skupina</th>
                  <th>Setupů</th>
                  <th>Uzavřené</th>
                  <th>Úspěšnost</th>
                  <th>Σ R</th>
                  <th>Ø R</th>
                  <th>Hrubě (1 kontrakt)</th>
                  <th>Poplatky</th>
                  <th>Čistě</th>
                </tr>
              </thead>
              <tbody>
                <SplitRow
                  label="Obchodovatelné"
                  testId="split-tradeable"
                  group={summary.tradeable}
                  title={[
                    'Setupy, které risk pravidla (#1185) pustila do obchodu:',
                    '• stop v rozpočtu rizika, brzdy neaktivní, šablona s prokázaným edge',
                  ].join('\n')}
                />
                <SplitRow
                  label="Stínové"
                  testId="split-shadow"
                  group={summary.shadow}
                  title={shadowTitle}
                />
                <SplitRow
                  label="Bez risk pravidel"
                  testId="split-unruled"
                  group={summary.unruled}
                  title={[
                    'Setupy z doby před risk pravidly (#1185, 15. 9. 2026):',
                    '• nevíme, zda by prošly — do bilance účtu nevstupují',
                  ].join('\n')}
                />
              </tbody>
            </table>
          </div>
          {account !== null && (
            <div
              className={summaryClass('setups-stats setups-stats-account')}
              role="group"
              aria-label="Účet — obchodovatelné"
              aria-busy={stale}
              title={[
                `Co by reálně vydělal účet ${Math.round(summary.account_usd)} $ (#1185):`,
                '• jen OBCHODOVATELNÉ uzavřené setupy',
                '• kontrakty ze serverového sizingu × R × stop × bod − poplatky',
                '• max DD = nejhlubší propad od vrcholu, chronologicky podle uzavření',
                '• reálně na MES/MNQ jsou dolary ÷ 10',
              ].join('\n')}
            >
              <Stat
                label={`Účet ${Math.round(summary.account_usd / 1000)}k · obchodů`}
                value={account.trades}
              />
              <Stat
                label="Účet hrubě"
                value={account.trades > 0 ? formatPnlUsd(account.gross_usd) : '—'}
                tone={account.gross_usd}
              />
              <Stat
                label="Účet poplatky"
                value={account.trades > 0 ? formatFees(account.fees_usd) : '—'}
              />
              <Stat
                label="Účet čistě"
                value={account.trades > 0 ? formatPnlUsd(account.net_usd) : '—'}
                tone={account.net_usd}
                testId="setups-account-pnl"
              />
              <Stat
                label="% účtu"
                value={account.trades > 0 ? formatPct(account.net_pct) : '—'}
                tone={account.net_pct}
                testId="setups-account-pct"
              />
              <Stat
                label="Max DD"
                value={account.trades > 0 ? formatPnlUsd(account.max_drawdown_usd) : '—'}
              />
            </div>
          )}
        </>
      )}
      {/* Bilance dnešní SEANCE (#748) — oddělená od celkové historie výše.
          Den je Globex seance (#512), ne kalendářní datum. */}
      {day !== null && (
        <div
          className={summaryClass('setups-stats setups-stats-day')}
          role="group"
          aria-label="Dnešní seance"
          aria-busy={stale}
        >
          {/* Seance, ke které čísla patří (serverový snímek) — zastaralost je vidět */}
          <Stat
            label="Seance"
            value={formatSessionDay(day.session)}
            testId="day-session"
            title="Obchodní den = Globex seance od 17:00 CT předchozího dne (#512)"
          />
          <Stat
            label="Dnes obchodů"
            value={day.trades > 0 ? day.trades : '—'}
            testId="day-trades"
          />
          <Stat
            label="Úspěšné / ztrátové"
            value={
              day.closed > 0 ? (
                <>
                  <span className="r-positive">{day.wins}</span>
                  {' / '}
                  <span className="r-negative">{day.losses}</span>
                </>
              ) : (
                '—'
              )
            }
          />
          {/* null ≠ 0 %: den bez uzavřeného obchodu není neúspěšný, jen nedokončený */}
          <Stat label="Úspěšnost dne" value={formatRate(day.win_rate)} testId="day-winrate" />
          <Stat
            label="Největší zisk"
            value={day.best_usd !== null && day.best_usd > 0 ? formatPnlUsd(day.best_usd) : '—'}
            tone={1}
          />
          <Stat
            label="Největší ztráta"
            value={day.worst_usd !== null && day.worst_usd < 0 ? formatPnlUsd(day.worst_usd) : '—'}
            tone={-1}
          />
          <Stat
            label="Σ dnes hrubě (1 kontrakt)"
            value={day.closed > 0 ? formatPnlUsd(day.gross_usd) : '—'}
            tone={day.gross_usd}
            testId="day-pnl"
          />
          <Stat
            label="Poplatky dnes"
            value={day.closed > 0 ? formatFees(day.fees_usd) : '—'}
            testId="day-fees"
          />
          <Stat
            label="% účtu dnes (hrubě)"
            value={day.closed > 0 ? formatPct(day.gross_pct) : '—'}
            tone={day.gross_pct}
            testId="day-pct"
          />
          {day.account !== null && (
            <Stat
              label="Účet dnes čistě"
              value={day.account.trades > 0 ? formatPnlUsd(day.account.net_usd) : '—'}
              tone={day.account.net_usd}
              testId="day-account"
              title={[
                'Jen obchodovatelné setupy dne (#1185):',
                '• kontrakty ze sizingu × R × stop × bod − poplatky',
              ].join('\n')}
            />
          )}
          {/* Dvě čtení rizika: největší jednotlivá sázka a celkové nasazení dne.
              Počítá se i z aktivních — „co je v sázce" je otázka o vstupu. */}
          <Stat
            label="Riskováno (max / celkem)"
            value={
              day.trades > 0
                ? `${day.max_risk_pct.toFixed(1)} % / ${day.total_risk_pct.toFixed(1)} %`
                : '—'
            }
            testId="day-risk"
            title={[
              'Riziko dnešních obchodů na 1 kontrakt, v % startovního účtu:',
              '• max = největší riziko v jednom obchodě',
              '• celkem = součet rizik všech dnešních obchodů (i aktivních)',
            ].join('\n')}
          />
        </div>
      )}
      {gates !== null && (
        <div
          className={summaryClass('setups-stats setups-stats-gate')}
          role="group"
          aria-label="Stínová brána podle polohy v pásmu"
          aria-busy={stale}
          title={[
            'Poloha entry v tlumící zóně Dyn GEX (#1060). Nic se neblokuje — u každého setupu se jen zapisuje, co by pravidlo udělalo. Hodnota: počet uzavřených · Ø R.',
            '• jen poloha: mimo pásmo / bez pásma = blok',
            '• poloha × režim: uvnitř vždy, přechod jen v negativní gammě, mimo nikdy',
            'Vyhodnocení ~5. 10. 2026: blok horší o ≥ 0,2 R než prošel → pravidlo se zapne.',
          ].join('\n')}
        >
          <Stat
            label="Jen poloha · prošel"
            value={formatGateBucket(gates.simple.pass)}
            testId="gate-simple-pass"
          />
          <Stat
            label="Jen poloha · blok"
            value={formatGateBucket(gates.simple.block)}
            testId="gate-simple-block"
          />
          <Stat
            label="Poloha × režim · prošel"
            value={formatGateBucket(gates.regime.pass)}
            testId="gate-regime-pass"
          />
          <Stat
            label="Poloha × režim · blok"
            value={formatGateBucket(gates.regime.block)}
            testId="gate-regime-block"
          />
        </div>
      )}
      {/* Kouč nad setupy (#1201): doporučení s vzorkem, denní doba, příznaky */}
      <CoachSetupsBlock symbol={symbol} />
      {day !== null && day.trades === 0 && (
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
      {visible.length > 0 && totalCount !== null && totalCount > setups.length && (
        <p className="muted setups-page-note" data-testid="setups-page-note">
          Tabulka ukazuje posledních {setups.length} z {totalCount} setupů {symbol} (všechny verze
          mechaniky); souhrn nahoře počítá celou historii.
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
                <th>Pásmo</th>
                <th>Účet</th>
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
                const risk = riskInfo(row)
                const accountPnl = accountPnlUsd(row)
                return (
                  <tr
                    key={row.id}
                    title={row.reason}
                    className={risk !== null && !risk.tradeable ? 'setup-shadow' : undefined}
                  >
                    <td>{formatTs(row.created_ts)}</td>
                    <td>{templateLabel(row.template)}</td>
                    <td className={row.direction}>{row.direction === 'long' ? 'LONG' : 'SHORT'}</td>
                    <td>{formatLevel(row.entry)}</td>
                    <td>{formatLevel(row.target)}</td>
                    <td>{formatLevel(row.stop)}</td>
                    <td>{setupRrr(row).toFixed(1)}</td>
                    <td title={confidenceTooltip(row) ?? undefined}>{row.confidence} %</td>
                    <td data-part="band">
                      {(() => {
                        const band = bandInfo(row)
                        if (band === null) return <span className="muted">—</span>
                        return (
                          <span
                            className={`setup-band ${band.bandClass}`}
                            title={bandTooltip(band)}
                          >
                            {bandLabel(band)}
                          </span>
                        )
                      })()}
                    </td>
                    <td data-part="risk">
                      {risk === null ? (
                        <span className="muted">—</span>
                      ) : (
                        <span
                          className={`setup-risk ${risk.tradeable ? 'tradeable' : 'shadow'}`}
                          title={riskTooltip(risk)}
                        >
                          {riskLabel(risk)}
                          {accountPnl !== null && (
                            <span
                              className={`pnl-pct ${accountPnl >= 0 ? 'r-positive' : 'r-negative'}`}
                            >
                              {' '}
                              {formatPnlUsd(accountPnl)}
                            </span>
                          )}
                        </span>
                      )}
                    </td>
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
