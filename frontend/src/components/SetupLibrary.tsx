/** Setupy → Knihovna (#1323, fáze 1): buňky ticker × šablona se stádiem a důkazem.

Rozhoduje se nad čísly: u každé buňky stádium (Auto / Stín / Zkouška), verdikt
brány spočítaný teď, ØR hrubě a čistě, průkaznost n / n potřebné a odhad
v seancích. Řadí server podle průkaznosti, UI nic nepřepočítává. Nahoře stav
brzd účtu a běžící zkoušky. Změna stádia = dialog s povinným důvodem →
`POST /setups/stage` (nová verze parametrů, engine ji převezme do sekund). */
import { useEffect, useMemo, useState } from 'react'
import { fetchSetupParams, templateLabel } from '../api/setups'
import type { SetupParamsResponse } from '../api/setups'
import {
  cellTitle,
  decisionText,
  decisionTooltip,
  evidenceTooltip,
  gateLabel,
  gateTooltip,
  lbText,
  libraryParams,
  proofText,
  proofTooltip,
  stageLabel,
  stageTooltip,
  TRIAL_END_SHORT,
  trialEnd,
  trialUsageText,
} from '../api/setupLibrary'
import type { LibraryBrakes, LibraryCell, LibraryParams } from '../api/setupLibrary'
import { useSessionDate } from '../hooks/useSessionDate'
import { useSetupsSummary } from '../hooks/useSetupsSummary'
import { useWatchlistSymbols } from '../hooks/useWatchlistSymbols'
import { useAppState } from '../state/AppState'
import { StageDialog } from './StageDialog'

/** Watchlist se mění zřídka; nové buňky přinese i WS setups.* (nový setup). */
const WATCHLIST_REFRESH_MS = 300_000

