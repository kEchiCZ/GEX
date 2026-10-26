/** Kalibrovaná FA α / ρ per symbol (#232 fáze 2, ADR-0061) — zdroj pro badge ve stavové liště.

Faktor se mění nejvýš jednou denně (ranní kalibrace po OI archivu), takže stačí
načtení při startu a obnova à 15 minut. Bez dat (API neběží) vrací null
a badge se neukáže. Co badge ukáže, rozhoduje `faAlphaBadge` proti zdroji
CumΔ běžícího enginu (ADR-0057 bod 6, #1632, #1666).
*/
import { useEffect, useState } from 'react'
import { API_BASE } from '../config'
import { CUMDELTA_SOURCE_AGGRESSOR } from '../replay/loader'

/** Zdroj kalibrace pod v6 (engine `fa_calibration.SOURCE_VOLUME`, ADR-0061 bod 4):
ρ = podíl objemu strany za seanci, který se propíše do OI. */
export const FA_SOURCE_VOLUME = 'volume'

/** Zdroj kalibrace: α z netu midpoint testu (ADR-0057 bod 8; NULL ze starších
stavů = midpoint), α z netu tisků (`aggressor`) nebo ρ z objemu (`volume`, ADR-0061). */
export type FaAlphaSource = 'midpoint' | 'aggressor' | 'volume'

export interface FaAlphaState {
  alpha: number
  days: number
  source: FaAlphaSource
}

/** Odpověď `/fa/alpha`: stavy per symbol a práh dnů, od kterého FA pod tisky jede
(`FA_MIN_DAYS` enginu; null = API ho nenese, pak se „nekalibrováno“ neukáže). */
export interface FaAlphaPayload {
  bySymbol: Record<string, FaAlphaState>
  minDays: number | null
}

/** Co ukáže badge: kalibrovaná α, nebo FA vypnutá do kalibrace z tisků. */
export type FaAlphaBadge =
  | { kind: 'calibrated'; alpha: number; days: number; source: FaAlphaSource }
  | { kind: 'uncalibrated'; days: number; minDays: number }

const REFRESH_MS = 15 * 60_000

interface AlphaRow {
  symbol: string
  alpha: number
  days: number
  source?: string | null
}

function parseSource(source: string | null | undefined): FaAlphaSource {
  if (source === FA_SOURCE_VOLUME) return 'volume'
  return source === CUMDELTA_SOURCE_AGGRESSOR ? 'aggressor' : 'midpoint'
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
                source: parseSource(row.source),
              }
            }
          }
          const minDays = Number(body.min_days)
          setPayload({
            bySymbol,
            minDays: Number.isFinite(minDays) && minDays > 0 ? minDays : null,
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

/** Badge FA pro symbol podle zdroje CumΔ běžícího enginu (zrcadlí `live_alpha`).

- Pod midpointem (mechanika v5; zdroj neznámý = starší engine = midpoint) FA
  jede s uloženým faktorem → „FA α=…“ (u stavu `volume` po návratu z v6 „FA ρ=…“);
  bez stavu běží default z konfigurace a badge se nekreslí (beze změny proti
  stavu před #1632).
- Pod tisky (v6, ADR-0061 bod 4) jede FA jen se stavem ρ z objemu (`volume`)
  s aspoň `minDays` dny, jinak je vypnutá → „nekalibrováno n/5“; α z netu
  (midpoint i tisky) je jiná veličina a počítá se jako 0 dnů. Bez stavu „0/5“,
  ne default α — ale jen u symbolu, který CumΔ tracker má
  (`/status.cumdelta_coverage`): ad-hoc pohled FA nemá vůbec.
*/
export function faAlphaBadge(
  payload: FaAlphaPayload | null,
  symbol: string,
  runningSource: string | undefined,
  trackedSymbols: readonly string[],
): FaAlphaBadge | null {
  if (!payload) return null
  const state = payload.bySymbol[symbol]
  if (runningSource !== CUMDELTA_SOURCE_AGGRESSOR) {
    return state ? { kind: 'calibrated', ...state } : null
  }
  const { minDays } = payload
  if (minDays === null) return null // starší API bez prahu — nehádat
  if (state && state.source === 'volume' && state.days >= minDays) {
    return { kind: 'calibrated', ...state }
  }
  if (!state && !trackedSymbols.includes(symbol)) return null
  return {
    kind: 'uncalibrated',
    days: state?.source === 'volume' ? state.days : 0,
    minDays,
  }
}

/** České skloňování dnů pro badge: 1 den, 2–4 dny, jinak dní. */
export function daysLabel(days: number): string {
  if (days === 1) return '1 den'
  if (days >= 2 && days <= 4) return `${days} dny`
  return `${days} dní`
}
