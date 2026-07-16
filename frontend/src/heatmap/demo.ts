/** Syntetický demo grid, dokud není zapojený replay/live feed (issue #27). */
import type { HeatmapGrid } from './grid'

export function demoGrid(minutes = 390, strikeCount = 60): HeatmapGrid {
  const strikes = Array.from({ length: strikeCount }, (_, index) => 7400 + index * 5)
  const size = minutes * strikeCount
  const call = new Float32Array(size)
  const put = new Float32Array(size)
  const staleAge = new Float32Array(size)

  for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
    for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
      const index = strikeIdx * minutes + minuteIdx
      const drift = Math.sin(minuteIdx / 45) * 8
      const callCenter = 42 + drift
      const putCenter = 18 + drift
      const spread = 6
      call[index] = Math.exp(-((strikeIdx - callCenter) ** 2) / (2 * spread ** 2))
      put[index] = 0.9 * Math.exp(-((strikeIdx - putCenter) ** 2) / (2 * spread ** 2))
      staleAge[index] = minuteIdx > minutes - 15 && strikeIdx % 7 === 0 ? 600 : 0
    }
  }
  return { minutes, strikes, layers: { call, put }, staleAge }
}
