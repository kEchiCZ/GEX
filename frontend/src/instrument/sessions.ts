/** Automatické seance markery světových burz (SPEC 7.2 Sessions, Moodix styl).

Pevné UTC časy (bez DST korekcí — přibližná orientace v grafu, ne přesná
burzovní kalendarizace). Marker se umístí na první minutu dne ≥ času seance;
seance mimo rozsah zobrazených minut se vynechají.
*/
import type { SessionMarker } from '../heatmap/overlays'

const WORLD_SESSIONS: Array<{ label: string; utcHour: number; utcMinute: number }> = [
  { label: 'Sydney', utcHour: 0, utcMinute: 0 },
  { label: 'Tokio', utcHour: 0, utcMinute: 0 },
  { label: 'Šanghaj', utcHour: 1, utcMinute: 30 },
  { label: 'Indie', utcHour: 3, utcMinute: 45 },
  { label: 'Sydney Cl', utcHour: 6, utcMinute: 0 },
  { label: 'Tokio Cl', utcHour: 6, utcMinute: 0 },
  { label: 'Šanghaj Cl', utcHour: 7, utcMinute: 0 },
  { label: 'Frankfurt', utcHour: 7, utcMinute: 0 },
  { label: 'Londýn', utcHour: 7, utcMinute: 0 },
  { label: 'US Pre', utcHour: 12, utcMinute: 0 },
  { label: 'US Open', utcHour: 13, utcMinute: 30 },
  { label: 'Indie Cl', utcHour: 10, utcMinute: 0 },
  { label: 'Frankfurt Cl', utcHour: 15, utcMinute: 30 },
  { label: 'Londýn Cl', utcHour: 15, utcMinute: 30 },
  { label: 'US Close', utcHour: 20, utcMinute: 0 },
]

/** Markery pro den daný ISO minutami (UTC); mimo rozsah dne se vynechají.

Seance padnoucí na tutéž minutu se slučují do jednoho popisku — jinak by se
texty na ose překrývaly (Frankfurt a Londýn otevírají ve stejnou UTC minutu). */
export function autoSessions(minuteKeysIso: string[]): SessionMarker[] {
  if (minuteKeysIso.length === 0) return []
  const times = minuteKeysIso.map((iso) => new Date(iso).getTime())
  const dayStart = new Date(minuteKeysIso[0])
  const byMinute = new Map<number, string[]>()
  for (const session of WORLD_SESSIONS) {
    const at = Date.UTC(
      dayStart.getUTCFullYear(),
      dayStart.getUTCMonth(),
      dayStart.getUTCDate(),
      session.utcHour,
      session.utcMinute,
    )
    // Jen seance uvnitř rozsahu dat (minutová tolerance na začátku dne)
    if (at < times[0] - 60_000 || at > times[times.length - 1]) continue
    const minuteIdx = times.findIndex((t) => t >= at)
    if (minuteIdx < 0) continue
    const labels = byMinute.get(minuteIdx)
    if (labels) labels.push(session.label)
    else byMinute.set(minuteIdx, [session.label])
  }
  return [...byMinute.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([minuteIdx, labels]) => ({ minuteIdx, label: labels.join(' · ') }))
}
