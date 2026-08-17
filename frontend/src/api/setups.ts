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

/** Aktuální verze mechaniky — zrcadlo `SETUP_MECHANICS_VERSION` v enginu (#311).
Statistiky se defaultně počítají jen z ní, aby se nemíchaly výsledky staré
(absolutní buffery, RRR 25–47) a nové R-mechaniky. */
export const CURRENT_MECHANICS_VERSION = 2

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
export function formatPnlUsd(value: number): string {
  const rounded = Math.round(value * 100) / 100
  return `${rounded > 0 ? '+' : ''}${rounded} $`
}

/** Startovní kapitál účtu na jeden ticker (#191, zadání uživatele) — báze procent. */
export const ACCOUNT_START_USD = 5000

/** P/L setupu v % startovního účtu (#191): pnl $ / 5 000 $ na ticker.

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
