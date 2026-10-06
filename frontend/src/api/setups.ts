/** Setup detektor (ADR-0004): REST klient a české popisky šablon. */
import { API_BASE } from '../config'
import type { LibraryBrakes, LibraryCell, UserStage } from './setupLibrary'

export interface SetupRow {
  id: number
  symbol: string
  expiry: string
  template: string
  direction: 'long' | 'short'
  created_ts: string
  entry: number
  target: number
  stop: number
  confidence: number
  reason: string
  status: 'active' | 'closed_target' | 'closed_stop' | 'closed_timeout'
  closed_ts: string | null
  outcome_r: number | null
  mfe: number | null
  mae: number | null
  user_rating: number | null
  user_note: string | null
  /** Verze mechaniky detektoru, která setup vyrobila (#311); starší řádky 1. */
  mechanics_version?: number
  /** Kontext vzniku (gex_regime, …) — podklad režimových statistik (#402). */
  context?: Record<string, unknown> | null
  /** Vznikl po settle vlastní expirace (#1324, server `born_after_settle`) —
   *  nemohl existovat, souhrn, brzdy ani brána ho nepočítají. */
  after_settle?: boolean
  /** Důvod trvalého vyřazení ze statistik (#1346, `context.excluded`), např.
   *  `vstup_mimo_bary` = vznik nad zamrzlým spotem; null = bez značky. */
  excluded?: string | null
}

/** Zrcadlo `SETUP_MECHANICS_VERSION` v enginu (#311) — UŽ JEN pro testy.

Aktuální mechaniku posílá server v souhrnu (`SetupsSummary.mechanics_version`,
#1319): tahle konstanta už jednou zastarala (2 vs. engine 4) a statistiky
Setupů týden neviděly aktuální setupy. */
export const CURRENT_MECHANICS_VERSION = 4

export const TEMPLATE_LABELS: Record<string, string> = {
  wall_bounce: 'Odraz od zdi',
  failed_break: 'Neúspěšný průraz',
  max_pain_pin: 'Max Pain pin',
  gamma_momentum: 'Gamma momentum',
  divergence_spring: 'Divergenční spring',
  trend_continuation: 'Pokračování trendu',
}

export const STATUS_LABELS: Record<SetupRow['status'], string> = {
  active: 'Aktivní',
  closed_target: 'Cíl',
  closed_stop: 'Stop',
  closed_timeout: 'Timeout',
}

export function templateLabel(template: string): string {
  return TEMPLATE_LABELS[template] ?? template
}

/** RRR z uložených úrovní (predikce je neměnná — počítá se ze setupu, ne z běhu). */
export function setupRrr(row: Pick<SetupRow, 'entry' | 'target' | 'stop'>): number {
  const risk = Math.abs(row.entry - row.stop)
  return risk > 0 ? Math.abs(row.target - row.entry) / risk : 0
}

/** P/L uzavřeného setupu v USD na 1 KONTRAKT (#185).

`outcome_r` je výsledek v násobcích rizika; riziko v bodech = |entry − stop|,
takže P/L body = outcome_r × riziko a dolary přes hodnotu bodu instrumentu.
Platí i pro timeout (engine počítá outcome_r z exit ceny). */
export function setupPnlUsd(
  row: Pick<SetupRow, 'entry' | 'stop' | 'outcome_r'>,
  pointValueUsd: number,
): number | null {
  if (row.outcome_r === null) return null
  return row.outcome_r * Math.abs(row.entry - row.stop) * pointValueUsd
}

/** Expected Value na obchod (#911) ze serverového souhrnu (#1319):
(WinRate × AvgWin) − (LossRate × AvgLoss).

Matematicky ≡ prostý průměr výsledků (v R je to přesně Ø R) — hodnota je ve
viditelném ROZKLADU: trader vidí, jestli EV táhne win rate, velikost výher,
nebo ho zabíjí velikost proher. `avg_loss` je kladné číslo. */
export interface EvBreakdown {
  ev: number
  win_rate: number
  loss_rate: number
  avg_win: number
  avg_loss: number
  n: number
}

