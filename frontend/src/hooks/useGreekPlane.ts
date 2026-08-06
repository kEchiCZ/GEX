/** Charm/vanna plocha pro Dyn dropdown (#204) — načítá se až při volbě.

Gamma jede v /replay balíku a WS kanálech jako dřív; charm/vanna se stahuje
přes `/gexplane` a odebírá z WS jen když je zobrazená („only the displayed
greek is transferred" — LiveHub doručuje pouze odběratelům).
*/
import { useEffect, useState } from 'react'
import { API_BASE } from '../config'
import { canonicalTs } from '../replay/loader'
import type { GexFieldRow, GexProfileRow } from '../replay/loader'
import type { LiveSocket } from '../api/ws'

export type UnderlayPlane = 'off' | 'gex' | 'charm' | 'vanna'

export interface GreekPlaneData {
  profiles: GexProfileRow[]
  field: GexFieldRow | null
}

const EMPTY: GreekPlaneData = { profiles: [], field: null }

type RawRow = Record<string, unknown>

function parseProfile(row: RawRow): GexProfileRow | null {
  const values = Array.isArray(row.values) ? (row.values as unknown[]).map(Number) : []
  if (values.length === 0) return null
  return {
    tsIso: canonicalTs(row.ts_min),
    gridStart: Number(row.grid_start),
    gridStep: Number(row.grid_step),
    values,
  }
}

function parseField(row: RawRow | undefined): GexFieldRow | null {
  if (!row) return null
  const values = Array.isArray(row.values) ? (row.values as unknown[]).map(Number) : []
  const colCount = Number(row.col_count)
  if (values.length === 0 || !Number.isFinite(colCount) || colCount <= 0) return null
  if (values.length % colCount !== 0) return null
  return {
    tsIso: canonicalTs(row.ts_min),
    gridStart: Number(row.grid_start),
    gridStep: Number(row.grid_step),
    colStartIso: canonicalTs(row.col_start),
    colStepMin: Number(row.col_step_min),
    colCount,
    values,
  }
}

export function useGreekPlane(
  symbol: string,
  expiry: string | null,
  date: string,
  plane: UnderlayPlane,
  socket?: LiveSocket,
): GreekPlaneData {
  const [data, setData] = useState<GreekPlaneData>(EMPTY)
  const active = (plane === 'charm' || plane === 'vanna') && expiry !== null

  useEffect(() => {
    setData(EMPTY) // přepnutí plochy/instrumentu nesmí ukázat cizí data
    if (!active || !expiry) return
    let cancelled = false
    fetch(`${API_BASE}/gexplane/${symbol}/${expiry}?greek=${plane}&date=${date}`)
      .then((response) => (response.ok ? response.json() : null))
      .then((payload: { profiles?: RawRow[]; field?: RawRow[] } | null) => {
        if (cancelled || !payload) return
        const fetched = (payload.profiles ?? [])
          .map(parseProfile)
          .filter((row): row is GexProfileRow => row !== null)
        const fetchedField = parseField((payload.field ?? []).at(-1))
        // Fetch a WS subscribe běží souběžně — minuty došlé z WS mezi vyžádáním
        // a vyřízením fetche nesmí náhrada celého objektu zahodit (#504).
        // Merge podle tsIso; při duplicitě vyhrává WS řádek (dorazil později).
        setData((previous) => {
          const wsTs = new Set(previous.profiles.map((row) => row.tsIso))
          const onlyFetched = fetched.filter((row) => !wsTs.has(row.tsIso))
          const field =
            previous.field && (!fetchedField || previous.field.tsIso >= fetchedField.tsIso)
              ? previous.field
              : fetchedField
          return { profiles: [...onlyFetched, ...previous.profiles], field }
        })
      })
      .catch(() => {
        // API nedostupné — plocha zůstane prázdná, graf jede dál
      })
    return () => {
      cancelled = true
    }
  }, [active, symbol, expiry, date, plane])

  useEffect(() => {
    if (!active || !socket || !expiry) return
    const profileChannel = `${plane}profile.${symbol}.${expiry}`
    const fieldChannel = `${plane}field.${symbol}.${expiry}`
    const onProfile = (raw: Record<string, unknown>) => {
      const row = parseProfile(raw)
      if (!row) return
      setData((previous) => ({
        ...previous,
        profiles: [...previous.profiles.filter((item) => item.tsIso !== row.tsIso), row],
      }))
    }
    const onField = (raw: Record<string, unknown>) => {
      const row = parseField(raw)
      if (!row) return
      setData((previous) => ({ ...previous, field: row }))
    }
    socket.subscribe(profileChannel, onProfile)
    socket.subscribe(fieldChannel, onField)
    return () => {
      socket.unsubscribe(profileChannel, onProfile)
      socket.unsubscribe(fieldChannel, onField)
    }
  }, [active, socket, symbol, expiry, plane])

  return active ? data : EMPTY
}

/** Sparse zarovnání řádků plochy na minutovou osu dne (vzor loaderu). */
export function alignPlaneProfiles(
  rows: GexProfileRow[],
  minutesIso: string[],
): (GexProfileRow | null)[] {
  const index = new Map(minutesIso.map((iso, minuteIdx) => [iso, minuteIdx]))
  const aligned: (GexProfileRow | null)[] = Array.from({ length: minutesIso.length }, () => null)
  for (const row of rows) {
    const minuteIdx = index.get(row.tsIso)
    if (minuteIdx !== undefined) aligned[minuteIdx] = row
  }
  return aligned
}
