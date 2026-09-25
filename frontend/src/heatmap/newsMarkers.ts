/** Markery zpráv na časové ose heatmapy (#287, SPEC 9.1) — čisté funkce.

Zprávy se mapují na koš grafu **časem** (#1290): koš je wall-clock interval
[start, start + TF) a zpráva padne do toho, který obsahuje její `ts_event`.
Dřív se párovalo shodou popisku `HH:MM`, takže na 5m a delších TF zmizela
každá zpráva mimo hranici koše (13:02 na 5m) a zprávy z pauzy CME neměly
sloupec vůbec.

Nadcházející plánované eventy se kreslí **do projekční zóny** vpravo od živé
hrany: trader má vidět, že ve 14:30 přijde CPI, dřív než přijde. Odlišují se
dutým markerem, protože o jejich dopadu se zatím nic neví. Nadcházející je jen
**plánovaný** event (`kind = scheduled`) — titulek z feedu už proběhl, i když
jeho `ts_event` leží za posledním tikem „teď".
*/
import { categoryGlyph } from '../api/news'
import type { ChartNewsRow } from '../api/news'

export interface NewsMarker {
  /** Koš osy grafu; historie (#788) má záporné indexy. */
  minuteIdx: number
  /** Kolik zpráv padlo do téhož koše — kreslí se jeden marker s badge (SPEC 9.1). */
  count: number
  /** Součet skóre clusteru; rozhoduje o barvě. */
  score: number
  /** Nejvyšší důležitost v clusteru — řídí jas a tloušťku. */
  importance: number
  glyph: string
  /** Plánovaný event, který ještě nenastal → dutý marker s countdownem. */
  upcoming: boolean
  /** Zprávy clusteru — dialog po kliknutí na marker je zobrazí celé (#408). */
  rows: ChartNewsRow[]
}

/** Úsek osy grafu jedné seance: dnešek (koše od 0) nebo historický den (#788). */
export interface AxisSegment {
  /** Okno seance [openMs, closeMs) — zprávy z něj patří tomuto úseku. */
  openMs: number
  closeMs: number
  /** Wall-clock starty košů úseku, vzestupně (`bucketStartsMs` + projekce). */
  startsMs: ArrayLike<number>
  bucketMs: number
  /** Index prvního koše úseku na ose grafu (dnešek 0, historie záporné). */
  firstIdx: number
}

/** Zpráva s předparsovaným časem — `Date.parse` se platí jednou při načtení. */
export type TimedNewsRow = ChartNewsRow & { tsMs: number }