/** Tooltip EV — odřádkovaný (konvence 27. 8.); rozklad + čtení znaménka. */
export function evTooltip(stats: EvBreakdown, unit: string): string {
  const pct = (value: number) => `${Math.round(100 * value)} %`
  return [
    'Expected Value = průměrný očekávaný výsledek NA OBCHOD:',
    `(WinRate × AvgWin) − (LossRate × AvgLoss)`,
    `= ${pct(stats.win_rate)} × ${stats.avg_win.toFixed(0)} ${unit} − ${pct(stats.loss_rate)} × ${stats.avg_loss.toFixed(0)} ${unit}`,
    `= ${stats.ev >= 0 ? '+' : ''}${stats.ev.toFixed(0)} ${unit} (n=${stats.n})`,
    '',
    'Čtení:',
    '• EV > 0 — přístup dlouhodobě vydělává peníze',
    '• EV < 0 — přístup dlouhodobě ztrácí peníze',
    '',
    'V jednotkách R je EV totéž co Ø R — tady je v penězích a s rozkladem,',
    'ať je vidět, KTERÁ složka výsledek táhne.',
  ].join('\n')
}

/** Formát P/L se znaménkem („+512.5 $" / „-250 $"). */
export function formatPnlUsd(value: number): string {
  const rounded = Math.round(value * 100) / 100
  return `${rounded > 0 ? '+' : ''}${rounded} $`
}

/** Startovní kapitál účtu v jednotkách aplikace (#191 → #1185, rozhodnutí 15. 9. 2026).

Aplikace počítá v plných kontraktech ES/NQ, uživatel obchoduje MES/MNQ (1/10):
reálných 5 000 $ na mikro ≡ 50 000 $ v aplikaci, 1 kontrakt aplikace = 1 mikro.
Body sedí 1:1, dolary jsou ×10. Báze procent P/L. */
export const ACCOUNT_START_USD = 50000

/** P/L setupu v % startovního účtu (#191): pnl $ / 50 000 $ (jednotky aplikace).

S fixní bází je součet procent setupů roven celkovému zhodnocení účtu. */
export function setupPnlPct(
  row: Pick<SetupRow, 'entry' | 'stop' | 'outcome_r'>,
  pointValueUsd: number,
): number | null {
  const pnl = setupPnlUsd(row, pointValueUsd)
  if (pnl === null) return null
  return (pnl / ACCOUNT_START_USD) * 100
}

/** Formát procenta se znaménkem („+0.19 %"). */
export function formatPct(value: number): string {
  return `${value > 0 ? '+' : ''}${value.toFixed(2)} %`
}

/** Stránka tabulky setupů: posledních ≤ 200 podle vzniku + počet všech (#1319).

Stránka je JEN pro tabulku — souhrny počítá server (`fetchSetupsSummary`)
z celé historie; agregace nad stránkou by byla klouzavé okno. `date`
(UTC den vzniku, YYYY-MM-DD) zúží výpis na jeden den. */
export interface SetupsPage {
  setups: SetupRow[]
  /** Všechny řádky se stejnými filtry; null = server počet nedodal (DB nedostupná). */
  totalCount: number | null
}

export async function fetchSetups(symbol: string, date?: string): Promise<SetupsPage> {
  const query = date ? `?date=${encodeURIComponent(date)}` : ''
  const response = await fetch(`${API_BASE}/setups/${symbol}${query}`)
  if (!response.ok) return { setups: [], totalCount: null }
  const payload = (await response.json()) as { setups?: SetupRow[]; total_count?: number | null }
  return {
    setups: payload.setups ?? [],
    totalCount: typeof payload.total_count === 'number' ? payload.total_count : null,
  }
}

// ── Souhrn setupů z celé historie (#1319) — počítá server ─────────────────
//
// `GET /setups/summary` (engine `compute/setup_summary.py`): UI nic
// nesčítá, jen vykresluje. Tvar zrcadlí dataclassy souhrnu.

export interface SummaryGroup {
  count: number
  active: number
  closed: number
  wins: number
  losses: number
  /** 0–1; null = nic uzavřeného. */
  win_rate: number | null
  sum_r: number
  avg_r: number | null
  /** Na 1 kontrakt: Σ R × stop × hodnota bodu. */
  gross_usd: number
  /** Na 1 kontrakt: uzavřené × poplatek za kontrakt a obchod. */
  fees_usd: number
  net_usd: number
  ev_r: EvBreakdown | null
  /** Hrubě na 1 kontrakt (před poplatky). */
  ev_usd: EvBreakdown | null
}

