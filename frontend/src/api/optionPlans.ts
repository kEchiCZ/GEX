/** Plán opce QQQ ze scénáře dne NQ (ADR-0062): track record pro Stats (F5b #1749), seznam plánů,
PNG plánu a klíče účtu pro Briefing a Settings (F5a #1746).

Track record počítá čistá funkce enginu `compute/option_plan.track_record`, kterou API volá
při čtení `GET /option-plans/stats`. Frontend nic nesčítá ani nepřepočítává, jen zobrazuje bloky
odpovědi: hlavní plán a stíny K10 B zvlášť, po variantách obchody, vyřazené P3 a třídy
rozpočtu (Rozhodnuto 16. 11. v #1738).

Seznam `GET /option-plans` nese sloupce řádku, platný plán řádku (`effective`, ADR-0062 K4
bod 10), výsledek F3 (`result`) a odeslané přechody živého stavu (`live_alerts`, F4d #1841).
R:R, P3, P4, ceny opce i třídu rozpočtu počítá engine, karta je jen zobrazí. */
import { API_BASE } from '../config'

/** Scénář, ze kterého plán vzniká. Jiný symbol plán zatím nemá (výběr instrumentů #1790). */
export const OPTION_PLAN_SCENARIO_SYMBOL = 'NQ'

/** P&L jedné nohy (výstup v TP1, nebo držení do TP2) v $ na 1 kontrakt. */
export interface OptionPlanPnl {
  /** Nohy s naměřeným bidem výstupu; noha bez kotace výstupu se nepočítá */
  n: number
  sum_usd: number
  mean_usd: number | null
  wins: number
}

/** Skupina obchodů (nebo vyřazených P3) jedné varianty. */
export interface OptionPlanGroup {
  n: number
  preliminary: boolean
  /** Průběh po vstupu → počet; pořadí klíčů určuje engine */
  outcomes: Record<string, number>
  pnl_tp1: OptionPlanPnl
  pnl_hold: OptionPlanPnl
  /** Třídy rozpočtu P4 (v rozpočtu / nad rozpočtem / nenastaven); jen na první úrovni */
  by_budget?: Record<string, OptionPlanGroup>
}

export interface OptionPlanVariantStats {
  trades: OptionPlanGroup
  rejected_p3: OptionPlanGroup
  /** Spoušť bez kotace (ask nebo IV) = mezera mimo obchody i vyřazené P3 */
  no_quote: number
}

/** Track record jedné části plánu (hlavní plán nebo stín). */
export interface OptionPlanSection {
  /** Posouzené plány: spuštěno + bez vstupu */
  plans: number
  preliminary: boolean
  by_status: Record<string, number>
  variants: Record<string, OptionPlanVariantStats>
}

export interface OptionPlanStats {
  rules_version: number
  /** Začátek track recordu (`TRACK_RECORD_SINCE`); null = track record zatím nezačal */
  since: string | null
  /** Vyhodnocené plány mimo track record (před začátkem) */
  outside: number
  main: OptionPlanSection
  shadows: Record<string, OptionPlanSection>
}

export type OptionPlanStatsResult =
  { ok: true; stats: OptionPlanStats } | { ok: false; error: string }

