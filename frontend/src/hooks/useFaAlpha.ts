/** Kalibrovaná FA α per symbol (#232 fáze 2) — zdroj pro badge ve stavové liště.

α se mění nejvýš jednou denně (ranní kalibrace po OI archivu), takže stačí
načtení při startu a obnova à 15 minut. Bez dat (API neběží) vrací null
a badge se neukáže. Co badge ukáže, rozhoduje `faAlphaBadge` proti zdroji
CumΔ běžícího enginu (ADR-0057 bod 6, #1632).
*/
import { useEffect, useState } from 'react'
import { API_BASE } from '../config'

/** Zdroj netu kalibrace α (ADR-0057 bod 8); NULL ze starších stavů = midpoint. */
export type FaAlphaSource = 'midpoint' | 'aggressor'

export interface FaAlphaState {
  alpha: number
  days: number
  source: FaAlphaSource
}

/** Odpověď `/fa/alpha`: stavy per symbol a práh dnů, od kterého FA pod tisky jede. */
export interface FaAlphaPayload {
  bySymbol: Record<string, FaAlphaState>
  minDays: number
}

/** Co ukáže badge: kalibrovaná α, nebo FA vypnutá do kalibrace z tisků. */
export type FaAlphaBadge =
  | { kind: 'calibrated'; alpha: number; days: number; source: FaAlphaSource }
  | { kind: 'uncalibrated'; days: number; minDays: number }

const REFRESH_MS = 15 * 60_000
/** Výchozí práh dnů (`FA_MIN_DAYS` enginu), když starší API `min_days` nenese. */
const DEFAULT_MIN_DAYS = 5

interface AlphaRow {
  symbol: string
  alpha: number
  days: number
  source?: string | null
}

export function useFaAlpha(): FaAlphaPayload | null {
  const [payload, setPayload] = useState<FaAlphaPayload | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      fetch(`${API_BASE}/fa/alpha`)
        .then((response) => (response.ok ? response.json() : null))
        .then((body: { alphas?: AlphaRow[]; min_days?: number } | null) => {
          if (cancelled || !body?.alphas) return
          const bySymbol: Record<string, FaAlphaState> = {}
          for (const row of body.alphas) {
            if (typeof row.symbol === 'string' && Number.isFinite(row.alpha)) {
              bySymbol[row.symbol] = {
                alpha: row.alpha,
                days: Number(row.days) || 0,
                source: row.source === 'aggressor' ? 'aggressor' : 'midpoint',
              }
            }
          }
          const minDays = Number(body.min_days)
          setPayload({
            bySymbol,
            minDays: Number.isFinite(minDays) && minDays > 0 ? minDays : DEFAULT_MIN_DAYS,
          })
        })
        .catch(() => {
          // API neběží — badge se prostě neukáže
        })
    }
    load()
    const id = setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  return payload
}

/** Badge FA α pro symbol podle zdroje CumΔ běžícího enginu (zrcadlí `live_alpha`).

- Pod midpointem (mechanika v5; zdroj neznámý = starší engine = midpoint) FA
  jede s uloženou α → „FA α=…“; bez stavu běží default z konfigurace a badge
  se nekreslí (beze změny proti stavu před #1632).
- Pod tisky (v6, ADR-0057 bod 6) jede FA jen se stavem z tisků s aspoň
  `minDays` dny, jinak je vypnutá → „nekalibrováno n/5“; stav z midpointu je
  jiná veličina a počítá se jako 0 dnů. Bez stavu „0/5“, ne default α — ale
  jen u symbolu, který CumΔ tracker má (`/status.cumdelta_coverage`): ad-hoc
  pohled FA nemá vůbec.
*/
export function faAlphaBadge(
  payload: FaAlphaPayload | null,
  symbol: string,
  runningSource: string | undefined,
  trackedSymbols: readonly string[],
): FaAlphaBadge | null {
  if (!payload) return null
  const state = payload.bySymbol[symbol]
  if (runningSource !== 'aggressor') {
    return state ? { kind: 'calibrated', ...state } : null
  }
  if (state && state.source === 'aggressor' && state.days >= payload.minDays) {
    return { kind: 'calibrated', ...state }
  }
  if (!state && !trackedSymbols.includes(symbol)) return null
  return {
    kind: 'uncalibrated',
    days: state?.source === 'aggressor' ? state.days : 0,
    minDays: payload.minDays,
  }
}

/** České skloňování dnů pro badge: 1 den, 2–4 dny, jinak dní. */
export function daysLabel(days: number): string {
  if (days === 1) return '1 den'
  if (days >= 2 && days <= 4) return `${days} dny`
  return `${days} dní`
}