export interface SummaryAccount {
  trades: number
  gross_usd: number
  fees_usd: number
  net_usd: number
  net_pct: number
  /** ≤ 0 */
  max_drawdown_usd: number
}

export interface SummaryDay {
  session: string
  trades: number
  closed: number
  active: number
  wins: number
  losses: number
  win_rate: number | null
  best_usd: number | null
  worst_usd: number | null
  gross_usd: number
  fees_usd: number
  net_usd: number
  gross_pct: number
  max_risk_pct: number
  total_risk_pct: number
  account: SummaryAccount | null
}

export interface GateBucket {
  n: number
  avg_r: number
  win_rate: number
}

export type BandGateSummary = Record<'simple' | 'regime', Record<'pass' | 'block', GateBucket>>

export interface RegimeRow {
  template: string
  regime: string
  n: number
  wins: number
  win_rate: number
}

export interface SharpeValue {
  sharpe: number | null
  days: number
}

export interface SetupsPerformance {
  daily: { session: string; trades: number; sum_r: number; cum_r: number }[]
  sharpe_all: SharpeValue
  sharpe_30: SharpeValue
  max_drawdown_r: number
  simulation: { traded: number; skipped: number; total_usd: number; sharpe: SharpeValue } | null
}

export interface SetupsSummary {
  symbols: string[]
  mechanics_version: number
  all_versions: boolean
  total_count: number
  legacy_count: number
  /** Setupy vzniklé po settle vlastní expirace (#1324) — vyřazené ze všech čísel souhrnu. */
  after_settle_count: number
  /** Setupy se značkou vyřazení (#1346, vznik nad zamrzlým spotem) — vyřazené stejně. */
  excluded_count: number
  fee_per_contract_usd: number
  account_usd: number
  unpriced_symbols: string[]
  all: SummaryGroup
  tradeable: SummaryGroup
  /** Obchodovatelné jen díky zkoušce (#1323, `gate_overridden`) — podmnožina
   *  `tradeable`; chybí u API před #1323. */
  trial?: SummaryGroup
  shadow: SummaryGroup
  unruled: SummaryGroup
  shadow_reasons: Record<string, number>
  account: SummaryAccount | null
  today: SummaryDay
  band_gates: BandGateSummary | null
  regimes: RegimeRow[]
  performance: SetupsPerformance
  /** Knihovna setupů (#1323): buňky ticker × šablona; chybí u API před #1323. */
  cells?: LibraryCell[]
  /** Stav brzd účtu teď (#1323, hlavička Knihovny); chybí u API před #1323. */
  brakes?: LibraryBrakes
}

export interface SummaryOptions {
  /** Včetně starších verzí mechaniky (#311); výchozí jen aktuální. */
  allVersions?: boolean
  /** Účet a % rizika kalkulačky (#679) → USD simulace mikro kontrakty. */
  simulation?: { accountUsd: number; riskPct: number }
}

/** Souhrn setupů symbolů z celé historie; null = server ho nedodal (chyba se ukáže, nic se nedopočítává). */
export async function fetchSetupsSummary(
  symbols: string[],
  options: SummaryOptions = {},
): Promise<SetupsSummary | null> {
  const params = new URLSearchParams({ symbols: symbols.join(',') })
  if (options.allVersions) params.set('all_versions', 'true')
  if (options.simulation) {
    params.set('sim_account_usd', String(options.simulation.accountUsd))
    params.set('sim_risk_pct', String(options.simulation.riskPct))
  }
  try {
    const response = await fetch(`${API_BASE}/setups/summary?${params.toString()}`)
    if (!response.ok) return null
    const payload = (await response.json()) as Partial<SetupsSummary> | null
    if (!payload || typeof payload !== 'object' || typeof payload.all !== 'object') return null
    return payload as SetupsSummary
  } catch {
    return null
  }
}

