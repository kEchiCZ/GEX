/** Model anotací (SPEC 7.4): body vázané na souřadnice čas × strike, ne pixely.

Čas bodu je SKUTEČNÁ absolutní minuta dne (offset od první minuty osy),
ne index osy (#502): osa X může nést díry (výpadek sběru) a backfill po
rekonciliaci vkládá sloupce doprostřed (#460) — index-based kotva by se
tím posunula. Převod minuta ↔ index osy jde přes `minuteAxisOffsets`.
*/

export type AnnotationTool = 'arrow' | 'line' | 'freehand'
export type ActiveTool = AnnotationTool | 'eraser' | null

export interface AnnotationPoint {
  /** Minuta dne (float — freehand vede mezi buňkami). */
  minute: number
  /** Hodnota striku (float — interpolovaně mezi listovanými strikes). */
  strike: number
}

export interface AnnotationPayload {
  tool: AnnotationTool
  color: string
  points: AnnotationPoint[]
}

export interface StoredAnnotation {
  id: number
  payload: AnnotationPayload
}

/** Ofsety minut 1m osy od jejího začátku (index sloupce → minuta dne).

`null` = osa není k dispozici nebo je nečitelná → převod je identita
(index == minuta). To drží dosavadní chování demo dat a Daily pohledu
(jednotka = sloupec-den). */
export function minuteAxisOffsets(minutesIso: string[]): number[] | null {
  if (minutesIso.length === 0) return null
  const start = Date.parse(minutesIso[0])
  if (Number.isNaN(start)) return null
  const offsets: number[] = []
  for (const iso of minutesIso) {
    const ts = Date.parse(iso)
    if (Number.isNaN(ts)) return null
    offsets.push((ts - start) / 60_000)
  }
  return offsets
}

/** Spojitý index 1m osy → absolutní minuta dne.

Uvnitř osy lineární interpolace mezi sousedy (přes díru se zlomek sloupce
mapuje proporčně na chybějící minuty); za okraji extrapolace 1 min/index —
projekční zóna za koncem dat je souvislá. */
export function minuteFromAxisIndex(offsets: number[], index: number): number {
  const last = offsets.length - 1
  if (index <= 0) return offsets[0] + index
  if (index >= last) return offsets[last] + (index - last)
  const low = Math.floor(index)
  const fraction = index - low
  return offsets[low] + fraction * (offsets[low + 1] - offsets[low])
}

/** Absolutní minuta dne → spojitý index 1m osy (inverze `minuteFromAxisIndex`). */
export function axisIndexFromMinute(offsets: number[], minute: number): number {
  const last = offsets.length - 1
  if (minute <= offsets[0]) return minute - offsets[0]
  if (minute >= offsets[last]) return last + (minute - offsets[last])
  // Binární hledání intervalu [low, low+1] s offsets[low] <= minute
  let low = 0
  let high = last
  while (high - low > 1) {
    const mid = (low + high) >> 1
    if (offsets[mid] <= minute) low = mid
    else high = mid
  }
  const span = offsets[low + 1] - offsets[low]
  return low + (span > 0 ? (minute - offsets[low]) / span : 0)
}

/** Vzdálenost bodu od úsečky `a`–`b` (vše v normalizovaných souřadnicích). */
function segmentDistance(
  point: { x: number; y: number },
  a: { x: number; y: number },
  b: { x: number; y: number },
): number {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const lengthSq = dx * dx + dy * dy
  if (lengthSq === 0) return Math.hypot(point.x - a.x, point.y - a.y)
  // Průmět bodu na úsečku, ořezaný na její rozsah (konce, ne přímka)
  const t = Math.min(1, Math.max(0, ((point.x - a.x) * dx + (point.y - a.y) * dy) / lengthSq))
  return Math.hypot(point.x - (a.x + t * dx), point.y - (a.y + t * dy))
}

/** Najde anotaci nejblíž bodu (guma #28, uchopení pro přesun #589).

Měří se vzdálenost od ÚSEČEK mezi po sobě jdoucími body, ne jen od samotných bodů (#594):
uprostřed dlouhé linie se jinak nedalo chytit nic, i když se tam kreslí. Souřadnice se
normalizují tolerancemi os, takže 1 = hranice tolerance a obě osy váží stejně. */
export function nearestAnnotationId(
  annotations: StoredAnnotation[],
  point: AnnotationPoint,
  minuteTolerance: number,
  strikeTolerance: number,
): number | null {
  const normalize = (item: AnnotationPoint) => ({
    x: item.minute / minuteTolerance,
    y: item.strike / strikeTolerance,
  })
  const target = normalize(point)
  let bestId: number | null = null
  let bestDistance = 1 // > 1 = mimo toleranci
  for (const annotation of annotations) {
    const points = annotation.payload.points
    if (points.length === 0) continue
    for (let index = 0; index < points.length; index += 1) {
      // Jednobodová anotace nemá úsečku — měří se vzdálenost od bodu
      const from = normalize(points[index])
      const to = normalize(points[Math.min(index + 1, points.length - 1)])
      const distance = segmentDistance(target, from, to)
      if (distance <= bestDistance) {
        bestDistance = distance
        bestId = annotation.id
      }
    }
  }
  return bestId
}
