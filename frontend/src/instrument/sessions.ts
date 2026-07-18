/** Automatické seance markery světových burz (SPEC 7.2 Sessions, Moodix styl).

Pevné UTC časy (bez DST korekcí — přibližná orientace v grafu, ne přesná
burzovní kalendarizace). Marker se umístí na první minutu dne ≥ času seance;
seance mimo rozsah zobrazených minut se vynechají.
*/
import type { SessionMarker } from '../heatmap/overlays'

const WORLD_SESSIONS: Array<{ label: string; utcHour: number; utcMinute: number }> = [
  { label: 'Tokio', utcHour: 0, utcMinute: 0 },
  { label: 'Tokio Cl', utcHour: 6, utcMinute: 0 },
  { label: 'Londýn', utcHour: 7, utcMinute: 0 },
  { label: 'US Pre', utcHour: 12, utcMinute: 0 },
  { label: 'US Open', utcHour: 13, utcMinute: 30 },
  { label: 'Londýn Cl', utcHour: 15, utcMinute: 30 },
  { label: 'US Close', utcHour: 20, utcMinute: 0 },
]

/** Markery pro den daný ISO minutami (UTC); mimo rozsah dne se vynechají. */
export function autoSessions(minuteKeysIso: string[]): SessionMarker[] {
  if (minuteKeysIso.length === 0) return []
  const times = minuteKeysIso.map((iso) => new Date(iso).getTime())
  const dayStart = new Date(minuteKeysIso[0])
  const markers: SessionMarker[] = []
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
    if (minuteIdx >= 0) markers.push({ minuteIdx, label: session.label })
  }
  return markers
}
