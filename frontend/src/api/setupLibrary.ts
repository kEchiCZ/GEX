/** Knihovna setupů (#1323, fáze 1): buňky ticker × šablona, stádia a zkouška.

Buňky, verdikt brány „teď", důkaz a stav brzd počítá server
(`GET /setups/summary` → `cells`, `brakes`; engine `compute/setup_library.py`
týmiž funkcemi jako engine). UI jen vykresluje, řadí a filtruje hotové buňky
(`libraryRows`, nic nepřepočítává) a mění stádium jedné buňky přes
`POST /setups/stage` — začátek zkoušky nastavuje server, ne klient. */
import { API_BASE } from '../config'
import { oneOf, shortString } from '../state/persist'
import { TEMPLATE_LABELS, formatPnlUsd } from './setups'
import type { GateVerdict, SetupParamsResponse, TradeBlock } from './setups'

/** Stádium buňky: Auto (brána rozhoduje) / Stín / Zkouška. */
export type UserStage = 'auto' | 'shadow' | 'trial'
export const USER_STAGES: readonly UserStage[] = ['auto', 'shadow', 'trial']

export const STAGE_LABELS: Record<UserStage, string> = {
  auto: 'Auto',
  shadow: 'Stín',
  trial: 'Zkouška',
}

/** Co stádium dělá — jeden řádek v dialogu. */
export const STAGE_HELP: Record<UserStage, string> = {
  auto: 'brána rozhoduje (sizing → brzdy → brána)',
  shadow: 'měří se, bez pushe, mimo účet a brzdy',
  trial: 'přebije bránu, dokud nevyčerpá rozpočet',
}

/** „Změní se:" pod volbou stádia. */
export const STAGE_EFFECTS: Record<UserStage, string> = {
  auto: 'push na Telegram jen pro obchodovatelné (brána, sizing, brzdy) · do účtu a brzd jen obchodovatelné',
  shadow:
    'push ne · mimo účet a brzdy · setup dál vzniká, měří se, vstupuje do brány a je vidět v grafu i tabulce',
  trial:
    'push na Telegram ano se štítkem ZKOUŠKA · počítá se do účtu a brzd · štítek „zkouška" v Setupech a souhrnu',
}

/** Rychlé důvody (čipy) — povinný důvod jde vybrat jedním klikem. */
export const REASON_CHIPS: readonly string[] = [
  'test naživo',
  'edge neprokázán',
  'drawdown',
  'jiný režim trhu',
]

/** Délka důvodu jako v API (`note`: 3–500 znaků po ořezu mezer). */
export const NOTE_MIN = 3
export const NOTE_MAX = 500

/** Meze rozpočtu zkoušky — zrcadlo `TRIAL_BUDGET_*_RANGE` v engine
 *  `compute/setups.py` (rozhodnutí 1. 10. 2026); server je hlídá sám (422). */
export const TRIAL_BUDGET_SETUPS_RANGE = { min: 1, max: 20 } as const
export const TRIAL_BUDGET_R_RANGE = { min: 0.5, max: 6 } as const

export interface LibraryTrial {
  /** ISO UTC — začátek nastavil server při zahájení nebo obnovení. */
  started_at: string
  budget_setups: number
  /** Kladná velikost dovolené ztráty (3 = konec na Σ R ≤ −3). */
  budget_r: number
  /** Mechanika, na které zkouška začala; jiná než aktuální = zkouška skončila. */
  mechanics_version: number
  /** Setupy s přebitou bránou od začátku, uzavřené i otevřené. */
  setups: number
  /** Σ R uzavřených z nich. */
  sum_r: number
  /** Rozpočet vyčerpán (počet nebo ztráta). */
  spent: boolean
}

export interface LibraryCell {
  /** `NQ:trend_continuation` — ticker instance (ADR-0041) × šablona. */
  cell: string
  ticker: string
  template: string
  template_number: number
  /** Nastavené stádium (co „platí"). */
  stage: UserStage
  /** Co se teď uplatní: vyčerpaná zkouška nebo zkouška jiné mechaniky = auto. */
  effective_stage: UserStage
  trial: LibraryTrial | null
  gate_verdict: GateVerdict
  gate_n: number
  gate_lb: number | null
  avg_r: number | null
  avg_net_r: number | null
  /** Σ čistě v reálných mikro dolarech při skutečném sizingu (vzorek brány). */
  net_usd: number | null
  n_needed: number | null
  sessions: number
  per_session: number | null
  /** Kolik vzorků okno brány při dnešním tempu pojme (víc jich brána mít nebude). */
  window_capacity: number | null
  /** 0 = vzorek už stačí; null = nejde odhadnout, nebo ho okno nepojme. */
  sessions_to_decision: number | null
}