/** Track record plánu; chyba je výslovná (HTTP kód, síť, tvar odpovědi), ne prázdná tabulka. */
export async function fetchOptionPlanStats(): Promise<OptionPlanStatsResult> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}/option-plans/stats`)
  } catch (err) {
    return { ok: false, error: `síť: ${err instanceof Error ? err.message : String(err)}` }
  }
  if (!response.ok) return { ok: false, error: `HTTP ${response.status}` }
  let payload: Partial<OptionPlanStats> | null
  try {
    payload = (await response.json()) as Partial<OptionPlanStats> | null
  } catch {
    return { ok: false, error: 'odpověď není JSON' }
  }
  if (
    payload === null ||
    typeof payload !== 'object' ||
    typeof payload.outside !== 'number' ||
    typeof payload.rules_version !== 'number' ||
    payload.main?.variants === undefined ||
    payload.shadows === undefined
  ) {
    return { ok: false, error: 'neočekávaný tvar odpovědi' }
  }
  return { ok: true, stats: payload as OptionPlanStats }
}

/** Popisky stínů K10 B; neznámý klíč se ukáže tak, jak ho pošle engine. */
export const SHADOW_LABELS: Record<string, string> = {
  entry_b: 'vstup B',
  wick_test: 'test knotem',
}

/** Tooltipy průběhů po vstupu (`compute/option_plan.OUTCOMES`). */
export const OUTCOME_TOOLTIPS: Record<string, string> = {
  SL: 'SL před TP1:\n• 5min close za stopem',
  TP1: 'TP1 u scénáře s jedním cílem:\n• dotyk cíle v 1m baru, konec',
  TP2: 'TP1 a pak TP2:\n• po TP1 stop na vstupu, TP2 dotykem',
  'TP1→vstup': 'Po TP1 stop na vstupu:\n• 5min close zpět za cenou vstupu',
  'TP1→exit': 'Po TP1 bez TP2 a bez stopu:\n• konec v exitu (15 min před close)',
  exit: 'Bez SL i bez cíle:\n• konec v exitu (15 min před close)',
  'cíl 1 za vstupem': 'Cíl 1 leží už za cenou vstupu:\n• není kam jít, bez průběhu',
}

export const ROW_TOOLTIPS = {
  trades:
    'Obchod = spoušť vstupu A, která prošla P3 (R:R z kotace ≥ 2):\n• vstup za ask v minutě spouště\n• výstup za bid v minutě události\n• $ na 1 kontrakt',
  rejected:
    'Vyřazeno P3 = spoušť s R:R z kotace pod 2:\n• neobchoduje se, výsledek je hypotetický\n• do obchodů se nepočítá\n• měří, jestli filtr P3 pomáhá',
  budget:
    'Třída podle rozpočtu na obchod v Settings v době vyhodnocení (P4):\n• čerpání P4 proti rozpočtu\n• hodnota rozpočtu se neukládá',
  noQuote:
    'Bez kotace = v minutě spouště chyběl ask nebo IV:\n• mezera, ne odhad\n• mimo obchody i vyřazené P3',
  gap: 'Mezera = chybí 1m bar QQQ mezi US openem a rozhodnutím:\n• plán nešlo posoudit\n• do posouzených ani do obchodů nejde',
  noOption:
    'Bez opce = plán bez variant opce:\n• není co ocenit\n• do posouzených ani do obchodů nejde',
  shadow:
    'Stín (ADR-0062 K10 B) = táž pravidla s jinou spouští:\n• do track recordu hlavního plánu se nepočítá\n• živá upozornění nemá',
  pnlTp1:
    'Výstup celé pozice v TP1 (bez TP1 v SL, nebo v exitu):\n• Σ součet, ⌀ průměr na kontrakt\n• zisk k/n = nohy se ziskem z nohou s naměřeným bidem výstupu',
  pnlHold:
    'Držení do TP2, po TP1 stop na vstupu:\n• Σ součet, ⌀ průměr na kontrakt\n• zisk k/n = nohy se ziskem z nohou s naměřeným bidem výstupu',
} as const

/** Klíče účtu plánu v `/settings` (ADR-0062 K2 A, `compute/option_plan.ACCOUNT_KEYS`). */
export const OPTION_PLAN_ACCOUNT_KEY = 'option_plan_account_usd'
export const OPTION_PLAN_MAX_LOSS_KEY = 'option_plan_max_loss_usd'

/** Nastavená hodnota účtu: kladné konečné číslo (API ukládá číslo, nebo `null` = nenastaveno). */
export function accountValueSet(value: unknown): boolean {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}

/** Text pole v Settings → hodnota pro server: `null` = nenastaveno, `undefined` = neplatné.
 *  Platí totéž co `account_value` na serveru (kladné konečné číslo), čárka jako desetinná tečka. */
export function parseAccountInput(text: string): number | null | undefined {
  const trimmed = text.trim().replace(',', '.')
  if (trimmed === '') return null
  const value = Number(trimmed)
  return Number.isFinite(value) && value > 0 ? value : undefined
}

/** Kotace varianty v jednom okamžiku (9:15 ve variantě, 9:31 v `prices`). */
export interface OptionPlanQuote {
  role: string
  right: 'C' | 'P'
  strike: number
  expiry: string
  bid: number
  ask: number
  iv: number
  delta: number
  quote_ts: string
  /** Model ceny opce v TP1/TP2 s IV kotace („model z naměřené IV“) */
  at_targets: number[]
  source: string
}

/** P3 a P4 při spoušti ($ na 1 kontrakt); `paid` = model (plán), nebo ask kotace (F3). */
export interface OptionPlanPricing {
  paid: number
  at_stop: number
  at_tp1: number
  at_tp2: number | null
  rr: number | null
  rr_strict: number | null
  loss_at_stop_usd: number
  drawdown_usd: number
  pnl_tp1_usd: number
  pnl_tp2_usd: number | null
}

export interface OptionPlanVariant extends OptionPlanQuote {
  /** null do spouště (K12 A) */
  pricing: OptionPlanPricing | null
  /** `čeká` / `obchodovat` / `neobchodovat` */
  verdict: string
  reasons: string[]
}

/** Spoušť vstupu: 5min close, SL na podkladu. */
export interface OptionPlanEntry {
  kind: 'A' | 'B'
  ts: string
  price: number
  stop: number
  level: string
  level_price: number
}

/** Plán jedné části (hlavní plán nebo stín) ve tvaru `compute/option_plan.plan_to_json`. */
export interface OptionPlanMain {
  rules: string
  direction: 'long' | 'short'
  right: 'C' | 'P'
  created_at: string
  targets: number[]
  target_ts: string[]
  exit_ts: string
  /** Úroveň vstupu A: [klíč `LEVEL_LABELS`, cena QQQ]; null = žádná do 1 EM */
  level_a: [string, number] | null
  entry: OptionPlanEntry | null
  variants: OptionPlanVariant[]
  missing: string[]
  p5: string
  model_label: string
  verdict: string
}

/** Platný plán řádku (`option_plans_store.effective_plan`). */
export interface OptionPlanEffective {
  slot: string
  /** null = bez opce */
  main: OptionPlanMain | null
  /** Ceny z 9:31 u plánu úplného už v 9:15 (`main`, `entry_b`, `wick_test`), jinak null */
  prices: Record<string, OptionPlanQuote[]> | null
  reasons: string[]
  /** null = přecenění v 9:31 ještě neproběhlo */
  repricing_reasons: string[] | null
}

export interface OptionPlanEvent {
  kind: string
  ts: string
  price: number
}

export interface OptionPlanExit {
  event: string
  ts: string
  bid: number | null
  quote_age_s: number | null
  /** null = kotace v minutě výstupu chybí (mezera) */
  pnl_usd: number | null
}

/** Varianta po spoušti z vyhodnocení F3: P3 a P4 z kotace, P&L z naměřených kotací. */
export interface OptionPlanVariantResult {
  role: string
  right: 'C' | 'P'
  strike: number
  expiry: string
  /** `obchod`, `vyřazeno P3` (hypotetický výsledek), `bez kotace` (mezera) */
  kind: string
  entry_quote: { ts_min: string; bid: number | null; ask: number | null } | null
  pricing: OptionPlanPricing | null
  /** `v rozpočtu` / `nad rozpočtem` / `nenastaven`; třída ze Settings při vyhodnocení */
  budget: string | null
  exit_tp1: OptionPlanExit | null
  exit_hold: OptionPlanExit | null
  gaps: string[]
}

export interface OptionPlanPartResult {
  rules: string
  /** `spuštěno` / `bez vstupu` / `bez opce` / `mezera` */
  status: string
  reasons: string[]
  entry: OptionPlanEntry | null
  outcome: string | null
  events: OptionPlanEvent[]
  variants: OptionPlanVariantResult[]
}

export interface OptionPlanResult {
  rules_version: number
  main: OptionPlanPartResult
  shadows: Record<string, OptionPlanPartResult>
}

/** Odeslaný přechod živého stavu (F3b #1757): jen událost a čas, ceny nese text zprávy. */
export interface OptionPlanLiveAlert {
  event: string
  ts: string
  sent_at: string
  final: boolean
}

export interface OptionPlanRow {
  id: number
  scenario_id: number
  day: string
  created_at: string
  rules_version: number
  /** `sestaven` / `bez opce` */
  status: string
  /** Hlavní slovo verdiktu hlavního plánu; null u plánu bez opce */
  verdict: string | null
  evaluated_at: string | null
  result: OptionPlanResult | null
  has_image: boolean
  /** Chybí u API před F4d #1841 */
  live_alerts?: OptionPlanLiveAlert[]
  effective: OptionPlanEffective
}

export type OptionPlansResult = { ok: true; plans: OptionPlanRow[] } | { ok: false; error: string }

/** Nejnovější plány; chyba je výslovná (HTTP kód, síť, tvar odpovědi), ne prázdný seznam. */
export async function fetchOptionPlans(limit = 10): Promise<OptionPlansResult> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}/option-plans?limit=${limit}`)
  } catch (err) {
    return { ok: false, error: `síť: ${err instanceof Error ? err.message : String(err)}` }
  }
  if (!response.ok) return { ok: false, error: `HTTP ${response.status}` }
  let payload: { option_plans?: unknown } | null
  try {
    payload = (await response.json()) as { option_plans?: unknown } | null
  } catch {
    return { ok: false, error: 'odpověď není JSON' }
  }
  if (payload === null || typeof payload !== 'object' || !Array.isArray(payload.option_plans)) {
    return { ok: false, error: 'neočekávaný tvar odpovědi' }
  }
  return { ok: true, plans: payload.option_plans as OptionPlanRow[] }
}

