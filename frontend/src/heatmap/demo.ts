/** Syntetický demo grid + overlaye, dokud není zapojený replay/live feed (issue #27). */
import type { ProfileRow } from '../profile/bars'
import type { HeatmapGrid } from './grid'
import type { OverlayData, PriceBar } from './overlays'

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

export function demoProfile(grid: HeatmapGrid): ProfileRow[] {
  const middle = grid.strikes[Math.floor(grid.strikes.length / 2)]
  return grid.strikes.map((strike, index) => {
    const callWeight = Math.exp(-(((index - 42) / 8) ** 2))
    const putWeight = Math.exp(-(((index - 18) / 8) ** 2))
    return {
      strike,
      callVolComponent: 40 * callWeight,
      callOiComponent: 25 * callWeight,
      putVolComponent: 35 * putWeight,
      putOiComponent: 30 * putWeight,
      callVolume: Math.round(400 * callWeight),
      putVolume: Math.round(350 * putWeight),
      callOi: Math.round(2000 * callWeight),
      putOi: Math.round(2400 * putWeight),
      distanceFromSpot: strike - middle,
    }
  })
}

export function demoPanels(minutes: number): {
  vol: number[]
  optVolCall: number[]
  optVolPut: number[]
  cumDelta: number[]
} {
  const vol: number[] = []
  const optVolCall: number[] = []
  const optVolPut: number[] = []
  const cumDelta: number[] = []
  let cum = 0
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    const activity = 1 + Math.exp(-(((minuteIdx - 30) / 25) ** 2)) * 3
    vol.push(Math.round(800 * activity * (0.7 + 0.3 * Math.abs(Math.sin(minuteIdx / 9)))))
    optVolCall.push(Math.round(120 * activity * (0.6 + 0.4 * Math.abs(Math.sin(minuteIdx / 5)))))
    optVolPut.push(Math.round(110 * activity * (0.6 + 0.4 * Math.abs(Math.cos(minuteIdx / 6)))))
    cum += Math.sin(minuteIdx / 40) * 90 + Math.sin(minuteIdx / 11) * 35
    cumDelta.push(Math.round(cum))
  }
  return { vol, optVolCall, optVolPut, cumDelta }
}

export function demoOverlays(grid: HeatmapGrid): OverlayData {
  const price: PriceBar[] = []
  let previousClose = 0
  for (let minuteIdx = 0; minuteIdx < grid.minutes; minuteIdx += 1) {
    const middle = grid.strikes[Math.floor(grid.strikes.length / 2)]
    const close = middle + Math.sin(minuteIdx / 45) * 40 + Math.sin(minuteIdx / 7) * 6
    price.push({ minuteIdx, close, up: close >= previousClose })
    previousClose = close
  }
  const flip = price.map((bar) => bar.close - 12 + Math.sin(bar.minuteIdx / 60) * 5)
  const callWall = price.map((bar) => bar.close + 55)
  const putWall = price.map((bar) => bar.close - 60)
  return {
    price,
    timestamp: 'demo data',
    sessions: [
      { minuteIdx: 60, label: 'London' },
      { minuteIdx: 210, label: 'New York' },
    ],
    levels: [{ name: 'flip', color: '#e8c14b', series: flip }],
    walls: [
      { name: 'call_wall', color: '#3ecf8e', series: callWall },
      { name: 'put_wall', color: '#f0616d', series: putWall },
    ],
  }
}