function signed(value: number, digits: number): string {
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`
}

function tone(value: number | null): string | undefined {
  if (value === null) return undefined
  return value >= 0 ? 'r-positive' : 'r-negative'
}

function brakePart(label: string, value: number, limit: number): string {
  return limit > 0
    ? `${label} ${signed(value, 1)} / ${(-limit).toFixed(1)} R`
    : `${label} ${signed(value, 1)} R (brzda vypnutá)`
}

const BRAKES_TOOLTIP = [
  'Brzdy účtu — Σ R obchodovatelných uzavřených setupů napříč všemi symboly:',
  '• den = seance do 17:00 CT, týden = od pondělní seance (neděle 17:00 CT)',
  '• na limitu jsou nové setupy do konce seance / týdne jen stínové',
  '• strop stopů šablony: po N stopech téže šablony za seanci je šablona stínová',
  '• brzdy nepřebije žádné stádium, ani Zkouška',
].join('\n')

/** Hlavička: brzdy účtu a zkoušky. */
function LibraryStatus({ brakes, cells }: { brakes: LibraryBrakes | null; cells: LibraryCell[] }) {
  const trials = cells.filter((cell) => cell.trial !== null)
  const numberOf = (template: string) =>
    cells.find((cell) => cell.template === template)?.template_number ?? null
  return (
    <div className="library-status" data-testid="library-status">
      {brakes === null ? (
        <span className="muted">Brzdy: server stav nedodal</span>
      ) : (
        <span title={BRAKES_TOOLTIP} data-testid="library-brakes">
          Brzdy: {brakePart('den', brakes.day_r, brakes.daily_brake_r)} ·{' '}
          {brakePart('týden', brakes.week_r, brakes.weekly_brake_r)}
          {brakes.block !== null && (
            <strong className="r-negative" data-testid="library-brake-active">
              {' '}
              · {brakes.block === 'daily_brake' ? 'denní' : 'týdenní'} brzda aktivní — nové
              obchodovatelné setupy jen stínově
            </strong>
          )}
          {Object.entries(brakes.template_stops).map(([template, count]) => {
            const number = numberOf(template)
            const name = number === null ? templateLabel(template) : `T${number}`
            const cap = brakes.max_template_stops_per_day
            return (
              <span key={template} className={cap > 0 && count >= cap ? 'r-negative' : undefined}>
                {' '}
                · stopy {name} {cap > 0 ? `${count}/${cap}` : count}
              </span>
            )
          })}
        </span>
      )}
      <span data-testid="library-trials">
        Zkoušky:{' '}
        {trials.length === 0
          ? 'žádná'
          : trials.map((cell, index) => {
              const ended = trialEnd(cell)
              return (
                <span key={cell.cell} className={ended !== null ? 'muted' : undefined}>
                  {index > 0 && ' · '}
                  {cell.ticker} T{cell.template_number}{' '}
                  {cell.trial !== null && trialUsageText(cell.trial)}
                  {ended !== null && ` (${TRIAL_END_SHORT[ended]} → Auto)`}
                </span>
              )
            })}
      </span>
    </div>
  )
}

/** Řádek tabulky; na úzké obrazovce karta (popisky z `data-label`, CSS).
Víceprvkový obsah buňky drží jeden `span`, ať je na kartě popisek vlevo
a hodnota vpravo (flex se dvěma položkami). */
function LibraryRow({
  cell,
  params,
  onEdit,
}: {
  cell: LibraryCell
  params: LibraryParams
  onEdit: () => void
}) {
  return (
    <tr
      data-testid={`library-row-${cell.cell}`}
      className={`library-row stage-${cell.effective_stage}`}
    >
      <td data-label="Setup" className="library-setup">
        {cellTitle(cell)}
      </td>
      <td data-label="Ticker">{cell.ticker}</td>
      <td data-label="Stádium">
        <span>
          <button
            type="button"
            className={`chip stage-chip stage-${cell.effective_stage}`}
            aria-haspopup="dialog"
            aria-label={`Stádium ${cellTitle(cell)} · ${cell.ticker}: ${stageLabel(cell)} — změnit`}
            title={stageTooltip(cell)}
            onClick={onEdit}
          >
            {stageLabel(cell)} ▾
          </button>
          {cell.trial !== null && trialEnd(cell) === null && (
            <span className="muted library-trial-usage"> {trialUsageText(cell.trial)}</span>
          )}
        </span>
      </td>
      <td data-label="Brána teď" title={gateTooltip(cell, params)}>
        <span>
          <span className={`library-gate gate-${cell.gate_verdict}`}>
            {gateLabel(cell, params.gateMinSamples)}
          </span>{' '}
          <span className={tone(cell.gate_lb)}>{lbText(cell.gate_lb)}</span>
        </span>
      </td>
      <td data-label="ØR hrubě / čistě" title={evidenceTooltip(cell)}>
        <span>
          <span className={tone(cell.avg_r)}>
            {cell.avg_r === null ? '—' : signed(cell.avg_r, 2)}
          </span>
          {' / '}
          <span className={tone(cell.avg_net_r)}>
            {cell.avg_net_r === null ? '—' : signed(cell.avg_net_r, 2)}
          </span>
        </span>
      </td>
      <td data-label="Průkaznost" title={proofTooltip(cell)}>
        {proofText(cell)}
      </td>
      <td data-label="Rozhodnutelné" title={decisionTooltip(cell, params)}>
        {decisionText(cell)}
      </td>
    </tr>
  )
}

export function SetupLibrary() {
  const { symbol, setupsVersion } = useAppState()
  const watchlist = useWatchlistSymbols(WATCHLIST_REFRESH_MS)
  // Tickery watchlistu + aktivní symbol (pinovaný kontrakt, ad-hoc pohled);
  // server z nich vrátí jen ty se setupy v okně brány nebo se stádiem
  const symbols = useMemo(() => [...new Set([...watchlist, symbol])].sort(), [watchlist, symbol])
  const sessionDay = useSessionDate()
  // Po uložení stádia se načte nová verze parametrů i buňky
  const [reload, setReload] = useState(0)
  const { summary, failed, stale } = useSetupsSummary(symbols, {
    refreshKey: `${setupsVersion}|${sessionDay}|${reload}`,
  })
  const [paramsResponse, setParamsResponse] = useState<SetupParamsResponse | null>(null)
  // Platná verze se čte znovu i při otevření dialogu — „platí: … (verze N)"
  // a „Uložit → verze N+1" nesmí lhát, když mezitím uložily Settings
  const [paramsKey, setParamsKey] = useState(0)
  const [editing, setEditing] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    void fetchSetupParams().then((response) => {
      if (!cancelled) setParamsResponse(response)
    })
    return () => {
      cancelled = true
    }
  }, [reload, paramsKey])

  const params = libraryParams(paramsResponse)
  const cells = summary?.cells ?? null
  const brakes = summary?.brakes ?? null
  const editingCell =
    editing === null ? null : (cells?.find((cell) => cell.cell === editing) ?? null)

  return (
    <section className="setup-library" aria-label="Knihovna setupů">
      <header className="setups-summary">
        <h2>Knihovna setupů</h2>
        <span className="muted" data-testid="library-scope">
          okno {params.gateDays} seancí
          {summary !== null && ` · mechanika v${summary.mechanics_version}`}
        </span>
      </header>
      {failed && (
        <p className="muted setups-summary-error" role="alert" data-testid="library-error">
          Knihovnu se nepodařilo načíst ze serveru (API nebo databáze).
        </p>
      )}
      {summary !== null && cells === null && (
        <p className="muted setups-summary-error" role="alert" data-testid="library-unsupported">
          Server Knihovnu ještě nezná (API před #1323) — po nasazení se ukáže.
        </p>
      )}
      {summary === null && !failed && <p className="muted">Načítám Knihovnu…</p>}
      {message !== null && (
        <p className="library-message" role="status" data-testid="library-message">
          {message}
        </p>
      )}
      {cells !== null && (
        <div className={stale ? 'summary-stale' : undefined} aria-busy={stale}>
          <LibraryStatus brakes={brakes} cells={cells} />
          {cells.length === 0 ? (
            <p className="muted" data-testid="library-empty">
              Žádná buňka: v okně brány zatím nevznikl žádný setup a žádná buňka nemá nastavené
              stádium.
            </p>
          ) : (
            <div className="setups-table-wrap">
              <table className="setups-table library-table" aria-label="Buňky ticker × šablona">
                <thead>
                  <tr>
                    <th>Setup</th>
                    <th>Ticker</th>
                    <th>Stádium</th>
                    <th>Brána teď</th>
                    <th>ØR hrubě / čistě</th>
                    <th>Průkaznost</th>
                    <th>Rozhodnutelné</th>
                  </tr>
                </thead>
                <tbody>
                  {cells.map((cell) => (
                    <LibraryRow
                      key={cell.cell}
                      cell={cell}
                      params={params}
                      onEdit={() => {
                        setMessage(null)
                        setParamsKey((value) => value + 1)
                        setEditing(cell.cell)
                      }}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="muted setups-page-note library-legend">
            řazeno podle průkaznosti (n / n potřebné) · ▾ = změna stádia s důvodem · čistě = po
            nákladech ADR-0030 · Stín a Zkouška platí jen pro danou buňku, brzdy pro celý účet
          </p>
          {params.disabledTemplates.length > 0 && (
            <p className="muted setups-page-note" data-testid="library-disabled">
              Vyřazené šablony (neměří se, `disabled_templates`):{' '}
              {params.disabledTemplates.map((template) => templateLabel(template)).join(', ')}
            </p>
          )}
        </div>
      )}
      {editingCell !== null && (
        <StageDialog
          cell={editingCell}
          params={params}
          brakes={brakes}
          onCancel={() => setEditing(null)}
          onSaved={(version) => {
            setEditing(null)
            setMessage(`Uloženo jako verze ${version} — engine přepne do sekund.`)
            setReload((value) => value + 1)
          }}
        />
      )}
    </section>
  )
}
