/** ⌛ značky kalendáře expirací v ose grafu (#1189) — čisté funkce.

Stejný princip jako news markery: značka se páruje na sloupec osy popiskem
z TÉHOŽ formatteru, kterým vznikly popisky (intraday minuta, Daily den).
Intradenní osa nese jen čas, proto se berou jen značky ze dnů, které osa
obsahuje (`axisDates`) — jinak by páteční SOQ 15:30 seděl na každé středě. */
import type { CalendarMarkerRow } from '../api/calendar'

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
  labels: string[],
  formatLabel: (iso: string) => string,
  axisDates: ReadonlySet<string> | null,
): ExpiryMarker[] {
  if (labels.length === 0 || markers.length === 0) return []
  const indexByLabel = new Map<string, number>()
  labels.forEach((label, index) => {
    if (!indexByLabel.has(label)) indexByLabel.set(label, index)
  })
  const result: ExpiryMarker[] = []
  const seen = new Set<number>()
  for (const marker of markers) {
    if (axisDates !== null && !axisDates.has(marker.ts.slice(0, 10))) continue
    const index = indexByLabel.get(formatLabel(marker.ts))
    if (index === undefined || seen.has(index)) continue
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

/** UTC dny (YYYY-MM-DD), které intradenní osa pokrývá — z ISO minut dne. */
export function axisDatesOf(minutesIso: readonly string[]): Set<string> {
  const dates = new Set<string>()
  for (const iso of minutesIso) dates.add(iso.slice(0, 10))
  return dates
}
