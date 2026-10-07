/** Značky deníku na časové ose heatmapy (#673) — čisté funkce.

Záznamy deníku se mapují na koš osy **časem** (#1303, vzor news markerů
#1290, `AxisLocator`): dřív shodou popisku HH:MM, takže na 5m a delších TF
zmizel záznam mimo hranici koše a záznam z pauzy CME neměl sloupec. Víc
záznamů v témže koši = jedna značka s počtem. Zobrazení je vypínatelné přes
Traders mode.
*/
import type { JournalEntry } from '../api/journal'
import type { AxisLocator } from './axisLocator'

export interface JournalMarker {
  minuteIdx: number
  /** Kolik záznamů padlo do téže minuty — jedna značka s badge. */
  count: number
  entries: JournalEntry[]
}

/** Glyf podle typu záznamu (#715) — obchod se má poznat od poznámky. */
export function journalGlyph(entries: JournalEntry[]): string {
  // Ve shluku vyhrává „nejsilnější" typ: obchod > promeškané > poznámka
  if (entries.some((entry) => entry.entry_type === 'obchod')) return '◆'
  if (entries.some((entry) => entry.entry_type === 'promeskane')) return '○'
  return '✎'
}

/**
 * Barva značky podle výsledku obchodů ve shluku (#715).
 *
 * `null` = neutrální (poznámky, neuzavřené obchody) — barva se dosazuje jen
 * tam, kde výsledek opravdu známe.
 */
export function journalMarkerColor(entries: JournalEntry[]): 'win' | 'loss' | null {
  let total = 0
  let known = false
  for (const entry of entries) {
    const net = entry.trade?.net_pnl
    if (net === null || net === undefined) continue
    known = true
    total += net
  }
  if (!known || total === 0) return null
  return total > 0 ? 'win' : 'loss'
}

export function buildJournalMarkers(entries: JournalEntry[], locate: AxisLocator): JournalMarker[] {
  const clusters = new Map<number, JournalMarker>()
  for (const entry of entries) {
    const index = locate(entry.ts_ref)
    if (index === null) continue // mimo osu (jiný den)
    const existing = clusters.get(index)
    if (existing) {
      clusters.set(index, {
        ...existing,
        count: existing.count + 1,
        entries: [...existing.entries, entry],
      })
    } else {
      clusters.set(index, { minuteIdx: index, count: 1, entries: [entry] })
    }
  }
  return [...clusters.values()].sort((a, b) => a.minuteIdx - b.minuteIdx)
}

/** Nejbližší značka do tolerance minut; null mimo dosah (vzor markerNear). */
export function journalMarkerNear(
  markers: JournalMarker[],
  minuteIdx: number,
  tolerance: number,
): JournalMarker | null {
  let best: JournalMarker | null = null
  let bestDistance = Infinity
  for (const marker of markers) {
    const distance = Math.abs(marker.minuteIdx - minuteIdx)
    if (distance <= tolerance && distance < bestDistance) {
      best = marker
      bestDistance = distance
    }
  }
  return best
}
