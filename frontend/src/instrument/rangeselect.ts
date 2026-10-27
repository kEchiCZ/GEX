/** Range selector (#484) — čisté funkce okna [t1, t2] nad denními daty.

Okenní profil se počítá V KLIENTOVI z už načteného /replay bundlu (posudek
#484): volume ve snapshotech je kumulativní denní (SPEC 4.3), objem okna je
Σ přírůstků proti průběžnému maximu kontraktu přes minuty okna
(`windowVolumes`, #1631) — identická aritmetika jako API /profile?from=&to=
(`window_volumes`), jen bez roundtripu. OI zůstává statické k t2 (otevřené
pozice nejsou tok), |Δ| k t2 se zpětně odvodí z komponenty (component = vol × |Δ|).

Rozsah se drží jako pár ISO minut (1m osa) — přežije přepnutí timeframu
i přetáčení playbacku; na koše aktuálního TF se mapuje až při kreslení.
*/
import type { RawDay } from '../heatmap/modes'
import type { ProfileRow } from '../profile/bars'

export interface RangeSelection {
  fromIso: string
  toIso: string
}

/** Objem okna per strike: call a put strana. */
export type WindowVolumes = Map<number, { call: number; put: number }>

/** Objem okna [fromIdx, toIdx] (1m indexy, obě meze včetně) per strike (#1631).

Σ přírůstků kumulativního volume proti průběžnému maximu kontraktu — tentýž
vzorec jako panel Opt Vol (`ensureDerived`) a API `window_volumes`, vč. catch-up
minuty a minuty po mezeře (objem okna, ne minutový obchod). Kmitání ani propad
IBKR v t1 nebo t2 okno nezkreslí, minuta bez objemu (maska 0) nic nepřidá
a strike bez měření před oknem první hodnotou v okně jen založí stav. Bez
snapshotu před oknem = okno od začátku seance: celý kumulativ do t2 (shodně
s API, kde `from` před prvním snapshotem). null = den bez masky objemu. */
export function windowVolumes(raw: RawDay, fromIdx: number, toIdx: number): WindowVolumes | null {
  const { callVolumeKnown, putVolumeKnown, snapshotMinutes } = raw
  if (!callVolumeKnown || !putVolumeKnown || !snapshotMinutes) return null
  const stride = raw.stride ?? raw.minutes
  const last = Math.min(toIdx, raw.minutes - 1)
  const fromStart = !snapshotMinutes.slice(0, Math.max(0, fromIdx)).some(Boolean)
  const side = (volume: Float32Array, known: Float32Array, row: number): number => {
    let max = Number.NaN
    let base = Number.NaN
    let first = Number.NaN
    for (let minuteIdx = 0; minuteIdx <= last; minuteIdx += 1) {
      if (known[row + minuteIdx] === 0) continue
      const value = volume[row + minuteIdx]
      if (minuteIdx >= fromIdx && Number.isNaN(first)) first = value
      if (Number.isNaN(max) || value > max) max = value
      if (minuteIdx < fromIdx) base = max
    }
    if (Number.isNaN(max)) return 0
    if (fromStart) return max
    return Number.isNaN(base) ? (Number.isNaN(first) ? 0 : max - first) : max - base
  }
  const result: WindowVolumes = new Map()
  raw.strikes.forEach((strike, strikeIdx) => {
    const row = strikeIdx * stride
    result.set(strike, {
      call: side(raw.callVolume, callVolumeKnown, row),
      put: side(raw.putVolume, putVolumeKnown, row),
    })
  })
  return result
}

/** Okenní profil: řádky k t2 s objemem okna z `windowVolumes` (#1631). OI
složky a vzdálenost od spotu zůstávají z t2; strike bez objemu okna má 0. */
export function windowProfileRows(rows2: ProfileRow[], volumes: WindowVolumes): ProfileRow[] {
  return rows2.map((row) => {
    const window = volumes.get(row.strike)
    const callVolume = window?.call ?? 0
    const putVolume = window?.put ?? 0
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

/** CumΔ okna z minutové řady panelu (#483): úroveň(t2) − úroveň(t1 − 1).
Mezera (null) uvnitř okna = `'gap'` — rozdíl přes ni je neúplný (ADR-0057
bod 4, shodně s API `/flow` okna); hranice v mezeře bere poslední měřenou
úroveň ≤ hranice, před prvním měřením 0 (kotva seance). */
export function panelWindowCumDelta(
  cum: (number | null)[],
  fromIdx: number,
  toIdx: number,
): number | 'gap' | null {
  if (cum.length === 0) return null
  const clamp = (idx: number) => Math.max(0, Math.min(idx, cum.length - 1))
  const levelAt = (idx: number): number => {
    for (let index = clamp(idx); index >= 0; index -= 1) {
      const value = cum[index]
      if (value !== null) return value
    }
    return 0
  }
  for (let index = clamp(fromIdx); index <= clamp(toIdx); index += 1) {
    if (cum[index] === null) return 'gap'
  }
  return levelAt(toIdx) - (fromIdx > 0 ? levelAt(fromIdx - 1) : 0)
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

/** Navazující okno B za A (#489): stejná šířka hned za koncem A, clamp na
poslední minutu dat; null když za A žádná data nejsou. */
export function followUpRange(
  a: RangeSelection,
  lastDataIso: string | null,
): RangeSelection | null {
  const start = Date.parse(a.fromIso)
  const end = Date.parse(a.toIso)
  const lastData = lastDataIso === null ? Number.NaN : Date.parse(lastDataIso)
  if (Number.isNaN(start) || Number.isNaN(end) || Number.isNaN(lastData)) return null
  const width = end - start // inkluzivní rozpětí A v ms (od→do poslední minuty)
  const bStart = end + 60_000 // navazuje NA další minutu, okna se nepřekrývají
  if (bStart > lastData) return null
  return {
    fromIso: new Date(bStart).toISOString(),
    toIso: new Date(Math.min(bStart + width, lastData)).toISOString(),
  }
}

/** Pre/post okna kolem eventu (#489): A = event−minut → event, B = event → +minut. */
export function prePostWindows(
  tsEventIso: string,
  minutes: number,
  lastDataIso: string | null,
): { a: RangeSelection; b: RangeSelection; bOpen: boolean } | null {
  const event = Date.parse(tsEventIso)
  if (Number.isNaN(event) || minutes <= 0) return null
  const post = reactionWindow(tsEventIso, minutes, lastDataIso)
  if (!post) return null
  return {
    a: {
      fromIso: new Date(event - minutes * 60_000).toISOString(),
      // A končí minutu PŘED eventem — event a reakce patří do B
      toIso: new Date(event - 60_000).toISOString(),
    },
    b: post.range,
    bOpen: post.open,
  }
}
