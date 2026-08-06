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

/** Najde anotaci nejblíž bodu (guma); vzdálenost normalizovaná tolerancemi os. */
export function nearestAnnotationId(
  annotations: StoredAnnotation[],
  point: AnnotationPoint,
  minuteTolerance: number,
  strikeTolerance: number,
): number | null {
  let bestId: number | null = null
  let bestDistance = 1 // > 1 = mimo toleranci
  for (const annotation of annotations) {
    for (const candidate of annotation.payload.points) {
      const dx = (candidate.minute - point.minute) / minuteTolerance
      const dy = (candidate.strike - point.strike) / strikeTolerance
      const distance = Math.sqrt(dx * dx + dy * dy)
      if (distance <= bestDistance) {
        bestDistance = distance
        bestId = annotation.id
      }
    }
  }
  return bestId
}