export function optionPlanImageUrl(id: number): string {
  return `${API_BASE}/option-plans/${id}/image`
}

export type OptionPlanImageResult = { ok: true; blob: Blob } | { ok: false; error: string }

/** PNG plánu (snímek stavu plánu, ADR-0062 K4 bod 12). Kreslí se při prvním dotazu, proto se
 *  nepodmiňuje `has_image`. `no-cache`: adresa je stejná pro všechny stavy plánu, prohlížeč se
 *  musí zeptat serveru. Selhání kreslení = důvod z odpovědi (500 s `detail`), ne prázdný obrázek. */
export async function fetchOptionPlanImage(id: number): Promise<OptionPlanImageResult> {
  let response: Response
  try {
    response = await fetch(optionPlanImageUrl(id), { cache: 'no-cache' })
  } catch (err) {
    return { ok: false, error: `síť: ${err instanceof Error ? err.message : String(err)}` }
  }
  if (!response.ok) {
    let detail = ''
    try {
      const body = (await response.json()) as { detail?: unknown } | null
      if (typeof body?.detail === 'string') detail = `: ${body.detail}`
    } catch {
      // Tělo bez JSON: stačí kód
    }
    return { ok: false, error: `HTTP ${response.status}${detail}` }
  }
  return { ok: true, blob: await response.blob() }
}

/** Popisky úrovní plánu (`compute/option_plan.LEVEL_LABELS`). */
export const LEVEL_LABELS: Record<string, string> = {
  flip: 'flip',
  call_wall: 'call wall',
  put_wall: 'put wall',
  centroid: 'těžiště',
  on_high: 'ONH',
  on_low: 'ONL',
  prev_high: 'PDH',
  prev_low: 'PDL',
  prev_close: 'PDC',
}

/** Popisky přechodů živého stavu (`compute/option_plan.live_transitions`). */
export const LIVE_EVENT_LABELS: Record<string, string> = {
  vstup: 'vstup A',
  TP1: 'TP1',
  TP2: 'TP2',
  SL: 'SL',
  'stop na vstupu': 'stop na vstupu',
  exit: 'exit',
  'bez vstupu': 'bez vstupu do exitu',
  mezera: 'mezera v barech',
}
