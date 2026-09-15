/** Souhrnný strike profil přes všechny expirace (Σ přepínač v pravém panelu).

Agregát počítá API z poslední zapsané minuty každé expirace; hook ho obnovuje
každou minutu, dokud je Σ zapnuté. Selhání → null (panel drží per-expiraci data).
*/
import { useEffect, useState } from 'react'
import { API_BASE } from '../config'
import type { ProfileRow } from '../profile/bars'

interface AggregateRow {
  strike: number
  callVolComponent: number
  callOiComponent: number
  putVolComponent: number
  putOiComponent: number
  callVolume: number
  putVolume: number
  callOi: number
  putOi: number
}

const REFRESH_MS = 60_000

export function useAggregateProfile(
  symbol: string,
  date: string,
  enabled: boolean,
  spot: number | null,
): ProfileRow[] | null {
  // Řádky nesou klíč symbol|den, pro který platí: vypnutí Σ nebo jiný den
  // dává null odvozením při renderu, ne resetem stavu v efektu (#1123)
  const key = `${symbol}|${date}`
  const [loaded, setLoaded] = useState<{ key: string; rows: AggregateRow[] } | null>(null)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const load = () => {
      fetch(`${API_BASE}/profile/${symbol}/aggregate?date=${date}`)
        .then((response) => (response.ok ? response.json() : null))
        .then((payload: { rows: AggregateRow[] } | null) => {
          if (!cancelled && payload) setLoaded({ key, rows: payload.rows })
        })
        .catch(() => {
          // API nedostupné — panel zůstává na per-expiračních datech
        })
    }
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [symbol, date, key, enabled])

  if (!enabled || loaded === null || loaded.key !== key) return null
  return loaded.rows.map((row) => ({
    ...row,
    distanceFromSpot: spot !== null ? row.strike - spot : 0,
    callOiChange: null, // ΔOI je per expirace — v souhrnu se nezobrazuje
    putOiChange: null,
  }))
}