/** Číslo z API; `Numeric` sloupce můžou dorazit jako řetězec (PG Decimal). */
function asNumber(value: number | string | null | undefined): number {
  if (value === null || value === undefined || value === '') return 0
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

/** Poslední index `k` s `starts[k] ≤ ms`; −1 = před prvním košem. */
function lastStartAtOrBefore(starts: ArrayLike<number>, ms: number): number {
  let low = 0
  let high = starts.length - 1
  let found = -1
  while (low <= high) {
    const middle = (low + high) >> 1
    if (starts[middle] <= ms) {
      found = middle
      low = middle + 1
    } else {
      high = middle - 1
    }
  }
  return found
}

/** Koš osy grafu pro okamžik `ms`; null = mimo osu.

- Zpráva patří úseku své seance; mimo všechny úseky se nekreslí.
- Uvnitř seance padne do koše, který ji obsahuje. Když koš neexistuje (pauza
  CME 16–17 CT, díra ve sběru, minuta ještě mimo grid), přimkne se
  k poslednímu koši ≤ čas zprávy; před prvním košem k prvnímu — **zpráva dne
  nesmí z grafu zmizet** (#1290 AC).
- Nadcházející event (`upcoming`) se nepřimyká: bez koše (za horizontem
  projekce) se nekreslí, jinak by se budoucnost nalepila na živou hranu. */
export function newsBucketIndex(
  ms: number,
  upcoming: boolean,
  segments: readonly AxisSegment[],
): number | null {
  for (const segment of segments) {
    if (ms < segment.openMs || ms >= segment.closeMs) continue
    const count = segment.startsMs.length
    if (count === 0) return null
    const found = lastStartAtOrBefore(segment.startsMs, ms)
    if (upcoming) {
      if (found < 0 || ms >= segment.startsMs[found] + segment.bucketMs) return null
      return segment.firstIdx + found
    }
    return segment.firstIdx + Math.max(0, found)
  }
  return null
}

/** Nadcházející = plánovaný event s časem po `nowMs`. Titulek, broker ani
sociální síť nadcházející nejsou nikdy: `nowMs` se posouvá po minutách a čerstvý
titulek by jinak do dalšího tiku visel jako dutý marker v projekci (#1290). */
export function isUpcoming(row: Pick<ChartNewsRow, 'kind'>, tsMs: number, nowMs: number): boolean {
  return row.kind === 'scheduled' && tsMs > nowMs
}

/** Přidá zprávu do clusteru koše `index` (mutuje mapu — volá se jen při stavbě). */
function addToCluster(
  clusters: Map<number, NewsMarker>,
  index: number,
  row: ChartNewsRow,
  upcoming: boolean,
): void {
  const score = asNumber(row.sentiment_score)
  const importance = row.importance ?? 1
  const existing = clusters.get(index)
  if (existing) {
    existing.count += 1
    existing.score += score
    existing.importance = Math.max(existing.importance, importance)
    existing.rows.push(row)
    // Cluster je „nadcházející" jen když v něm není nic proběhlého
    existing.upcoming = existing.upcoming && upcoming
    return
  }
  clusters.set(index, {
    minuteIdx: index,
    count: 1,
    score,
    importance,
    glyph: categoryGlyph(row.category),
    upcoming,
    rows: [row],
  })
}

/** Markery z řádků; cluster = koš grafu. Nadcházející podle `isUpcoming`.

Duplicitní id (tatáž zpráva ze dne i z `/news/upcoming`) se počítá jednou —
dřív z ní vznikl marker s počtem 2 a plný místo dutého (#1290). */
export function buildNewsMarkers(
  rows: Iterable<TimedNewsRow>,
  segments: readonly AxisSegment[],
  nowMs: number,
): NewsMarker[] {
  if (segments.length === 0) return []
  const clusters = new Map<number, NewsMarker>()
  const seen = new Set<number>()
  for (const row of rows) {
    if (seen.has(row.id)) continue
    seen.add(row.id)
    const upcoming = isUpcoming(row, row.tsMs, nowMs)
    const index = newsBucketIndex(row.tsMs, upcoming, segments)
    if (index !== null) addToCluster(clusters, index, row, upcoming)
  }
  return [...clusters.values()].sort((a, b) => a.minuteIdx - b.minuteIdx)
}

/** Marker ze zpráv upozornění (#1290) — dialog prokliku ze zvonečku.

Skládá se stejně jako cluster v grafu (barva, důležitost); `minuteIdx` dodá
volající (−1 = mimo zobrazenou osu, jen dialog bez posunu); null = žádné zprávy. */
export function markerFromRows(
  rows: readonly ChartNewsRow[],
  minuteIdx: number,
  nowMs: number,
): NewsMarker | null {
  const clusters = new Map<number, NewsMarker>()
  for (const row of rows) {
    addToCluster(clusters, minuteIdx, row, isUpcoming(row, Date.parse(row.ts_event), nowMs))
  }
  return clusters.get(minuteIdx) ?? null
}

interface ClosedDayEntry {
  startsMs: ArrayLike<number>
  firstIdx: number
  bucketMs: number
  importantOnly: boolean
  pinnedIds: ReadonlySet<number>
  markers: NewsMarker[]
}

/** Markery uzavřených seancí per identita dne (mapa dne se při změně kopíruje). */
const closedDayCache = new WeakMap<ReadonlyMap<number, TimedNewsRow>, ClosedDayEntry>()

/** Markery uzavřené seance (historie #788, proběhlá expirace) — s cache (#1290).

Uzavřený den je neměnný a v něm nic není nadcházející, takže se markery
přestaví jen při změně dne (nové načtení), osy (TF, posun slice) nebo filtru.
Živý WS push, minutové dotažení ani tik „teď" je nepřepočítávají — dřív
každá změna skládala markery všech dnů historie znovu. */
export function closedDayMarkers(
  day: ReadonlyMap<number, TimedNewsRow>,
  segment: AxisSegment,
  importantOnly: boolean,
  pinnedIds: ReadonlySet<number>,
): NewsMarker[] {
  const hit = closedDayCache.get(day)
  if (
    hit &&
    hit.startsMs === segment.startsMs &&
    hit.firstIdx === segment.firstIdx &&
    hit.bucketMs === segment.bucketMs &&
    hit.importantOnly === importantOnly &&
    hit.pinnedIds === pinnedIds
  ) {
    return hit.markers
  }
  const rows = importantOnly ? significantOnly([...day.values()], pinnedIds) : day.values()
  const markers = buildNewsMarkers(rows, [segment], Number.POSITIVE_INFINITY)
  const { startsMs, firstIdx, bucketMs } = segment
  closedDayCache.set(day, { startsMs, firstIdx, bucketMs, importantOnly, pinnedIds, markers })
  return markers
}

/** Marker na dané minutě (pro readout u crosshairu); null = žádný. */
export function markerAt(markers: NewsMarker[], minuteIdx: number | null): NewsMarker | null {
  if (minuteIdx === null) return null
  return markers.find((marker) => marker.minuteIdx === minuteIdx) ?? null
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

/** Pod tolik px na koš se markery kreslí jen jako čárky (#1290, varianta A). */
export const NEWS_DETAIL_MIN_PX = 4
/** Minimální rozestup glyfů v px — hustší glyfy by splynuly v nečitelnou kaši. */
export const NEWS_GLYPH_MIN_GAP_PX = 8
/** Rezerva za okrajem plátna — glyf s počtem přečnívá čárku o ~12 px. */
const VIEWPORT_MARGIN_PX = 16

export interface NewsTick {
  x: number
  color: string
  width: number
  dashed: boolean
}

export interface NewsGlyph {
  x: number
  /** Marker glyfu — hit-test kliknutí bere nakreslené glyfy (#1290). */
  marker: NewsMarker
  color: string
  glyph: string
  /** Počet zpráv clusteru; null = bez badge (jedna zpráva nebo hrubý zoom). */
  count: number | null
}

export interface NewsDrawPlan {
  /** Čárky seskupené podle stylu — jeden `stroke()` na skupinu, ne na marker. */
  ticks: Map<string, NewsTick[]>
  glyphs: NewsGlyph[]
}

/** Co z markerů nakreslit (#1290): jen viditelné a při hrubém zoomu bez detailu.

Denně je 1 000–1 250 clusterů (filtr „Vše"), dřív ~50. Kreslení každého glyfu
a počtu při každém panu by stálo desítky tisíc volání 2D API (#1274), proto:
- markery mimo viewport se přeskočí;
- pod `NEWS_DETAIL_MIN_PX` na koš jen čárky, glyf dostanou jen významné
  (importance ≥ 2) a bez počtu;
- glyfy se neslévají: bližší než `NEWS_GLYPH_MIN_GAP_PX` k už kreslenému
  se vynechají, přednost má vyšší důležitost. Čárka zůstává vždy — žádná
  zpráva z osy nezmizí. */
export function newsDrawPlan(
  markers: readonly NewsMarker[],
  minuteToX: (minuteIdx: number) => number,
  scaleX: number,
  width: number,
): NewsDrawPlan {
  const ticks = new Map<string, NewsTick[]>()
  const detailed = scaleX >= NEWS_DETAIL_MIN_PX
  const candidates: { marker: NewsMarker; x: number; color: string }[] = []
  for (const marker of markers) {
    const x = minuteToX(marker.minuteIdx) - 0.5 * scaleX
    if (x < -VIEWPORT_MARGIN_PX || x > width + VIEWPORT_MARGIN_PX) continue
    const { alpha, width: lineWidth } = markerStyle(marker)
    const color = markerColor(marker, alpha)
    const key = `${color}|${lineWidth}|${marker.upcoming ? 1 : 0}`
    let group = ticks.get(key)
    if (!group) {
      group = []
      ticks.set(key, group)
    }
    group.push({ x, color, width: lineWidth, dashed: marker.upcoming })
    if (detailed || marker.importance >= 2) {
      candidates.push({ marker, x, color: markerColor(marker, Math.min(1, alpha + 0.05)) })
    }
  }
  // Přednost vyšší důležitosti, v rámci ní časové pořadí (stabilní sort)
  candidates.sort((a, b) => b.marker.importance - a.marker.importance)
  // Obsazené sloupce px (posun o okraj viewportu) — O(n) místo párového srovnání
  const reach = NEWS_GLYPH_MIN_GAP_PX - 1
  const shift = VIEWPORT_MARGIN_PX + reach
  const taken = new Uint8Array(Math.ceil(width) + 2 * shift + 1)
  const glyphs: NewsGlyph[] = []
  for (const { marker, x, color } of candidates) {
    const column = Math.round(x) + shift
    let free = true
    for (let near = column - reach; near <= column + reach; near += 1) {
      if (taken[near]) {
        free = false
        break
      }
    }
    if (!free) continue
    taken[column] = 1
    glyphs.push({
      x,
      marker,
      color,
      glyph: marker.glyph,
      count: detailed && marker.count > 1 ? marker.count : null,
    })
  }
  return { ticks, glyphs }
}

/** Glyf se kreslí od `x − 4` px (11px písmo, ~12 px široký) — střed a poloviční
šířka zásahu. Glyfy stojí ≥ 8 px od sebe, takže se zásahy překrývají nejvýš
na okraji — tam rozhodne důležitost. */
const GLYPH_HIT_CENTER_PX = 2
const GLYPH_HIT_REACH_PX = 6
/** Dosah zásahu čárky bez glyfu v px (dřív ~6 px převedených na koše). */
const TICK_HIT_REACH_PX = 6

/** Marker pod kliknutím (#408, #1290) — podle toho, co je **nakreslené**.

Oddálený den má ~1 100 clusterů a glyf dostane jen část z nich (`newsDrawPlan`):
nejbližší marker podle indexu koše pak typicky trefil sousední drobnou zprávu
se skrytým glyfem. Proto:
1. glyfy z plánu kreslení v dosahu kliknutí — přednost vyšší důležitost, pak
   vzdálenost (glyf je širší než koš a překrývá sousední čárky);
2. jinak nejbližší čárka v dosahu px, při shodě vzdálenosti důležitější. */
export function newsMarkerAtX(
  markers: readonly NewsMarker[],
  minuteToX: (minuteIdx: number) => number,
  scaleX: number,
  width: number,
  x: number,
): NewsMarker | null {
  const plan = newsDrawPlan(markers, minuteToX, scaleX, width)
  let best: NewsMarker | null = null
  let bestDistance = Infinity
  for (const glyph of plan.glyphs) {
    const distance = Math.abs(x - (glyph.x + GLYPH_HIT_CENTER_PX))
    if (distance > GLYPH_HIT_REACH_PX) continue
    const better =
      best === null ||
      glyph.marker.importance > best.importance ||
      (glyph.marker.importance === best.importance && distance < bestDistance)
    if (better) {
      best = glyph.marker
      bestDistance = distance
    }
  }
  if (best) return best
  for (const marker of markers) {
    const distance = Math.abs(x - (minuteToX(marker.minuteIdx) - 0.5 * scaleX))
    if (distance > TICK_HIT_REACH_PX) continue
    const better =
      distance < bestDistance ||
      (distance === bestDistance && best !== null && marker.importance > best.importance)
    if (better) {
      best = marker
      bestDistance = distance
    }
  }
  return best
}

/** Očekávaný dopad zprávy na trh: 1 = long, −1 = short, 0 = neutrální/nezměřené.

Klasifikovaný směr (sentiment_dir) má přednost; bez něj rozhoduje znaménko
skóre. Nadcházející eventy směr nemají — o dopadu se před výsledkem neví nic. */
export function expectedImpact(row: ChartNewsRow): -1 | 0 | 1 {
  if (row.sentiment_dir === 1 || row.sentiment_dir === -1) return row.sentiment_dir
  const score = asNumber(row.sentiment_score)
  if (score > 0) return 1
  if (score < 0) return -1
  return 0
}

/** Významné zprávy (importance ≥ 2) — filtr markerů „Významné" (#408).

`pinnedIds` projdou vždy: zprávy prokliknutého upozornění (#1290). Upozornění
bere plánované eventy podle FF impactu, ne podle `importance` (pravidlový
klasifikátor dává „USD PPI m/m" High importance 1) — bez výjimky by proklik
posunul graf na koš bez markeru. */
export function significantOnly<T extends ChartNewsRow>(
  rows: T[],
  pinnedIds?: ReadonlySet<number>,
): T[] {
  return rows.filter((row) => (row.importance ?? 1) >= 2 || (pinnedIds?.has(row.id) ?? false))
}