/** Ruční hodnocení uzavřeného setupu (kvalitativní vrstva — nevstupuje do kalibrace). */
export async function reviewSetup(
  symbol: string,
  id: number,
  rating: 1 | -1 | null,
  note: string | null,
): Promise<boolean> {
  const response = await fetch(`${API_BASE}/setups/${symbol}/${id}/review`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rating, note }),
  })
  return response.ok
}

// ── Poloha v tlumící zóně a stínová brána (#1060, fáze 2 z #575) ──────────
//
// Engine zapíše do `context` třídu polohy podle `band_depth`, posun confidence
// (varianta B: +10 uvnitř, 0 přechod, −15 mimo / bez pásma) a verdikty dvou
// STÍNOVÝCH pravidel — nic se neblokuje, setup vzniká vždy. UI je jen ukazuje;
// vyhodnocení ~5. 10. 2026 rozhodne, zda se některé pravidlo zapne naostro.

export type BandClass = 'inside' | 'transition' | 'outside' | 'no_zone'
export type BandGate = 'pass' | 'block' | 'unknown'

export const BAND_CLASS_LABELS: Record<BandClass, string> = {
  inside: 'uvnitř pásma',
  transition: 'přechod',
  outside: 'mimo pásmo',
  no_zone: 'bez pásma',
}

export interface BandInfo {
  bandClass: BandClass
  /** Posun confidence v bodech procent (může být 0). */
  adjust: number
  /** Základní confidence šablony před posunem; null u starších řádků. */
  confidenceBase: number | null
  gateSimple: BandGate
  gateRegime: BandGate
  depth: number | null
}

const BAND_CLASSES: readonly string[] = ['inside', 'transition', 'outside', 'no_zone']
const BAND_GATES: readonly string[] = ['pass', 'block', 'unknown']

function gateOf(value: unknown): BandGate | null {
  return typeof value === 'string' && BAND_GATES.includes(value) ? (value as BandGate) : null
}

/** Poloha setupu v zóně z `context`; null = řádek bránu nenese (starší setup,
minuta bez Dyn profilu) — kreslí se nic, ne „neznámé". */
export function bandInfo(row: Pick<SetupRow, 'context'>): BandInfo | null {
  const context = row.context ?? {}
  const bandClass = context.band_class
  if (typeof bandClass !== 'string' || !BAND_CLASSES.includes(bandClass)) return null
  const gateSimple = gateOf(context.band_gate_simple)
  const gateRegime = gateOf(context.band_gate_regime)
  if (gateSimple === null || gateRegime === null) return null
  const adjust = context.confidence_band_adjust
  const base = context.confidence_base
  const depth = context.band_depth
  return {
    bandClass: bandClass as BandClass,
    adjust: typeof adjust === 'number' ? adjust : 0,
    confidenceBase: typeof base === 'number' ? base : null,
    gateSimple,
    gateRegime,
    depth: typeof depth === 'number' ? depth : null,
  }
}

/** Tooltip u čísla důvěry (#794 fáze 2B): odkud základ pochází a co se k němu přičetlo.
Bez `confidence_source` (starší setup) vrací null — nic se nevymýšlí. */
export function confidenceTooltip(row: Pick<SetupRow, 'confidence' | 'context'>): string | null {
  const context = row.context ?? {}
  const source = context.confidence_source
  if (typeof source !== 'string') return null
  const base = typeof context.confidence_base === 'number' ? context.confidence_base : null
  const template =
    typeof context.confidence_template === 'number' ? context.confidence_template : null
  const adjust =
    typeof context.confidence_band_adjust === 'number' ? context.confidence_band_adjust : 0
  const lines = [
    `Důvěra ${row.confidence} % = základ${base === null ? '' : ` ${base} %`}${adjust === 0 ? '' : ` ${adjust > 0 ? '+' : '−'}${Math.abs(adjust)} poloha v pásmu`}.`,
    source === 'constant'
      ? `Základ = konstanta šablony${template === null ? '' : ` (${template} %)`} — track record koše zatím pod minimem vzorku.`
      : `Základ = Wilsonova dolní mez úspěšnosti z track recordu (${source.replace(/^wilson /, '')}).`,
    '',
    'Kalibrace z uzavřených setupů aktuální mechaniky (#794 fáze 2B); koše od nejkonkrétnějšího:',
    '• symbol × šablona × gamma režim → šablona × režim → symbol × šablona → šablona.',
  ]
  return lines.join('\n')
}

