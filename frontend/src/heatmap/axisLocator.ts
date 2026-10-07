/** Poloha okamžiku na ose grafu (#1303) — sdílená cesta značek deníku, signálů
a expirací, vzor news markerů (#1290).

Intraday: koš seance, který okamžik obsahuje (`newsBucketIndex` nad úsekem
zobrazeného dne); minulý okamžik v pauze CME nebo v díře se přimkne k poslednímu
koši před ním, budoucí se kreslí jen do koše projekce. Daily: sloupec dne
(popisek data — sloupec osy JE den). Dřív se všechny tři značky párovaly
shodou popisku HH:MM a na 5m/15m/1h zmizely mimo hranici koše. */
import type { AxisSegment } from './newsMarkers'
import { newsBucketIndex } from './newsMarkers'

/** ISO okamžik → index sloupce osy; null = mimo osu. */
export type AxisLocator = (iso: string) => number | null

/** Lokátor intradenní osy nad úseky seancí; `nowMs` odliší budoucí okamžik. */
export function sessionAxisLocator(segments: readonly AxisSegment[], nowMs: number): AxisLocator {
  return (iso) => {
    const ms = Date.parse(iso)
    if (Number.isNaN(ms)) return null
    return newsBucketIndex(ms, ms > nowMs, segments)
  }
}

/** Lokátor Daily osy: sloupec podle data (`dayLabel` téhož formatteru jako osa). */
export function dailyAxisLocator(
  labels: readonly string[],
  dayLabel: (dateIso: string) => string,
): AxisLocator {
  const indexByLabel = new Map<string, number>()
  labels.forEach((label, index) => {
    if (!indexByLabel.has(label)) indexByLabel.set(label, index)
  })
  return (iso) => indexByLabel.get(dayLabel(iso.slice(0, 10))) ?? null
}
