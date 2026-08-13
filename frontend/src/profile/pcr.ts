/** PUT/CALL poměr v kontraktech, prémiích a notionalu (#469) — čisté funkce.

Prémie váží pozice penězi, které za ně někdo zaplatil: OTM křídla s tisíci
levných kontraktů nepřebijí ATM pozici za násobně víc. Mid = (bid+ask)/2
k zobrazené minutě — u volume je to aproximace (neváží cenu v okamžiku
obchodu, viz #486); `last` se nepoužívá, u nelikvidních striků je zmrzlý.

Zmrzlé kotace (ADR-0015, stale > práh) do prémií nevstupují — místo toho se
hlásí `missingShare` a panel při >30 % zašedne s vysvětlením. Kontrakty a
notional mid nepotřebují, tam se nevylučuje nic.
*/
import { STALE_THRESHOLD_S } from '../heatmap/color'
import type { ProfileRow } from './bars'

export type PcrBasis = 'vol_oi' | 'vol' | 'oi'
export type PcrUnit = 'contracts' | 'premium' | 'notional'

export const PCR_BASES: readonly PcrBasis[] = ['vol_oi', 'vol', 'oi']
export const PCR_UNITS: readonly PcrUnit[] = ['contracts', 'premium', 'notional']

export const PCR_BASIS_LABELS: Record<PcrBasis, string> = {
  vol_oi: 'Vol + OI',
  vol: 'Vol',
  oi: 'OI',
}
export const PCR_UNIT_LABELS: Record<PcrUnit, string> = {
  contracts: 'Kontrakty',
  premium: 'Prémie $',
  notional: 'Notional $',
}

/** Nad tímhle podílem vyloučených kontraktů je prémie zavádějící → zašedne. */
export const PCR_MISSING_LIMIT = 0.3

export interface PcrResult {
  put: number
  call: number
  /** put/call; null když call strana nemá nic (dělení nulou). */
  ratio: number | null
  /** Podíl kontraktů vyloučených kvůli chybějícímu/zmrzlému midu (jen prémie). */
  missingShare: number
}

function sideCount(row: ProfileRow, side: 'call' | 'put', basis: PcrBasis): number {
  const volume = side === 'call' ? row.callVolume : row.putVolume
  const oi = side === 'call' ? row.callOi : row.putOi
  if (basis === 'vol') return volume
  if (basis === 'oi') return oi
  return volume + oi
}

export function computePcr(
  rows: ProfileRow[],
  basis: PcrBasis,
  unit: PcrUnit,
  multiplier: number,
  spot: number | null,
  staleThresholdS: number = STALE_THRESHOLD_S,
): PcrResult {
  let put = 0
  let call = 0
  let included = 0
  let excluded = 0
  for (const row of rows) {
    const callCount = sideCount(row, 'call', basis)
    const putCount = sideCount(row, 'put', basis)
    if (unit === 'contracts') {
      call += callCount
      put += putCount
      continue
    }
    if (unit === 'notional') {
      if (spot !== null && Number.isFinite(spot)) {
        call += callCount * spot * multiplier
        put += putCount * spot * multiplier
      }
      continue
    }
    // Prémie: mid per strana; zmrzlý/chybějící mid stranu vylučuje z výpočtu
    const stale = (row.staleAge ?? 0) > staleThresholdS
    const callMid = !stale && (row.callMid ?? 0) > 0 ? (row.callMid ?? 0) : null
    const putMid = !stale && (row.putMid ?? 0) > 0 ? (row.putMid ?? 0) : null
    if (callMid !== null) {
      call += callCount * callMid * multiplier
      included += callCount
    } else {
      excluded += callCount
    }
    if (putMid !== null) {
      put += putCount * putMid * multiplier
      included += putCount
    } else {
      excluded += putCount
    }
  }
  const total = included + excluded
  return {
    put,
    call,
    ratio: call > 0 ? put / call : null,
    missingShare: unit === 'premium' && total > 0 ? excluded / total : 0,
  }
}

/** Kompaktní peníze: $61,3M / $1,2B — hrubá čísla, přesnost nese tooltip. */
export function formatMoney(value: number): string {
  const abs = Math.abs(value)
  if (abs >= 1e9) return `$${(value / 1e9).toFixed(1)}B`
  if (abs >= 1e6) return `$${(value / 1e6).toFixed(1)}M`
  if (abs >= 1e3) return `$${(value / 1e3).toFixed(1)}k`
  return `$${value.toFixed(0)}`
}

/** Top strikes okna dle premium podílu (#486) — rozklad do tooltipu.

Premium per strana striku = objem okna × mid × multiplikátor (táž aproximace
jako computePcr s unit 'premium'); share = podíl na součtu obou stran. Stale
strany se vylučují shodně s computePcr.
*/
export interface TopPremiumStrike {
  strike: number
  side: 'C' | 'P'
  premium: number
  share: number
}

export function topPremiumStrikes(
  rows: ProfileRow[],
  multiplier: number,
  limit = 5,
  staleThresholdS = STALE_THRESHOLD_S,
): TopPremiumStrike[] {
  const entries: Array<{ strike: number; side: 'C' | 'P'; premium: number }> = []
  let total = 0
  for (const row of rows) {
    const stale = (row.staleAge ?? 0) > staleThresholdS
    if (stale) continue
    const callMid = row.callMid ?? 0
    const putMid = row.putMid ?? 0
    if (callMid > 0 && row.callVolume > 0) {
      const premium = row.callVolume * callMid * multiplier
      entries.push({ strike: row.strike, side: 'C', premium })
      total += premium
    }
    if (putMid > 0 && row.putVolume > 0) {
      const premium = row.putVolume * putMid * multiplier
      entries.push({ strike: row.strike, side: 'P', premium })
      total += premium
    }
  }
  if (total <= 0) return []
  return entries
    .sort((a, b) => b.premium - a.premium)
    .slice(0, limit)
    .map((entry) => ({ ...entry, share: entry.premium / total }))
}
