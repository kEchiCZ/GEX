/** Model anotací (SPEC 7.4): body vázané na souřadnice čas × strike, ne pixely. */

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
