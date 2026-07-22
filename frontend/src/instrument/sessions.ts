/** Automatické seance markery světových burz (SPEC 7.2 Sessions, Moodix styl).

Časy jsou uložené jako LETNÍ UTC; US a evropské seance dostávají aproximaci
DST (#159) — mimo letní čas se posouvají o hodinu později. Aproximace pracuje
s celými dny UTC (přechodový víkend může být ±hodinu vedle), asijské burzy
zůstávají pevně (Indie/Šanghaj DST nemají, Tokio taky ne; Sydney se záměrně
neřeší — jde o orientaci v grafu, ne o burzovní kalendář). Marker se umístí
na první minutu dne >= času seance; seance mimo rozsah minut se vynechají.
*/
import type { SessionMarker } from '../heatmap/overlays'

const WORLD_SESSIONS: Array<{
  label: string
  utcHour: number
  utcMinute: number
  /** Trh s letním časem: mimo DST se čas posouvá o hodinu později. */
  dst?: 'us' | 'eu'
}> = [
  { label: 'Sydney', utcHour: 0, utcMinute: 0 },
  { label: 'Tokio', utcHour: 0, utcMinute: 0 },
  { label: 'Šanghaj', utcHour: 1, utcMinute: 30 },
  { label: 'Indie', utcHour: 3, utcMinute: 45 },
  { label: 'Sydney Cl', utcHour: 6, utcMinute: 0 },
  { label: 'Tokio Cl', utcHour: 6, utcMinute: 0 },
  { label: 'Šanghaj Cl', utcHour: 7, utcMinute: 0 },
  { label: 'Frankfurt', utcHour: 7, utcMinute: 0, dst: 'eu' },
  { label: 'Londýn', utcHour: 7, utcMinute: 0, dst: 'eu' },
  { label: 'US Pre', utcHour: 12, utcMinute: 0, dst: 'us' },
  { label: 'US Open', utcHour: 13, utcMinute: 30, dst: 'us' },
  { label: 'Indie Cl', utcHour: 10, utcMinute: 0 },
  { label: 'Frankfurt Cl', utcHour: 15, utcMinute: 30, dst: 'eu' },
  { label: 'Londýn Cl', utcHour: 15, utcMinute: 30, dst: 'eu' },
  { label: 'US Close', utcHour: 20, utcMinute: 0, dst: 'us' },
]

/** N-tá neděle měsíce (UTC půlnoc); month 0-based. */
function nthSundayUtc(year: number, month: number, nth: number): number {
  const firstDay = new Date(Date.UTC(year, month, 1)).getUTCDay()
  return Date.UTC(year, month, 1 + ((7 - firstDay) % 7) + (nth - 1) * 7)
}

/** Poslední neděle měsíce (UTC půlnoc); month 0-based. */
function lastSundayUtc(year: number, month: number): number {
  const last = new Date(Date.UTC(year, month + 1, 0))
  return Date.UTC(year, month, last.getUTCDate() - last.getUTCDay())
}

/** US DST: 2. neděle března až 1. neděle listopadu (aproximace po dnech UTC). */
function isUsDst(at: number, year: number): boolean {
  return at >= nthSundayUtc(year, 2, 2) && at < nthSundayUtc(year, 10, 1)
}

/** EU DST: poslední neděle března až poslední neděle října. */
function isEuDst(at: number, year: number): boolean {
  return at >= lastSundayUtc(year, 2) && at < lastSundayUtc(year, 9)
}

/** Čas seance v daný den (epoch ms) včetně DST posunu trhu (#159). */
function sessionAtUtc(dayStart: Date, session: (typeof WORLD_SESSIONS)[number]): number {
  const year = dayStart.getUTCFullYear()
  let at = Date.UTC(
    year,
    dayStart.getUTCMonth(),
    dayStart.getUTCDate(),
    session.utcHour,
    session.utcMinute,
  )
  // Uložené časy jsou letní — mimo DST daného trhu o hodinu později
  const summer =
    session.dst === 'us' ? isUsDst(at, year) : session.dst === 'eu' ? isEuDst(at, year) : true
  if (!summer) at += 60 * 60_000
  return at
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
