/** Automatické seance markery světových burz (SPEC 7.2 Sessions).

Časy jsou definované v LOKÁLNÍM čase burzy + IANA zóně a na UTC se převádí
přes `instrument/tz.ts` (#511) — DST tak řeší zoneinfo prohlížeče, ne vlastní
aproximace po celých dnech UTC (ta se na přechodovém víkendu míjela o hodinu,
#159). Burzy bez DST (Tokio, Šanghaj, Indie) padnou na stejné UTC minuty jako
dřív; Sydney tím dostává korektní posun AEST/AEDT zdarma. Marker se umístí na
první minutu dne >= času seance; seance mimo rozsah minut se vynechají.
*/
import type { SessionMarker } from '../heatmap/overlays'
import { zonedTimeUtc } from './tz'

const WORLD_SESSIONS: Array<{
  label: string
  hour: number
  minute: number
  /** IANA zóna burzy — lokální čas výše se převádí DST-korektně (#511). */
  tz: string
}> = [
  { label: 'Sydney', hour: 10, minute: 0, tz: 'Australia/Sydney' },
  { label: 'Tokio', hour: 9, minute: 0, tz: 'Asia/Tokyo' },
  { label: 'Šanghaj', hour: 9, minute: 30, tz: 'Asia/Shanghai' },
  { label: 'Indie', hour: 9, minute: 15, tz: 'Asia/Kolkata' },
  { label: 'Sydney Cl', hour: 16, minute: 0, tz: 'Australia/Sydney' },
  { label: 'Tokio Cl', hour: 15, minute: 0, tz: 'Asia/Tokyo' },
  { label: 'Šanghaj Cl', hour: 15, minute: 0, tz: 'Asia/Shanghai' },
  { label: 'Frankfurt', hour: 9, minute: 0, tz: 'Europe/Berlin' },
  { label: 'Londýn', hour: 8, minute: 0, tz: 'Europe/London' },
  { label: 'US Pre', hour: 8, minute: 0, tz: 'America/New_York' },
  { label: 'US Open', hour: 9, minute: 30, tz: 'America/New_York' },
  { label: 'Indie Cl', hour: 15, minute: 30, tz: 'Asia/Kolkata' },
  { label: 'Frankfurt Cl', hour: 17, minute: 30, tz: 'Europe/Berlin' },
  { label: 'Londýn Cl', hour: 16, minute: 30, tz: 'Europe/London' },
  { label: 'US Close', hour: 16, minute: 0, tz: 'America/New_York' },
]

/** Čas seance v den `dayStart` (epoch ms) — lokální čas burzy → UTC (#511). */
function sessionAtUtc(dayStart: Date, session: (typeof WORLD_SESSIONS)[number]): number {
  return zonedTimeUtc(
    session.tz,
    dayStart.getUTCFullYear(),
    dayStart.getUTCMonth() + 1,
    dayStart.getUTCDate(),
    session.hour,
    session.minute,
  )
}

/** Slučování popisků na téže pozici — kreslí se pod sebou (#193). */
function mergeMarkers(byIndex: Map<number, string[]>): SessionMarker[] {
  return [...byIndex.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([minuteIdx, labels]) => ({ minuteIdx, label: labels.join(' · ') }))
}

/** Markery pro den daný ISO minutami (UTC); mimo rozsah dne se vynechají.

Seance padnoucí na tutéž minutu se slučují do jednoho popisku — jinak by se
texty na ose překrývaly (Frankfurt a Londýn otevírají ve stejnou UTC minutu). */
export function autoSessions(minuteKeysIso: string[]): SessionMarker[] {
  if (minuteKeysIso.length === 0) return []
  const times = minuteKeysIso.map((iso) => new Date(iso).getTime())
  const dayStart = new Date(minuteKeysIso[0])
  const byMinute = new Map<number, string[]>()
  for (const session of WORLD_SESSIONS) {
    const at = sessionAtUtc(dayStart, session)
    // Jen seance uvnitř rozsahu dat (minutová tolerance na začátku dne)
    if (at < times[0] - 60_000 || at > times[times.length - 1]) continue
    const minuteIdx = times.findIndex((t) => t >= at)
    if (minuteIdx < 0) continue
    const labels = byMinute.get(minuteIdx)
    if (labels) labels.push(session.label)
    else byMinute.set(minuteIdx, [session.label])
  }
  return mergeMarkers(byMinute)
}

/** Markery seancí v PROJEKTOVANÉ zóně (#195): mezi poslední naměřenou minutou
a settle. `minuteIdx` je v prostoru košů projektované osy — koš
`dataBuckets + k` pokrývá čas `last + (k+1) × bucket` (shodně s
`projectionLabels`), takže US Open ap. jsou vidět dřív, než začnou. */
export function projectedSessions(
  lastMinuteIso: string,
  settle: Date | null,
  bucketMinutes: number,
  dataBuckets: number,
): SessionMarker[] {
  const last = new Date(lastMinuteIso).getTime()
  if (Number.isNaN(last) || settle === null) return []
  const dayStart = new Date(lastMinuteIso)
  const bucketMs = Math.max(1, bucketMinutes) * 60_000
  const byBucket = new Map<number, string[]>()
  for (const session of WORLD_SESSIONS) {
    const at = sessionAtUtc(dayStart, session)
    if (at <= last || at > settle.getTime()) continue
    const index = Math.max(0, Math.ceil((at - last) / bucketMs) - 1)
    const minuteIdx = dataBuckets + index
    const labels = byBucket.get(minuteIdx)
    if (labels) labels.push(session.label)
    else byBucket.set(minuteIdx, [session.label])
  }
  return mergeMarkers(byBucket)
}
