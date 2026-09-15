/** Setup detektor (ADR-0004): REST klient a české popisky šablon. */
import { API_BASE } from '../config'

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
}

/** Zrcadlo `SETUP_MECHANICS_VERSION` v enginu (#311) — UŽ JEN pro testy.

Filtrování statistik používá `currentMechanicsVersion()` (setups/performance,
ADR-0030): tahle konstanta zastarala na 2, zatímco engine byl na 4, a
statistiky Setupů týden neviděly aktuální setupy. */
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

/** Formát P/L se znaménkem („+512.50 $" / „−250 $"). */
/** Expected Value na obchod (#911): (WinRate × AvgWin) − (LossRate × AvgLoss).

Matematicky ≡ prostý průměr výsledků (v R je to přesně Ø R) — hodnota EV
dlaždice je v USD vyjádření a ve viditelném ROZKLADU: trader vidí, jestli
EV táhne win rate, velikost výher, nebo ho zabíjí velikost proher.
EV > 0 = dlouhodobě vydělává, EV < 0 = dlouhodobě ztrácí. */
export interface EvStats {
  ev: number
  winRate: number
  lossRate: number
  avgWin: number
  /** Průměrná ztráta jako KLADNÉ číslo (vzorec ji odečítá). */
  avgLoss: number
  n: number
}

export function evStats(pnls: number[]): EvStats | null {
  if (pnls.length === 0) return null
  const winsList = pnls.filter((value) => value > 0)
  const lossList = pnls.filter((value) => value <= 0)
  const winRate = winsList.length / pnls.length
  const lossRate = lossList.length / pnls.length
  const avgWin =
    winsList.length > 0 ? winsList.reduce((sum, value) => sum + value, 0) / winsList.length : 0
  const avgLoss =
    lossList.length > 0
      ? Math.abs(lossList.reduce((sum, value) => sum + value, 0) / lossList.length)
      : 0
  return {
    ev: winRate * avgWin - lossRate * avgLoss,
    winRate,
    lossRate,
    avgWin,
    avgLoss,
    n: pnls.length,
  }
}