/** Štítek polohy s posunem confidence („uvnitř pásma +10", „přechod ±0"). */
export function bandLabel(info: BandInfo): string {
  const shift = info.adjust === 0 ? '±0' : `${info.adjust > 0 ? '+' : '−'}${Math.abs(info.adjust)}`
  return `${BAND_CLASS_LABELS[info.bandClass]} ${shift}`
}

const GATE_LABELS: Record<BandGate, string> = {
  pass: 'prošel by',
  block: 'byl by zablokován',
  unknown: 'nerozhodnuto (neznámý gamma režim)',
}

/** Tooltip polohy — odřádkovaný s odrážkami (konvence 27. 8.). */
export function bandTooltip(info: BandInfo): string {
  const depth = info.depth === null ? '' : ` (hloubka ${info.depth.toFixed(2)})`
  const adjusted =
    info.confidenceBase === null
      ? ''
      : ` — základ šablony ${info.confidenceBase} %, po úpravě ${Math.max(0, Math.min(100, info.confidenceBase + info.adjust))} %`
  return [
    `Poloha entry v tlumící zóně Dyn GEX: ${BAND_CLASS_LABELS[info.bandClass]}${depth}.`,
    `Úprava důvěry: ${info.adjust > 0 ? '+' : ''}${info.adjust} b.${adjusted}`,
    '',
    'Stínová brána (#1060) — setup vznikl, jen se zapisuje, co by pravidlo udělalo:',
    `• jen poloha: ${GATE_LABELS[info.gateSimple]}`,
    `• poloha × gamma režim: ${GATE_LABELS[info.gateRegime]}`,
    '',
    'Škála hloubky: −1 bez zóny · 0 hrana All · 1 hrana Major · 2 vrchol profilu.',
    'Vyhodnocení pass vs. block ~5. 10. 2026 na mechanice v5 rozhodne, zda se pravidlo zapne.',
  ].join('\n')
}

/** Text dlaždice skupiny: „n · Ø R" (bez vzorku pomlčka). */
export function formatGateBucket(bucket: GateBucket): string {
  if (bucket.n === 0) return '—'
  return `${bucket.n} · ${bucket.avg_r >= 0 ? '+' : ''}${bucket.avg_r.toFixed(2)} R`
}

// ── Risk framework malého účtu (#1185, varianta A) ─────────────────────────
//
// Engine u každého setupu spočítá sizing (kontrakty = ⌊účet × riziko % /
// (stop b × hodnota bodu)⌋), brzdy (−3 R den, −6 R týden, 2 stopy šablony za
// den) a bránu šablon (dolní mez očekávání > 0 při n ≥ 30 za 60 seancí; od #1325
// per šablona × symbol, `risk_rules_version` 2). Od #1323 (`risk_rules_version`
// 3) i stádium buňky ticker × šablona: Stín (`trade_block` `user`, po brzdách)
// a Zkouška, která přebije bránu (`gate_overridden`), sizing ani brzdy ne.
// Setup vzniká vždy; `tradeable` říká, zda se dá zobchodovat, `trade_block`
// proč ne. UI jen zobrazuje — nic nepřepočítává.

export type TradeBlock =
  'stop_over_budget' | 'stop_over_cap' | 'daily_brake' | 'weekly_brake' | 'template_stops' | 'user' | 'gate' // prettier-ignore

export const TRADE_BLOCK_LABELS: Record<TradeBlock, string> = {
  stop_over_budget: 'stop nad rozpočtem rizika',
  stop_over_cap: 'stop nad tvrdým stropem',
  daily_brake: 'denní brzda',
  weekly_brake: 'týdenní brzda',
  template_stops: 'strop stopů šablony',
  user: 've stínu z rozhodnutí uživatele',
  gate: 'šablona bez prokázaného edge',
}

const TRADE_BLOCKS: readonly string[] = Object.keys(TRADE_BLOCK_LABELS)

export type GateVerdict = 'pass' | 'block' | 'insufficient' | 'off'

