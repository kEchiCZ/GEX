/** Testovací úsek osy grafu (#1303) — koše po minutách od začátku. */
import type { AxisSegment } from '../heatmap/newsMarkers'

/** Úsek osy: koše po `bucketMin` minutách od `startIso`, seance [open, close). */
export function segmentOf(
  startIso: string,
  count: number,
  bucketMin = 1,
  bounds?: { openIso: string; closeIso: string },
): AxisSegment {
  const start = Date.parse(startIso)
  const bucketMs = bucketMin * 60_000
  return {
    openMs: bounds ? Date.parse(bounds.openIso) : start,
    closeMs: bounds ? Date.parse(bounds.closeIso) : start + count * bucketMs,
    startsMs: Float64Array.from({ length: count }, (_, i) => start + i * bucketMs),
    bucketMs,
    firstIdx: 0,
  }
}
