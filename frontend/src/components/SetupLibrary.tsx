/** Setupy → Knihovna (#1323, fáze 1): buňky ticker × šablona se stádiem a důkazem.

Rozhoduje se nad čísly: u každé buňky stádium (Auto / Stín / Zkouška), verdikt
brány spočítaný teď, ØR hrubě a čistě, průkaznost n / n potřebné a odhad
v seancích. Server řadí podle průkaznosti; tabulka jde přeřadit podle sloupce
a zúžit filtry (čisté funkce `libraryRows` v api/setupLibrary.ts, pohled se
pamatuje v localStorage), čísla UI nepřepočítává. Nahoře stav brzd účtu
a běžící zkoušky. Změna stádia = dialog s povinným důvodem →
`POST /setups/stage` (nová verze parametrů, engine ji převezme do sekund). */
import { useEffect, useMemo, useState } from 'react'
import { fetchSetupParams, templateLabel } from '../api/setups'
import type { SetupParamsResponse } from '../api/setups'
import {
  cellTitle,
  clearLibraryFilters,
  decisionText,
  decisionTooltip,
  DEFAULT_LIBRARY_VIEW,
  evidenceTooltip,
  GATE_FILTER_LABELS,
  gateLabel,
  gateTooltip,
  lbText,
  LIBRARY_GATE_FILTERS,
  LIBRARY_QUERY_MAX,
  LIBRARY_SORT_KEYS,
  LIBRARY_SORT_LABELS,
  libraryFiltersActive,
  libraryGateFilterOf,
  libraryParams,
  libraryRows,
  librarySortKeyOf,
  librarySortTooltip,
  libraryTickers,
  nextLibrarySort,
  proofText,
  proofTooltip,
  reviveLibraryView,
  STAGE_LABELS,
  stageLabel,
  stageTooltip,
  toggleLibraryStage,
  TRIAL_END_SHORT,
  trialEnd,
  trialUsageText,
  USER_STAGES,
} from '../api/setupLibrary'
import type {
  LibraryBrakes,
  LibraryCell,
  LibraryParams,
  LibrarySortKey,
  LibraryView,
} from '../api/setupLibrary'
import { useSessionDate } from '../hooks/useSessionDate'
import { useSetupsSummary } from '../hooks/useSetupsSummary'
import { useWatchlistSymbols } from '../hooks/useWatchlistSymbols'
import { useAppState } from '../state/AppState'
import { usePersistentState } from '../state/persist'
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
a hodnota vpravo (flex se dvěma položkami). Pořadí buněk = `LIBRARY_SORT_KEYS`
(záhlaví), popisky karet = `LIBRARY_SORT_LABELS`. */
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
      <td data-label={LIBRARY_SORT_LABELS.setup} className="library-setup">
        {cellTitle(cell)}
      </td>
      <td data-label={LIBRARY_SORT_LABELS.ticker}>{cell.ticker}</td>
      <td data-label={LIBRARY_SORT_LABELS.stage}>
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
      <td data-label={LIBRARY_SORT_LABELS.gate} title={gateTooltip(cell, params)}>
        <span>
          <span className={`library-gate gate-${cell.gate_verdict}`}>
            {gateLabel(cell, params.gateMinSamples)}
          </span>{' '}
          <span className={tone(cell.gate_lb)}>{lbText(cell.gate_lb)}</span>
        </span>
      </td>
      <td data-label={LIBRARY_SORT_LABELS.avgR} title={evidenceTooltip(cell)}>
        <span className={tone(cell.avg_r)}>
          {cell.avg_r === null ? '—' : signed(cell.avg_r, 2)}
        </span>
      </td>
      <td data-label={LIBRARY_SORT_LABELS.avgNetR} title={evidenceTooltip(cell)}>
        <span className={tone(cell.avg_net_r)}>
          {cell.avg_net_r === null ? '—' : signed(cell.avg_net_r, 2)}
        </span>
      </td>
      <td data-label={LIBRARY_SORT_LABELS.proof} title={proofTooltip(cell)}>
        {proofText(cell)}
      </td>
      <td data-label={LIBRARY_SORT_LABELS.decision} title={decisionTooltip(cell, params)}>
        {decisionText(cell)}
      </td>
    </tr>
  )
}

const SORT_ARROW = { asc: '▲', desc: '▼' } as const
const SORT_DIR_TEXT = { asc: 'vzestupně', desc: 'sestupně' } as const

/** Záhlaví sloupce: tlačítko (Tab + Enter/mezerník), směr v aria-sort a šipce. */
function SortHeader({
  sortKey,
  view,
  onChange,
}: {
  sortKey: LibrarySortKey
  view: LibraryView
  onChange: (view: LibraryView) => void
}) {
  const active = view.sortKey === sortKey
  return (
    <th
      scope="col"
      aria-sort={active ? (view.sortDir === 'asc' ? 'ascending' : 'descending') : undefined}
    >
      <button
        type="button"
        className={active ? 'library-sort active' : 'library-sort'}
        title={librarySortTooltip(sortKey)}
        onClick={() => onChange(nextLibrarySort(view, sortKey))}
      >
        {LIBRARY_SORT_LABELS[sortKey]}
        <span aria-hidden="true" className="library-sort-arrow">
          {active ? SORT_ARROW[view.sortDir] : '↕'}
        </span>
      </button>
    </th>
  )
}

const STAGE_FILTER_TOOLTIP = [
  'Filtr podle stádia, které se teď uplatní:',
  '• bez výběru = všechna stádia',
  '• víc voleb = kterékoli z nich',
  '• skončená zkouška se počítá jako Auto',
].join('\n')