export interface RiskInfo {
  tradeable: boolean
  affordable: boolean
  block: TradeBlock | null
  contracts: number
  riskBudgetUsd: number
  maxLossUsd: number
  feeUsd: number
  stopPoints: number
  accountUsd: number
  gate: GateVerdict | null
  gateN: number | null
  gateLb: number | null
  dayR: number | null
  weekR: number | null
  /** Stádium buňky při vzniku (#1323); null = řádek před #1323 (= Auto). */
  userStage: UserStage | null
  /** Obchodovatelný jen díky zkoušce (#1323) — brána by ho jinak zastavila. */
  gateOverridden: boolean
  /** Čerpání zkoušky při vzniku; null = buňka zkoušku neměla. */
  trial: { setups: number; budgetSetups: number; sumR: number; budgetR: number } | null
}

/** Risk kontext setupu (#1185); null = řádek vznikl před pravidly — nic se nevymýšlí. */
export function riskInfo(row: Pick<SetupRow, 'context'>): RiskInfo | null {
  const context = row.context ?? {}
  if (typeof context.tradeable !== 'boolean' || typeof context.contracts !== 'number') return null
  const num = (key: string): number | null =>
    typeof context[key] === 'number' ? (context[key] as number) : null
  const block = context.trade_block
  const gate = context.template_gate
  const stage = context.user_stage
  const trialSetups = num('trial_setups')
  const trialBudgetSetups = num('trial_budget_setups')
  const trialSumR = num('trial_sum_r')
  const trialBudgetR = num('trial_budget_r')
  return {
    tradeable: context.tradeable,
    affordable: context.affordable === true,
    block: typeof block === 'string' && TRADE_BLOCKS.includes(block) ? (block as TradeBlock) : null,
    contracts: context.contracts,
    riskBudgetUsd: num('risk_budget_usd') ?? 0,
    maxLossUsd: num('max_loss_usd') ?? 0,
    feeUsd: num('fee_usd') ?? 0,
    stopPoints: num('stop_points') ?? 0,
    accountUsd: num('account_equity_usd') ?? ACCOUNT_START_USD,
    gate:
      gate === 'pass' || gate === 'block' || gate === 'insufficient' || gate === 'off'
        ? gate
        : null,
    gateN: num('template_gate_n'),
    gateLb: num('template_gate_lb'),
    dayR: num('realized_day_r'),
    weekR: num('realized_week_r'),
    userStage: stage === 'auto' || stage === 'shadow' || stage === 'trial' ? stage : null,
    gateOverridden: context.gate_overridden === true,
    trial:
      trialSetups !== null &&
      trialBudgetSetups !== null &&
      trialSumR !== null &&
      trialBudgetR !== null
        ? {
            setups: trialSetups,
            budgetSetups: trialBudgetSetups,
            sumR: trialSumR,
            budgetR: trialBudgetR,
          }
        : null,
  }
}

function blockText(info: RiskInfo): string {
  return info.block === null ? 'neobchodovatelný' : TRADE_BLOCK_LABELS[info.block]
}

/** Štítek do tabulky: „1 ks · 500 $", „1 ks · 500 $ · zkouška 3/10" nebo „stín: denní brzda". */
export function riskLabel(info: RiskInfo): string {
  if (info.tradeable) {
    const base = `${info.contracts} ks · ${Math.round(info.maxLossUsd)} $`
    if (!info.gateOverridden) return base
    return info.trial === null
      ? `${base} · zkouška`
      : `${base} · zkouška ${info.trial.setups}/${info.trial.budgetSetups}`
  }
  return `stín: ${blockText(info)}`
}

/** P/L uzavřeného setupu pro ÚČET (kontrakty × R × stop × bod − poplatky); null u stínu/aktivního. */
export function accountPnlUsd(row: Pick<SetupRow, 'outcome_r' | 'context'>): number | null {
  const info = riskInfo(row)
  if (info === null || !info.tradeable || row.outcome_r === null) return null
  return row.outcome_r * info.maxLossUsd - info.feeUsd
}

const GATE_TEXT: Record<GateVerdict, string> = {
  pass: 'prošla (dolní mez očekávání > 0)',
  block: 'zablokována (dolní mez očekávání ≤ 0)',
  insufficient: 'nedostatek vzorku (n < minimum)',
  off: 'vypnuta',
}

