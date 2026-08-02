/** Markery zpráv na časové ose heatmapy (#287, SPEC 9.1) — čisté funkce.

Zprávy se mapují na minutu grafu **podle času, ne podle pořadí** — a stejným
formatterem, jakým vznikly popisky osy. Vlastní formátování by se s osou
rozešlo a markery by tiše zmizely (přesně to se stalo u panelu v #288).

Nadcházející plánované eventy se kreslí **do projekční zóny** vpravo od živé
hrany: trader má vidět, že ve 14:30 přijde CPI, dřív než přijde. Odlišují se
dutým markerem, protože o jejich dopadu se zatím nic neví.
*/
import { categoryGlyph } from '../api/news'
import type { NewsRow } from '../api/news'

export interface NewsMarker {
  minuteIdx: number
  /** Kolik zpráv padlo do téže minuty — kreslí se jeden marker s badge (SPEC 9.1). */
  count: number
  /** Součet skóre clusteru; rozhoduje o barvě. */
  score: number
  /** Nejvyšší důležitost v clusteru — řídí jas a tloušťku. */
  importance: number
  glyph: string
  /** Plánovaný event, který ještě nenastal → dutý marker s countdownem. */
  upcoming: boolean
  titles: string[]
  /** Zprávy clusteru — dialog po kliknutí na marker je zobrazí celé (#408). */
  rows: NewsRow[]
}

/** Číslo z API; `Numeric` sloupce můžou dorazit jako řetězec (PG Decimal). */
function asNumber(value: number | string | null): number {
  if (value === null || value === '') return 0
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

export function buildNewsMarkers(
  news: NewsRow[],
  upcoming: NewsRow[],
  labels: string[],
  formatLabel: (iso: string) => string,
): NewsMarker[] {
  if (labels.length === 0) return []
  // Popisek → index. Při shodě popisků (víc dnů na jedné ose) vyhrává první,
  // aby se marker nepřilepil na konec osy.
  const indexByLabel = new Map<string, number>()
  labels.forEach((label, index) => {
    if (!indexByLabel.has(label)) indexByLabel.set(label, index)
  })

  const clusters = new Map<number, NewsMarker>()
  const add = (row: NewsRow, isUpcoming: boolean) => {
    const index = indexByLabel.get(formatLabel(row.ts_event))
    if (index === undefined) return // mimo osu (jiný den nebo za horizontem projekce)
    const existing = clusters.get(index)
    const score = asNumber(row.sentiment_score)
    const importance = row.importance ?? 1
    if (existing) {
      clusters.set(index, {
        ...existing,
        count: existing.count + 1,
        score: existing.score + score,
        importance: Math.max(existing.importance, importance),
        titles: [...existing.titles, row.title],
        rows: [...existing.rows, row],
        // Cluster je „nadcházející" jen když v něm není nic proběhlého
        upcoming: existing.upcoming && isUpcoming,
      })
      return
    }
    clusters.set(index, {
      minuteIdx: index,
      count: 1,
      score,
      importance,
      glyph: categoryGlyph(row.category),
      upcoming: isUpcoming,
      titles: [row.title],
      rows: [row],
    })
  }

  for (const row of news) add(row, false)
  for (const row of upcoming) add(row, true)

  return [...clusters.values()].sort((a, b) => a.minuteIdx - b.minuteIdx)
}

/** Barva markeru: teal kladné, červená záporné, šedá neutrální/nezměřené. */
export function markerColor(marker: NewsMarker, alpha: number): string {
  if (marker.upcoming || marker.score === 0) return `rgba(125,133,150,${alpha})`
  return marker.score > 0 ? `rgba(20,184,166,${alpha})` : `rgba(224,82,96,${alpha})`
}

/** Jas a tloušťka podle důležitosti — okrajová zpráva nesmí křičet jako FOMC. */
export function markerStyle(marker: NewsMarker): { alpha: number; width: number } {
  switch (marker.importance) {
    case 3:
      return { alpha: 0.95, width: 2 }
    case 2:
      return { alpha: 0.7, width: 1.5 }
    default:
      return { alpha: 0.45, width: 1 }
  }
}

/** Marker na dané minutě (pro readout u crosshairu); null = žádný. */
export function markerAt(markers: NewsMarker[], minuteIdx: number | null): NewsMarker | null {
  if (minuteIdx === null) return null
  return markers.find((marker) => marker.minuteIdx === minuteIdx) ?? null
}

/** Nejbližší marker do tolerance minut — hit-test kliknutí na glyf (#408).

Klik nikdy netrefí přesnou minutu (glyf je pár px široký, osa zoomovaná),
proto se hledá nejbližší marker v okolí; při shodě vzdáleností vyhrává první. */
export function markerNear(
  markers: NewsMarker[],
  minuteIdx: number,
  tolerance: number,
): NewsMarker | null {
  let best: NewsMarker | null = null
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

/** Očekávaný dopad zprávy na trh: 1 = long, −1 = short, 0 = neutrální/nezměřené.

Klasifikovaný směr (sentiment_dir) má přednost; bez něj rozhoduje znaménko
skóre. Nadcházející eventy směr nemají — o dopadu se před výsledkem neví nic. */
export function expectedImpact(row: NewsRow): -1 | 0 | 1 {
  if (row.sentiment_dir === 1 || row.sentiment_dir === -1) return row.sentiment_dir
  const score = asNumber(row.sentiment_score)
  if (score > 0) return 1
  if (score < 0) return -1
  return 0
}

/** Významné zprávy (importance ≥ 2) — filtr markerů „Významné" (#408). */
export function significantOnly(rows: NewsRow[]): NewsRow[] {
  return rows.filter((row) => (row.importance ?? 1) >= 2)
}
