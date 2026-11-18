/** Plán opce QQQ ze scénáře dne NQ (ADR-0062): track record pro Stats (F5b #1749).

Track record počítá čistá funkce enginu `compute/option_plan.track_record`, kterou API volá
při čtení `GET /option-plans/stats`. Frontend nic nesčítá ani nepřepočítává, jen zobrazuje bloky
odpovědi: hlavní plán a stíny K10 B zvlášť, po variantách obchody, vyřazené P3 a třídy
rozpočtu (Rozhodnuto 16. 11. v #1738). */
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