function signed(value: number, digits: number): string {
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`
}

/** Tooltip risk štítku — odřádkovaný s odrážkami (konvence 27. 8.). */
export function riskTooltip(info: RiskInfo): string {
  const gate =
    info.gate === null
      ? '—'
      : `${GATE_TEXT[info.gate]}${info.gateN === null ? '' : ` · n=${info.gateN}`}${info.gateLb === null ? '' : ` · LB ${signed(info.gateLb, 2)} R`}`
  const day = info.dayR === null ? '—' : `${signed(info.dayR, 1)} R`
  const week = info.weekR === null ? '—' : `${signed(info.weekR, 1)} R`
  const stage = stageText(info)
  return [
    info.tradeable
      ? `Obchodovatelný: ${info.contracts} kontrakt(y), ztráta na stopu ${Math.round(info.maxLossUsd)} $ (+ poplatky ${Math.round(info.feeUsd)} $).`
      : `Stínový setup — neobchodovat: ${blockText(info)}.`,
    `Rozpočet rizika ${Math.round(info.riskBudgetUsd)} $ z účtu ${Math.round(info.accountUsd)} $ (aplikace = plné kontrakty; 1 kontrakt zde = 1 MES/MNQ reálně, dolary ÷ 10).`,
    `Stop ${info.stopPoints.toFixed(2)} b.`,
    '',
    'Pravidla (#1185):',
    `• brána šablony: ${gate}`,
    ...(stage === null ? [] : [`• stádium v Knihovně: ${stage}`]),
    `• brzdy: dnes ${day}, týden ${week} (−3 R den zastaví nové obchody do konce seance (17:00 CT), −6 R týden do konce obchodního týdne (neděle 17:00 CT))`,
    '• stínové setupy se dál měří, jen se neobchodují a nechodí do pushe',
  ].join('\n')
}

/** Stádium buňky při vzniku do tooltipu (#1323); null = řádek před #1323. */
function stageText(info: RiskInfo): string | null {
  if (info.userStage === null) return null
  const trial =
    info.trial === null
      ? ''
      : ` (${info.trial.setups}/${info.trial.budgetSetups} setupů, ${signed(info.trial.sumR, 1)} z ${(-info.trial.budgetR).toFixed(1)} R)`
  if (info.gateOverridden) return `Zkouška — přebila bránu${trial}`
  if (info.userStage === 'shadow') return 'Stín — rozhodnutí uživatele'
  if (info.userStage === 'trial') return `Zkouška${trial}`
  // Auto s klíči zkoušky = vyčerpaná zkouška (engine ji vrací na Auto bez zápisu)
  return info.trial === null ? 'Auto' : `Auto — zkouška vyčerpána${trial}`
}

// ── Parametry setupů (#794 fáze 2) vč. risk parametrů (#1185) ─────────────

export interface SetupParamsVersion {
  version: number
  created_ts: string
  created_by: string
  note: string
  params: Record<string, unknown>
}

export interface SetupParamsResponse {
  current: SetupParamsVersion | null
  defaults: Record<string, unknown>
}

export async function fetchSetupParams(): Promise<SetupParamsResponse | null> {
  try {
    const response = await fetch(`${API_BASE}/setups/params`)
    if (!response.ok) return null
    const payload = (await response.json()) as Partial<SetupParamsResponse> | null
    if (!payload || typeof payload !== 'object' || typeof payload.defaults !== 'object') return null
    return { current: payload.current ?? null, defaults: payload.defaults ?? {} }
  } catch {
    return null
  }
}

/** Nová verze parametrů (append-only, povinný důvod); chyba jako text, ne výjimka. */
export async function saveSetupParams(
  params: Record<string, unknown>,
  note: string,
): Promise<{ ok: true; version: number } | { ok: false; error: string }> {
  try {
    const response = await fetch(`${API_BASE}/setups/params`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ params, note, created_by: 'ui' }),
    })
    if (!response.ok) {
      const detail = (await response.json().catch(() => ({}))) as { detail?: unknown }
      const error = typeof detail.detail === 'string' ? detail.detail : `HTTP ${response.status}`
      return { ok: false, error }
    }
    const stored = (await response.json()) as { version?: number }
    return { ok: true, version: stored.version ?? 0 }
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) }
  }
}