const QUERY_TOOLTIP = [
  'Hledá v názvu setupu (T7 Pokračování trendu):',
  '• bez ohledu na velká písmena a diakritiku',
  '• „T7" najde šablonu podle čísla',
].join('\n')

const NET_POSITIVE_TOOLTIP = [
  'Jen buňky s kladným ØR čistě:',
  '• čistě = po nákladech 1 mikra (ADR-0030)',
  '• buňky bez ØR (—) skryje',
].join('\n')

/** Filtry nad tabulkou + řazení výběrem pro mobil (záhlaví tabulky je tam skryté). */
function LibraryFilters({
  cells,
  shown,
  view,
  onChange,
}: {
  cells: LibraryCell[]
  shown: number
  view: LibraryView
  onChange: (view: LibraryView) => void
}) {
  return (
    <div className="library-filters" role="group" aria-label="Filtry a řazení Knihovny">
      <label className="library-filter">
        Ticker
        <select
          aria-label="Ticker"
          value={view.ticker ?? ''}
          onChange={(event) =>
            onChange({ ...view, ticker: event.target.value === '' ? null : event.target.value })
          }
        >
          <option value="">všechny</option>
          {libraryTickers(cells, view.ticker).map((ticker) => (
            <option key={ticker} value={ticker}>
              {ticker}
            </option>
          ))}
        </select>
      </label>
      <span
        className="library-filter"
        role="group"
        aria-label="Stádium"
        title={STAGE_FILTER_TOOLTIP}
      >
        <span aria-hidden="true">Stádium</span>
        {USER_STAGES.map((stage) => {
          const pressed = view.stages.includes(stage)
          return (
            <button
              key={stage}
              type="button"
              className={pressed ? 'chip active' : 'chip'}
              aria-pressed={pressed}
              onClick={() => onChange(toggleLibraryStage(view, stage))}
            >
              {STAGE_LABELS[stage]}
            </button>
          )
        })}
      </span>
      <label className="library-filter">
        Brána
        <select
          aria-label="Brána"
          value={view.gate ?? ''}
          onChange={(event) => onChange({ ...view, gate: libraryGateFilterOf(event.target.value) })}
        >
          <option value="">vše</option>
          {LIBRARY_GATE_FILTERS.map((gate) => (
            <option key={gate} value={gate}>
              {GATE_FILTER_LABELS[gate]}
            </option>
          ))}
        </select>
      </label>
      <input
        type="search"
        className="library-search"
        aria-label="Hledat v názvu setupu"
        placeholder="hledat setup…"
        maxLength={LIBRARY_QUERY_MAX}
        title={QUERY_TOOLTIP}
        value={view.query}
        onChange={(event) => onChange({ ...view, query: event.target.value })}
      />
      <label className="setups-version-toggle" title={NET_POSITIVE_TOOLTIP}>
        <input
          type="checkbox"
          checked={view.netPositive}
          onChange={(event) => onChange({ ...view, netPositive: event.target.checked })}
        />
        jen ØR čistě &gt; 0
      </label>
      <span className="library-sort-mobile">
        <label className="library-filter">
          Řadit
          <select
            aria-label="Řadit podle"
            value={view.sortKey}
            onChange={(event) => {
              const key = librarySortKeyOf(event.target.value)
              if (key !== null) onChange(nextLibrarySort(view, key))
            }}
          >
            {LIBRARY_SORT_KEYS.map((key) => (
              <option key={key} value={key}>
                {LIBRARY_SORT_LABELS[key]}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="chip"
          aria-label={`Směr řazení: ${SORT_DIR_TEXT[view.sortDir]}`}
          onClick={() => onChange(nextLibrarySort(view, view.sortKey))}
        >
          {SORT_ARROW[view.sortDir]}
        </button>
      </span>
      <span className="muted" data-testid="library-count" aria-live="polite">
        zobrazeno {shown} z {cells.length}
      </span>
      <button
        type="button"
        className="chip"
        disabled={!libraryFiltersActive(view)}
        onClick={() => onChange(clearLibraryFilters(view))}
      >
        Zrušit filtry
      </button>
    </div>
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
  // Řazení a filtry tabulky přežijí refresh; rozbitý záznam = výchozí pohled.
  // Změna významu polí nebo DEFAULT_LIBRARY_VIEW = nový klíč (…v2): hook zapíše
  // pohled hned při prvním otevření, takže starý záznam by nový default přebil
  const [view, setView] = usePersistentState<LibraryView>(
    'setupLibraryView',
    DEFAULT_LIBRARY_VIEW,
    reviveLibraryView,
  )

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
  const rows = useMemo(() => (cells === null ? [] : libraryRows(cells, view)), [cells, view])
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
            <>
              <LibraryFilters cells={cells} shown={rows.length} view={view} onChange={setView} />
              {rows.length === 0 ? (
                <p className="muted" data-testid="library-filtered-empty">
                  Filtrům neodpovídá žádná buňka — uvolni je nebo je zruš.
                </p>
              ) : (
                <div className="setups-table-wrap">
                  <table className="setups-table library-table" aria-label="Buňky ticker × šablona">
                    <thead>
                      <tr>
                        {LIBRARY_SORT_KEYS.map((key) => (
                          <SortHeader key={key} sortKey={key} view={view} onChange={setView} />
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((cell) => (
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
            </>
          )}
          <p className="muted setups-page-note library-legend">
            klik na záhlaví (na mobilu výběr Řadit) řadí podle sloupce (výchozí podle průkaznosti n
            / n potřebné) · ▾ = změna stádia s důvodem · čistě = po nákladech ADR-0030 · Stín a
            Zkouška platí jen pro danou buňku, brzdy pro celý účet
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
