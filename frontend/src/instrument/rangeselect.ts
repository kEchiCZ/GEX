/** Range selector (#484) — čisté funkce okna [t1, t2] nad denními daty.

Okenní profil se počítá V KLIENTOVI z už načteného /replay bundlu (posudek
#484): volume ve snapshotech je kumulativní denní (SPEC 4.3), takže okno je
rozdíl řádků dvou minut — identická aritmetika jako API /profile?from=&to=
(#483), jen bez roundtripu. OI zůstává statické k t2 (otevřené pozice nejsou
tok), |Δ| k t2 se zpětně odvodí z komponenty (component = vol × |Δ|).

Rozsah se drží jako pár ISO minut (1m osa) — přežije přepnutí timeframu
i přetáčení playbacku; na koše aktuálního TF se mapuje až při kreslení.
*/
import type { ProfileRow } from '../profile/bars'

export interface RangeSelection {
  fromIso: string
  toIso: string
}

/** Okenní profil: rozdíl kumulativních volume řádků t2 − t1 (clamp na 0).

`rows1` prázdné = okno od začátku dat (baseline 0 — shodné s API, kde
`from` před prvním snapshotem znamená „od začátku seance"). OI složky
a vzdálenost od spotu zůstávají z t2. */
export function windowProfileRows(rows2: ProfileRow[], rows1: ProfileRow[]): ProfileRow[] {
  const baseline = new Map(rows1.map((row) => [row.strike, row]))
  return rows2.map((row) => {
    const base = baseline.get(row.strike)
    const callVolume = Math.max(0, row.callVolume - (base?.callVolume ?? 0))
    const putVolume = Math.max(0, row.putVolume - (base?.putVolume ?? 0))
    // |Δ| k t2 z komponenty: component = vol × |Δ| → škálování poměrem okna
    const callComponent =
      row.callVolume > 0 ? row.callVolComponent * (callVolume / row.callVolume) : 0
    const putComponent = row.putVolume > 0 ? row.putVolComponent * (putVolume / row.putVolume) : 0
    return {
      ...row,
      callVolume,
      putVolume,
      callVolComponent: callComponent,
      putVolComponent: putComponent,
    }
  })
}

/** Mapování ISO minuty na index 1m osy; nejbližší minuta ≤ iso (osa umí díry). */
export function minuteIndexFor(minutesIso: string[], iso: string): number | null {
  const target = Date.parse(iso)
  if (Number.isNaN(target)) return null
  let found: number | null = null
  for (let idx = 0; idx < minutesIso.length; idx += 1) {
    if (Date.parse(minutesIso[idx]) <= target) found = idx
    else break
  }
  return found
}

/** Range → indexy košů aktuálního TF; null když okno leží mimo osu. */
export function rangeBuckets(
  range: RangeSelection,
  minutesIso: string[],
  bucketMinutes: number,
): { startBucket: number; endBucket: number; fromIdx: number; toIdx: number } | null {
  const fromIdx = minuteIndexFor(minutesIso, range.fromIso)
  const toIdx = minuteIndexFor(minutesIso, range.toIso)
  if (fromIdx === null || toIdx === null || toIdx < fromIdx) return null
  return {
    fromIdx,
    toIdx,
    startBucket: Math.floor(fromIdx / bucketMinutes),
    endBucket: Math.floor(toIdx / bucketMinutes),
  }
}

/** CumΔ okna z flow řady dne: cum(≤t2) − cum(≤t1); kotva open se odečte. */
export function windowCumDelta(
  cumSeries: Array<{ tsIso: string; cumDelta: number | null }>,
  range: RangeSelection,
): number | null {
  const at = (limitIso: string): number => {
    const limit = Date.parse(limitIso)
    let value = 0
    for (const point of cumSeries) {
      if (Date.parse(point.tsIso) > limit) break
      if (point.cumDelta !== null) value = point.cumDelta
    }
    return value
  }
  if (cumSeries.length === 0) return null
  return at(range.toIso) - at(range.fromIso)
}

/** URL serializace (?range=fromIso~toIso) — share/restore při reloadu. */
export function encodeRange(range: RangeSelection): string {
  return `${range.fromIso}~${range.toIso}`
}

export function decodeRange(raw: string | null): RangeSelection | null {
  if (!raw) return null
  const [fromIso, toIso] = raw.split('~')
  if (!fromIso || !toIso) return null
  const from = Date.parse(fromIso)
  const to = Date.parse(toIso)
  if (Number.isNaN(from) || Number.isNaN(to) || to < from) return null
  return { fromIso, toIso }
}

/** Popisek chipu: „15:30–16:05" lokálním časem — vždy 24h (aplikace je česká;
CI v en-US by jinak dalo „03:30 PM"). */
export function rangeLabel(range: RangeSelection): string {
  const fmt = (iso: string) =>
    new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  return `${fmt(range.fromIso)}–${fmt(range.toIso)}`
}

/** Reakční okno zprávy (#488): ts_event → ts_event + minut, clamp na živou hranu.

Okna 15/60 min jsou TATÁŽ okna jako `news_reactions` (sentiment-SPEC 2.2) —
žádný duplicitní výpočet. `open` = okno ještě běží (t2 spadlo za poslední
minutu dat) — chip range to přizná místo tichého ořezu.
*/
export const REACTION_RANGE_MINUTES = [15, 60] as const

export function reactionWindow(
  tsEventIso: string,
  minutes: number,
  lastDataIso: string | null,
): { range: RangeSelection; open: boolean } | null {
  const start = Date.parse(tsEventIso)
  if (Number.isNaN(start) || minutes <= 0) return null
  const end = start + minutes * 60_000
  const lastData = lastDataIso === null ? Number.NaN : Date.parse(lastDataIso)
  if (Number.isNaN(lastData) || lastData <= start) return null // okno nemá žádná data
  const open = end > lastData
  return {
    range: {
      fromIso: new Date(start).toISOString(),
      toIso: new Date(Math.min(end, lastData)).toISOString(),
    },
    open,
  }
}
