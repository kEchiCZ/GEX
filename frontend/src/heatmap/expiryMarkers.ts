/** ⌛ značky kalendáře expirací v ose grafu (#1189) — čisté funkce.

Stejný princip jako news markery (#1290, #1303): značka padne do koše osy
podle **času** (`AxisLocator` — intraday koš seance, Daily den), ne shodou
popisku HH:MM, kterou na 5m a delších TF značka mimo hranici koše minula.
Budoucí značka (SOQ později dnes) se kreslí jen do koše projekce, který ji
obsahuje — k živé hraně se nepřimyká. */
import type { CalendarMarkerRow } from '../api/calendar'
import type { AxisLocator } from './axisLocator'

export interface ExpiryMarker {
  minuteIdx: number
  kind: CalendarMarkerRow['kind']
  label: string
  /** Kvartální expirace a roll jsou plné; měsíční OPEX a VIX slabší. */
  major: boolean
}

export const EXPIRY_GLYPH = '⌛'

export function buildExpiryMarkers(
  markers: CalendarMarkerRow[],
  locate: AxisLocator,
): ExpiryMarker[] {
  const result: ExpiryMarker[] = []
  const seen = new Set<number>()
  for (const marker of markers) {
    const index = locate(marker.ts)
    if (index === null || seen.has(index)) continue
    seen.add(index)
    result.push({
      minuteIdx: index,
      kind: marker.kind,
      label: marker.label,
      major: marker.kind === 'quarterly_expiry' || marker.kind === 'roll',
    })
  }
  return result
}