export interface LibraryBrakes {
  session: string
  day_r: number
  week_r: number
  daily_brake_r: number
  weekly_brake_r: number
  max_template_stops_per_day: number
  block: TradeBlock | null
  template_stops: Record<string, number>
}

/** Parametry, které Knihovna čte z platné verze (`GET /setups/params`). */
export interface LibraryParams {
  version: number | null
  createdTs: string | null
  gateDays: number
  gateMinSamples: number
  trialBudgetSetups: number
  trialBudgetR: number
  disabledTemplates: string[]
}

function numberParam(
  source: Record<string, unknown>,
  defaults: Record<string, unknown>,
  key: string,
  fallback: number,
): number {
  const value = source[key] ?? defaults[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

/** Platná verze parametrů → hodnoty Knihovny; chybějící klíč bere default serveru. */
export function libraryParams(response: SetupParamsResponse | null): LibraryParams {
  const defaults = response?.defaults ?? {}
  const source = response?.current?.params ?? defaults
  const disabled = source.disabled_templates ?? defaults.disabled_templates
  return {
    version: response?.current?.version ?? null,
    createdTs: response?.current?.created_ts ?? null,
    gateDays: numberParam(source, defaults, 'template_gate_days', 60),
    gateMinSamples: numberParam(source, defaults, 'template_gate_min_samples', 30),
    trialBudgetSetups: numberParam(source, defaults, 'trial_budget_setups', 10),
    trialBudgetR: numberParam(source, defaults, 'trial_budget_r', 3),
    disabledTemplates: Array.isArray(disabled)
      ? disabled.filter((item): item is string => typeof item === 'string')
      : [],
  }
}

function signed(value: number, digits: number): string {
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`
}

/** „T7 Pokračování trendu". */
export function cellTitle(cell: Pick<LibraryCell, 'template' | 'template_number'>): string {
  return `T${cell.template_number} ${TEMPLATE_LABELS[cell.template] ?? cell.template}`
}

/** Edge buňky neprokázán: dolní mez ØR ≤ 0 nebo ji vzorek ještě nedává. */
export function edgeUnproven(cell: Pick<LibraryCell, 'gate_lb'>): boolean {
  return cell.gate_lb === null || cell.gate_lb <= 0
}

/** Proč nastavená zkouška neplatí: vyčerpaný rozpočet, nebo změna mechaniky
 *  (zkouška platí jen na mechanice, na které začala). */
export type TrialEnd = 'budget' | 'mechanics'

/** Skončená zkouška buňky; null = zkouška platí, nebo žádná není. */
export function trialEnd(cell: Pick<LibraryCell, 'trial' | 'effective_stage'>): TrialEnd | null {
  if (cell.trial === null || cell.effective_stage === 'trial') return null
  return cell.trial.spent ? 'budget' : 'mechanics'
}

/** Krátce do hlavičky a dialogu. */
export const TRIAL_END_SHORT: Record<TrialEnd, string> = {
  budget: 'vyčerpaná',
  mechanics: 'jiná mechanika',
}

/** Štítek stádia v tabulce: skončená zkouška se hlásí jako Auto. */
export function stageLabel(cell: Pick<LibraryCell, 'stage' | 'effective_stage' | 'trial'>): string {
  const ended = trialEnd(cell)
  if (cell.stage === 'trial' && ended !== null)
    return ended === 'budget' ? 'Auto (zkouška vyčerpána)' : 'Auto (zkouška skončila)'
  return STAGE_LABELS[cell.stage]
}

/** Čerpání zkoušky: „3/10 · -1.0 z -3.0 R". */
export function trialUsageText(trial: LibraryTrial): string {
  return `${trial.setups}/${trial.budget_setups} · ${signed(trial.sum_r, 1)} z ${(-trial.budget_r).toFixed(1)} R`
}

/** Brána teď: „✓ pass", „✕ block", „· 24/30" (insufficient), „vypnuta". */
export function gateLabel(
  cell: Pick<LibraryCell, 'gate_verdict' | 'gate_n'>,
  minSamples: number,
): string {
  switch (cell.gate_verdict) {
    case 'pass':
      return '✓ pass'
    case 'block':
      return '✕ block'
    case 'insufficient':
      return `· ${cell.gate_n}/${minSamples}`
    case 'off':
      return 'vypnuta'
  }
}

/** Dolní mez do buňky tabulky: „LB −0.14"; bez dvou vzorků „LB —". */
export function lbText(lb: number | null): string {
  return lb === null ? 'LB —' : `LB ${signed(lb, 2)}`
}

const GATE_VERDICT_TEXT: Record<GateVerdict, string> = {
  pass: 'prošla — dolní mez ØR je kladná',
  block: 'zablokována — dolní mez ØR ≤ 0',
  insufficient: 'nedostatek vzorku',
  off: 'vypnuta (template_gate_enabled = false, jen přes API)',
}

/** Tooltip „Brána teď" — odrážky (vzor ivRankTooltip). */
export function gateTooltip(cell: LibraryCell, params: LibraryParams): string {
  return [
    `Brána šablon spočítaná teď (${cell.ticker}, ${cellTitle(cell)}):`,
    `• verdikt: ${GATE_VERDICT_TEXT[cell.gate_verdict]}`,
    `• vzorek: ${cell.gate_n} uzavřených setupů se stopem v rozpočtu (minimum ${params.gateMinSamples})`,
    `• dolní mez ØR (jednostranně 95 %): ${cell.gate_lb === null ? '— (pod 2 vzorky)' : `${signed(cell.gate_lb, 2)} R`}`,
    `• okno: posledních ${params.gateDays} seancí, aktuální mechanika, i stínové setupy`,
    '',
    'Počítají ji tytéž funkce jako engine u nového setupu — nejde o poslední uložený verdikt.',
  ].join('\n')
}

/** ØR do buňky: „+0.09 / +0.03". */
export function evidenceText(cell: Pick<LibraryCell, 'avg_r' | 'avg_net_r'>): string {
  const part = (value: number | null) => (value === null ? '—' : signed(value, 2))
  return `${part(cell.avg_r)} / ${part(cell.avg_net_r)}`
}

/** Tooltip ØR hrubě/čistě. */
export function evidenceTooltip(cell: LibraryCell): string {
  return [
    `Průměrný výsledek v R nad vzorkem brány (n = ${cell.gate_n}):`,
    `• hrubě: ${cell.avg_r === null ? '—' : `${signed(cell.avg_r, 2)} R`}`,
    `• čistě: ${cell.avg_net_r === null ? '—' : `${signed(cell.avg_net_r, 2)} R`} — po nákladech 1 mikra (ADR-0030: komise + skluz 1 tick na stranu)`,
    '• čistě = R − round-trip náklad mikra / (stop b × hodnota bodu mikra)',
    `• čistě v $: ${cell.net_usd === null ? '—' : formatPnlUsd(cell.net_usd)} — Σ za vzorek v reálných mikro dolarech při skutečném sizingu (kontrakty z kontextu setupu)`,
    '',
    'Krátký stop zvedá náklad v R i počet kontraktů — kladné ØR proto může v dolarech prodělat.',
  ].join('\n')
}

/** Průkaznost: „163 / 462"; bez odhadu „3 / —". */
export function proofText(cell: Pick<LibraryCell, 'gate_n' | 'n_needed'>): string {
  return `${cell.gate_n} / ${cell.n_needed ?? '—'}`
}

/** Tooltip průkaznosti. */
export function proofTooltip(cell: LibraryCell): string {
  return [
    'Průkaznost = n / n potřebné:',
    `• n: ${cell.gate_n} setupů ve vzorku brány`,
    cell.n_needed === null
      ? '• n potřebné: nejde odhadnout (pod 2 vzorky, nebo samé stejné výsledky pod minimem brány)'
      : `• n potřebné: ${cell.n_needed} — kolik vzorků prokáže edge +0.2 R (jednostranně 95 %, síla 80 %)`,
    '• vzorec: ((1.645 + 0.84) · σ / 0.2)², σ = rozptyl R téhož vzorku; nikdy pod minimum brány',
    '',
    'Výchozí řazení Knihovny: podle průkaznosti, ne podle ØR — malý vzorek s vysokým ØR nesmí svádět.',
  ].join('\n')
}

function sessionsWord(count: number): string {
  if (count === 1) return 'seance'
  return count >= 2 && count <= 4 ? 'seance' : 'seancí'
}

/** Okno brány při dnešním tempu nepojme vzorek potřebný na průkaz edge. */
export function windowTooShort(
  cell: Pick<LibraryCell, 'n_needed' | 'window_capacity' | 'gate_n'>,
): boolean {
  return (
    cell.n_needed !== null &&
    cell.window_capacity !== null &&
    cell.gate_n < cell.n_needed &&
    cell.window_capacity < cell.n_needed
  )
}

/** Rozhodnutelné: „~50 seancí", „vzorek stačí", „v okně nedosáhne · max ~337/462", „málo dat". */
export function decisionText(
  cell: Pick<LibraryCell, 'sessions_to_decision' | 'n_needed' | 'window_capacity' | 'gate_n'>,
): string {
  const value = cell.sessions_to_decision
  if (value === null) {
    if (cell.n_needed === null) return 'málo dat'
    if (windowTooShort(cell))
      return `v okně nedosáhne · max ~${cell.window_capacity}/${cell.n_needed}`
    return '—'
  }
  if (value === 0) return 'vzorek stačí'
  return `~${value} ${sessionsWord(value)}`
}

/** Tooltip odhadu v seancích. */
export function decisionTooltip(cell: LibraryCell, params: LibraryParams): string {
  const pace =
    cell.per_session === null
      ? '— (v okně brány žádná seance se setupem)'
      : `${cell.per_session.toFixed(1)} setupu za seanci (${cell.gate_n} za ${cell.sessions} ${sessionsWord(cell.sessions)} se setupem)`
  const capacity =
    cell.window_capacity === null
      ? '—'
      : `~${cell.window_capacity} vzorků (tempo × ${params.gateDays} seancí)`
  const lines = [
    'Za kolik seancí buňka nasbírá vzorek na průkaz edge:',
    '• (n potřebné − n) / tempo buňky za seanci',
    `• tempo: ${pace}`,
    `• okno brány při tomto tempu pojme: ${capacity} — starší setupy z brány vypadávají`,
  ]
  if (windowTooShort(cell))
    lines.push(
      `• ${cell.window_capacity} < ${cell.n_needed}: v okně ${params.gateDays} seancí vzorek nenaroste — edge +0.2 R brána s 80% silou neprokáže, rozhodne jen větší edge`,
    )
  lines.push('', 'Je to odhad při dnešním tempu, ne slib.')
  return lines.join('\n')
}

/** Tooltip stádia v tabulce. */
export function stageTooltip(cell: LibraryCell): string {
  const lines = [`Stádium ${cell.ticker} · ${cellTitle(cell)}: ${stageLabel(cell)}`]
  lines.push(`• ${STAGE_HELP[cell.effective_stage]}`)
  if (cell.trial !== null) {
    lines.push(
      `• zkouška od ${new Date(cell.trial.started_at).toLocaleString()}: ${trialUsageText(cell.trial)}`,
    )
    const ended = trialEnd(cell)
    if (ended === 'budget')
      lines.push('• rozpočet vyčerpán → rozhoduje brána (obnovit jde v dialogu)')
    if (ended === 'mechanics')
      lines.push(
        `• zkouška začala na mechanice v${cell.trial.mechanics_version}, po její změně skončila → rozhoduje brána (obnovit jde v dialogu)`,
      )
  }
  lines.push(
    '• sizing a brzdy platí ve všech stádiích',
    '',
    'Klik = změna stádia s povinným důvodem.',
  )
  return lines.join('\n')
}

/* ── Řazení a filtry tabulky ──────────────────────────────────────────────
Vstup: buňky v pořadí serveru (průkaznost sestupně) a pohled uživatele.
Výstup: viditelné řádky. Řadí se stabilně — shodné hodnoty drží pořadí
serveru, takže výchozí Průkaznost ↓ vrátí přesně jeho pořadí. */

/** Sloupec, podle kterého jde řadit. */
export type LibrarySortKey =
  'setup' | 'ticker' | 'stage' | 'gate' | 'avgR' | 'avgNetR' | 'proof' | 'decision'

export const LIBRARY_SORT_KEYS: readonly LibrarySortKey[] = [
  'setup',
  'ticker',
  'stage',
  'gate',
  'avgR',
  'avgNetR',
  'proof',
  'decision',
]

/** Záhlaví sloupců a volby řazení na mobilu. */
export const LIBRARY_SORT_LABELS: Record<LibrarySortKey, string> = {
  setup: 'Setup',
  ticker: 'Ticker',
  stage: 'Stádium',
  gate: 'Brána teď',
  avgR: 'ØR hrubě',
  avgNetR: 'ØR čistě',
  proof: 'Průkaznost',
  decision: 'Rozhodnutelné',
}

export type SortDir = 'asc' | 'desc'

/** Verdikty ve filtru brány; `off` (brána vypnutá přes API) ukáže jen „vše". */
export const LIBRARY_GATE_FILTERS = ['pass', 'block', 'insufficient'] as const
export type LibraryGateFilter = (typeof LIBRARY_GATE_FILTERS)[number]

export const GATE_FILTER_LABELS: Record<LibraryGateFilter, string> = {
  pass: '✓ pass',
  block: '✕ block',
  insufficient: '· nedostatek vzorku',
}

/** Řazení a filtry tabulky — pamatuje se v localStorage (`reviveLibraryView`). */
export interface LibraryView {
  sortKey: LibrarySortKey
  sortDir: SortDir
  /** null = všechny tickery. */
  ticker: string | null
  /** Platné stádium (`effective_stage`); prázdné = všechna. */
  stages: UserStage[]
  gate: LibraryGateFilter | null
  /** Hledání v názvu setupu („T7 Pokračování trendu"). */
  query: string
  /** Jen buňky s ØR čistě > 0. */
  netPositive: boolean
}

/** Výchozí pohled: dnešní řazení serveru (průkaznost sestupně), bez filtrů. */
export const DEFAULT_LIBRARY_VIEW: LibraryView = {
  sortKey: 'proof',
  sortDir: 'desc',
  ticker: null,
  stages: [],
  gate: null,
  query: '',
  netPositive: false,
}

/** Průkaznost jako podíl n / n potřebné; bez odhadu null (na konec). */
function proofRatio(cell: Pick<LibraryCell, 'gate_n' | 'n_needed'>): number | null {
  return cell.n_needed === null || cell.n_needed <= 0 ? null : cell.gate_n / cell.n_needed
}

/** Brána od nejhoršího: block → nedostatek vzorku → pass; `off` bez pořadí (na konec). */
const GATE_RANK: Record<GateVerdict, number | null> = {
  block: 0,
  insufficient: 1,
  pass: 2,
  off: null,
}

type SortValue = number | string | null

/** Rozhodnutelné: „v okně nedosáhne" je nejdál od rozhodnutí (nekonečno),
 *  neznámý odhad („málo dat", „—") nemá pořadí (null, na konec). */
function decisionValue(cell: LibraryCell): number | null {
  if (cell.sessions_to_decision !== null) return cell.sessions_to_decision
  return windowTooShort(cell) ? Number.POSITIVE_INFINITY : null
}

/** Hodnoty sloupce pro řazení; další prvek rozhoduje jen při shodě předchozích. */
function sortValues(cell: LibraryCell, key: LibrarySortKey): SortValue[] {
  switch (key) {
    case 'setup':
      return [cell.template_number]
    case 'ticker':
      return [cell.ticker]
    case 'stage':
      // Co se teď uplatní — skončená zkouška se hlásí i řadí jako Auto
      return [USER_STAGES.indexOf(cell.effective_stage)]
    case 'gate':
      return [GATE_RANK[cell.gate_verdict], cell.gate_lb]
    case 'avgR':
      return [cell.avg_r]
    case 'avgNetR':
      return [cell.avg_net_r]
    case 'proof':
      return [proofRatio(cell)]
    case 'decision':
      return [decisionValue(cell)]
  }
}

function compareCells(a: LibraryCell, b: LibraryCell, key: LibrarySortKey, dir: SortDir): number {
  const left = sortValues(a, key)
  const right = sortValues(b, key)
  for (let index = 0; index < left.length; index += 1) {
    const x = left[index]
    const y = right[index]
    if (x === null && y === null) continue
    // Prázdná hodnota na konec v obou směrech
    if (x === null) return 1
    if (y === null) return -1
    // Podle kódových jednotek jako `libraryTickers`: ticker je symbol, ne české
    // slovo (locale 'cs' řadí CH za H); dvě nekonečna jsou shoda, ne NaN
    const order = x < y ? -1 : x > y ? 1 : 0
    if (order !== 0) return dir === 'asc' ? order : -order
  }
  return 0
}

/** Seřadí kopii buněk; shodné hodnoty drží vstupní pořadí (stabilní řazení). */
export function sortLibraryCells(
  cells: readonly LibraryCell[],
  key: LibrarySortKey,
  dir: SortDir,
): LibraryCell[] {
  return [...cells].sort((a, b) => compareCells(a, b, key, dir))
}

/** Bez ohledu na velikost písmen a diakritiku („pokracovani" najde „Pokračování"). */
function searchable(text: string): string {
  return text
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .trim()
}

export function filterLibraryCells(
  cells: readonly LibraryCell[],
  view: LibraryView,
): LibraryCell[] {
  const query = searchable(view.query)
  return cells.filter(
    (cell) =>
      (view.ticker === null || cell.ticker === view.ticker) &&
      (view.stages.length === 0 || view.stages.includes(cell.effective_stage)) &&
      (view.gate === null || cell.gate_verdict === view.gate) &&
      (!view.netPositive || (cell.avg_net_r !== null && cell.avg_net_r > 0)) &&
      (query === '' || searchable(cellTitle(cell)).includes(query)),
  )
}

/** Viditelné řádky tabulky: filtry, pak řazení. */
export function libraryRows(cells: readonly LibraryCell[], view: LibraryView): LibraryCell[] {
  return sortLibraryCells(filterLibraryCells(cells, view), view.sortKey, view.sortDir)
}

/** Klik na záhlaví: nový sloupec vzestupně, týž sloupec obrátí směr. */
export function nextLibrarySort(view: LibraryView, key: LibrarySortKey): LibraryView {
  if (view.sortKey === key) return { ...view, sortDir: view.sortDir === 'asc' ? 'desc' : 'asc' }
  return { ...view, sortKey: key, sortDir: 'asc' }
}

/** Přepne stádium ve filtru; pořadí voleb drží Auto → Stín → Zkouška. */
export function toggleLibraryStage(view: LibraryView, stage: UserStage): LibraryView {
  const selected = view.stages.includes(stage)
    ? view.stages.filter((item) => item !== stage)
    : [...view.stages, stage]
  return { ...view, stages: USER_STAGES.filter((item) => selected.includes(item)) }
}

export function libraryFiltersActive(view: LibraryView): boolean {
  return (
    view.ticker !== null ||
    view.stages.length > 0 ||
    view.gate !== null ||
    view.query.trim() !== '' ||
    view.netPositive
  )
}

/** Zrušit filtry: řazení zůstává. */
export function clearLibraryFilters(view: LibraryView): LibraryView {
  return { ...DEFAULT_LIBRARY_VIEW, sortKey: view.sortKey, sortDir: view.sortDir }
}

/** Tickery do filtru: z dat a vybraný, i když z dat zmizel (ať je filtr vidět a jde zrušit). */
export function libraryTickers(cells: readonly LibraryCell[], selected: string | null): string[] {
  const tickers = new Set(cells.map((cell) => cell.ticker))
  if (selected !== null) tickers.add(selected)
  return [...tickers].sort()
}

const SORT_HELP: Record<LibrarySortKey, string> = {
  setup: 'číslo šablony (T1, T2, …)',
  ticker: 'ticker abecedně',
  stage: 'platné stádium Auto → Stín → Zkouška (skončená zkouška = Auto)',
  gate: 'verdikt block → nedostatek vzorku → pass, uvnitř verdiktu dolní mez LB (bez LB poslední)',
  avgR: 'ØR hrubě',
  avgNetR: 'ØR čistě (po nákladech ADR-0030)',
  proof: 'podíl n / n potřebné',
  decision: 'seance do rozhodnutí („vzorek stačí" = 0, „v okně nedosáhne" = nejdál)',
}

/** Tooltip záhlaví — odrážky (vzor ivRankTooltip). */
export function librarySortTooltip(key: LibrarySortKey): string {
  return [
    `Řadit podle: ${SORT_HELP[key]}`,
    '• klik = vzestupně, další klik = sestupně',
    '• bez hodnoty („—", „málo dat", brána „vypnuta") vždy na konec',
    '• výchozí: Průkaznost sestupně (pořadí serveru)',
  ].join('\n')
}

/** Delší hledaný text nedává smysl a do úložiště nepatří; drží ho i pole hledání. */
export const LIBRARY_QUERY_MAX = 100

const SORT_DIRS: readonly SortDir[] = ['asc', 'desc']

/** Hodnota z povolené množiny (select, localStorage), jinak null. Kontrolu dělá
 *  `oneOf`; '' v žádné z množin není, takže jen značí neplatnou hodnotu. */
function optionOf<T extends string>(allowed: readonly T[], value: unknown): T | null {
  return oneOf<T | ''>(allowed)(value, '') || null
}

/** Sloupec z výběru řazení (mobil) nebo z úložiště; neznámý = null. */
export function librarySortKeyOf(value: unknown): LibrarySortKey | null {
  return optionOf(LIBRARY_SORT_KEYS, value)
}

/** Filtr brány z výběru nebo z úložiště; „vše" ('') nebo neznámý = null. */
export function libraryGateFilterOf(value: unknown): LibraryGateFilter | null {
  return optionOf(LIBRARY_GATE_FILTERS, value)
}

/** Reviver uloženého pohledu (`usePersistentState`): neplatné pole → jeho výchozí hodnota. */
export function reviveLibraryView(value: unknown, fallback: LibraryView): LibraryView {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return fallback
  const raw = value as Record<string, unknown>
  const sortKey = librarySortKeyOf(raw.sortKey)
  const sortDir = optionOf(SORT_DIRS, raw.sortDir)
  // Řazení platí jen jako dvojice: zastaralý sloupec s uloženým směrem by
  // z výchozí Průkaznosti ↓ udělal Průkaznost ↑ (nejméně průkazné nahoře)
  const sortStored = sortKey !== null && sortDir !== null
  const stages: unknown = raw.stages
  return {
    sortKey: sortStored ? sortKey : fallback.sortKey,
    sortDir: sortStored ? sortDir : fallback.sortDir,
    // null = všechny tickery; jinak jen tvar symbolu (`shortString`, #554 L5)
    ticker: raw.ticker === null ? null : shortString()(raw.ticker, '') || fallback.ticker,
    stages: Array.isArray(stages)
      ? USER_STAGES.filter((stage) => (stages as unknown[]).includes(stage))
      : fallback.stages,
    gate: raw.gate === null ? null : (libraryGateFilterOf(raw.gate) ?? fallback.gate),
    query: typeof raw.query === 'string' ? raw.query.slice(0, LIBRARY_QUERY_MAX) : fallback.query,
    netPositive: typeof raw.netPositive === 'boolean' ? raw.netPositive : fallback.netPositive,
  }
}

/** Odpověď API s chybou → čitelný text (detail je string, u pydantic pole). */
export function apiErrorText(detail: unknown, status: number): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) =>
        typeof item === 'object' && item !== null && typeof item.msg === 'string' ? item.msg : null,
      )
      .filter((item): item is string => item !== null)
    if (messages.length > 0) return messages.join('; ')
  }
  return `HTTP ${status}`
}

export interface StageChange {
  cell: string
  stage: UserStage
  note: string
  /** Jen pro zkoušku; chybí = výchozí rozpočet z parametrů. */
  budgetSetups?: number
  budgetR?: number
}

/** Změna stádia jedné buňky → nová verze parametrů; chyba jako text, ne výjimka. */
export async function saveSetupStage(
  change: StageChange,
): Promise<{ ok: true; version: number } | { ok: false; error: string }> {
  const body: Record<string, unknown> = {
    cell: change.cell,
    stage: change.stage,
    note: change.note,
    created_by: 'ui',
  }
  if (change.stage === 'trial') {
    if (change.budgetSetups !== undefined) body.budget_setups = change.budgetSetups
    if (change.budgetR !== undefined) body.budget_r = change.budgetR
  }
  try {
    const response = await fetch(`${API_BASE}/setups/stage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!response.ok) {
      const payload = (await response.json().catch(() => ({}))) as { detail?: unknown }
      return { ok: false, error: apiErrorText(payload.detail, response.status) }
    }
    const stored = (await response.json()) as { version?: number }
    return { ok: true, version: stored.version ?? 0 }
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) }
  }
}