/** Tooltip EV — odřádkovaný (konvence 27. 8.); rozklad + čtení znaménka. */
export function evTooltip(stats: EvStats, unit: string): string {
  const pct = (value: number) => `${Math.round(100 * value)} %`
  return [
    'Expected Value = průměrný očekávaný výsledek NA OBCHOD:',
    `(WinRate × AvgWin) − (LossRate × AvgLoss)`,
    `= ${pct(stats.winRate)} × ${stats.avgWin.toFixed(0)} ${unit} − ${pct(stats.lossRate)} × ${stats.avgLoss.toFixed(0)} ${unit}`,
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

/** Riziko jednoho setupu v USD na 1 kontrakt: |entry − stop| × hodnota bodu.

Na rozdíl od P/L je známé už při vzniku setupu — proto se počítá i pro aktivní
pozice, kde `outcome_r` ještě není. */
export function setupRiskUsd(row: Pick<SetupRow, 'entry' | 'stop'>, pointValueUsd: number): number {
  return Math.abs(row.entry - row.stop) * pointValueUsd
}

/** Souhrn jednoho obchodního dne (#748). */
export interface DailyStats {
  /** Uzavřené dnes + aktivní vzniklé dnes. */
  trades: number
  closed: number
  active: number
  wins: number
  losses: number
  /** Úspěšnost z uzavřených; `null` když se dnes nic neuzavřelo. */
  winRate: number | null
  bestUsd: number | null
  worstUsd: number | null
  pnlUsd: number
  pnlPct: number
  /** Největší riziko v JEDNOM dnešním obchodě (% účtu). */
  maxRiskPct: number
  /** Součet rizik všech dnešních obchodů (% účtu) — celkové nasazení dne. */
  totalRiskPct: number
}

/** Statistika dne ze setupů (#748).

**Den je obchodní seance, ne kalendářní datum** — `sessionDateIso` mapuje čas na
seanci [17:00 CT D−1, 17:00 CT D), takže noční Globex obchod spadne do správného
dne (#512). Bez toho by se večerní obchody počítaly k předchozímu dni.

**Který čas rozhoduje**: uzavřený setup patří do dne, kdy se uzavřel (`closed_ts`)
— bilance dne je to, co se dnes zrealizovalo. Aktivní patří do dne vzniku
(`created_ts`), protože jiný čas nemají a riziko už nesou.

Riziko se počítá i pro aktivní pozice: „kolik dnes bylo v sázce" je otázka
o vstupu, ne o výsledku. */
export function dailyStats(
  rows: SetupRow[],
  pointValueUsd: number,
  sessionDate: string,
  toSessionDate: (ts: number) => string,
): DailyStats {
  const today = rows.filter((row) => {
    const stamp = row.status === 'active' ? row.created_ts : (row.closed_ts ?? row.created_ts)
    return toSessionDate(new Date(stamp).getTime()) === sessionDate
  })
  const closed = today.filter((row) => row.status !== 'active' && row.outcome_r !== null)
  const pnls = closed.map((row) => setupPnlUsd(row, pointValueUsd) ?? 0)
  const wins = pnls.filter((value) => value > 0).length
  const risks = today.map((row) => setupRiskUsd(row, pointValueUsd))
  const pnlUsd = pnls.reduce((sum, value) => sum + value, 0)
  return {
    trades: today.length,
    closed: closed.length,
    active: today.length - closed.length,
    wins,
    losses: closed.length - wins,
    // Bez uzavřeného obchodu úspěšnost neexistuje — nula by lhala, že se
    // nedařilo, přitom se jen ještě nic nedokončilo
    winRate: closed.length > 0 ? (wins / closed.length) * 100 : null,
    bestUsd: pnls.length > 0 ? Math.max(...pnls) : null,
    worstUsd: pnls.length > 0 ? Math.min(...pnls) : null,
    pnlUsd,
    pnlPct: (pnlUsd / ACCOUNT_START_USD) * 100,
    maxRiskPct: risks.length > 0 ? (Math.max(...risks) / ACCOUNT_START_USD) * 100 : 0,
    totalRiskPct: (risks.reduce((sum, value) => sum + value, 0) / ACCOUNT_START_USD) * 100,
  }
}

export async function fetchSetups(symbol: string): Promise<SetupRow[]> {
  const response = await fetch(`${API_BASE}/setups/${symbol}`)
  if (!response.ok) return []
  const payload = (await response.json()) as { setups?: SetupRow[] }
  return payload.setups ?? []
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

export interface GateBucket {
  n: number
  avgR: number
  winRate: number
}

export interface BandGateStats {
  simple: { pass: GateBucket; block: GateBucket }
  regime: { pass: GateBucket; block: GateBucket }
}

function gateBucket(rows: SetupRow[]): GateBucket {
  const results = rows.map((row) => row.outcome_r ?? 0)
  const wins = results.filter((value) => value > 0).length
  const sum = results.reduce((total, value) => total + value, 0)
  return {
    n: rows.length,
    avgR: rows.length > 0 ? sum / rows.length : 0,
    winRate: rows.length > 0 ? wins / rows.length : 0,
  }
}

/** Rozpad uzavřených setupů podle verdiktu obou stínových pravidel.

Jen uzavřené řádky s bránou v `context`; verdikt `unknown` nevstupuje do
žádné skupiny (režim nebyl znám, pravidlo nemělo co říct). null = žádný
uzavřený setup bránu nenese — blok se nekreslí. */
export function bandGateStats(rows: SetupRow[]): BandGateStats | null {
  const closed = rows
    .filter((row) => row.status !== 'active' && row.outcome_r !== null)
    .map((row) => ({ row, info: bandInfo(row) }))
    .filter((item): item is { row: SetupRow; info: BandInfo } => item.info !== null)
  if (closed.length === 0) return null
  const pick = (rule: 'gateSimple' | 'gateRegime', verdict: BandGate) =>
    gateBucket(closed.filter((item) => item.info[rule] === verdict).map((item) => item.row))
  return {
    simple: { pass: pick('gateSimple', 'pass'), block: pick('gateSimple', 'block') },
    regime: { pass: pick('gateRegime', 'pass'), block: pick('gateRegime', 'block') },
  }
}

/** Text dlaždice skupiny: „n · Ø R" (bez vzorku pomlčka). */
export function formatGateBucket(bucket: GateBucket): string {
  if (bucket.n === 0) return '—'
  return `${bucket.n} · ${bucket.avgR >= 0 ? '+' : ''}${bucket.avgR.toFixed(2)} R`
}

// ── Risk framework malého účtu (#1185, varianta A) ─────────────────────────
//
// Engine u každého setupu spočítá sizing (kontrakty = ⌊účet × riziko % /
// (stop b × hodnota bodu)⌋), brzdy (−3 R den, −6 R týden, 2 stopy šablony za
// den) a bránu šablon (dolní mez očekávání > 0 při n ≥ 30 za 60 seancí).
// Setup vzniká vždy; `tradeable` říká, zda se dá zobchodovat, `trade_block`
// proč ne. UI jen zobrazuje — nic nepřepočítává.

export type TradeBlock =
  'stop_over_budget' | 'stop_over_cap' | 'daily_brake' | 'weekly_brake' | 'template_stops' | 'gate'

export const TRADE_BLOCK_LABELS: Record<TradeBlock, string> = {
  stop_over_budget: 'stop nad rozpočtem rizika',
  stop_over_cap: 'stop nad tvrdým stropem',
  daily_brake: 'denní brzda',
  weekly_brake: 'týdenní brzda',
  template_stops: 'strop stopů šablony',
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
}

/** Risk kontext setupu (#1185); null = řádek vznikl před pravidly — nic se nevymýšlí. */
export function riskInfo(row: Pick<SetupRow, 'context'>): RiskInfo | null {
  const context = row.context ?? {}
  if (typeof context.tradeable !== 'boolean' || typeof context.contracts !== 'number') return null
  const num = (key: string): number | null =>
    typeof context[key] === 'number' ? (context[key] as number) : null
  const block = context.trade_block
  const gate = context.template_gate
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
  }
}

function blockText(info: RiskInfo): string {
  return info.block === null ? 'neobchodovatelný' : TRADE_BLOCK_LABELS[info.block]
}

/** Štítek do tabulky: „1 ks · 500 $" nebo „stín: denní brzda". */
export function riskLabel(info: RiskInfo): string {
  if (info.tradeable) return `${info.contracts} ks · ${Math.round(info.maxLossUsd)} $`
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
  return [
    info.tradeable
      ? `Obchodovatelný: ${info.contracts} kontrakt(y), ztráta na stopu ${Math.round(info.maxLossUsd)} $ (+ poplatky ${Math.round(info.feeUsd)} $).`
      : `Stínový setup — neobchodovat: ${blockText(info)}.`,
    `Rozpočet rizika ${Math.round(info.riskBudgetUsd)} $ z účtu ${Math.round(info.accountUsd)} $ (aplikace = plné kontrakty; 1 kontrakt zde = 1 MES/MNQ reálně, dolary ÷ 10).`,
    `Stop ${info.stopPoints.toFixed(2)} b.`,
    '',
    'Pravidla (#1185):',
    `• brána šablony: ${gate}`,
    `• brzdy: dnes ${day}, týden ${week} (−3 R den / −6 R týden zastaví nové obchody do settle)`,
    '• stínové setupy se dál měří, jen se neobchodují a nechodí do pushe',
  ].join('\n')
}

export interface AccountStats {
  n: number
  shadow: number
  pnlUsd: number
  feesUsd: number
  maxDrawdownUsd: number
}

/** Bilance účtu z obchodovatelných uzavřených setupů (chronologicky podle uzavření).
null = žádný řádek nenese risk kontext (před #1185) — blok se nekreslí. */
export function accountStats(rows: SetupRow[]): AccountStats | null {
  const withRisk = rows
    .map((row) => ({ row, info: riskInfo(row) }))
    .filter((item): item is { row: SetupRow; info: RiskInfo } => item.info !== null)
  if (withRisk.length === 0) return null
  const closed = withRisk
    .filter((item) => item.info.tradeable && item.row.outcome_r !== null && item.row.closed_ts)
    .sort((a, b) => Date.parse(a.row.closed_ts ?? '') - Date.parse(b.row.closed_ts ?? ''))
  let equity = 0
  let peak = 0
  let worst = 0
  let fees = 0
  for (const item of closed) {
    fees += item.info.feeUsd
    equity += accountPnlUsd(item.row) ?? 0
    if (equity > peak) peak = equity
    if (equity - peak < worst) worst = equity - peak
  }
  return {
    n: closed.length,
    shadow: withRisk.filter((item) => !item.info.tradeable).length,
    pnlUsd: equity,
    feesUsd: fees,
    maxDrawdownUsd: worst,
  }
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
