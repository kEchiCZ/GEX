/** Testy walls módů — zrcadlí engine tests/test_walls.py na malých ručních datech. */
import { expect, test } from 'vitest'
import {
  WALLS_MODES,
  centerSeries,
  dominantRidgeTrack,
  localMaxima,
  peakSeries,
  ridgeTracks,
  smoothSeries,
} from './wallsModes'

const STRIKES = [10, 20, 30]

/** 2 minuty × 3 strikes; index = strikeIdx * minutes + minuteIdx. */
function layer(columns: number[][]): Float32Array {
  const minutes = columns.length
  const result = new Float32Array(minutes * STRIKES.length)
  columns.forEach((column, t) => {
    column.forEach((value, strikeIdx) => {
      result[strikeIdx * minutes + t] = value
    })
  })
  return result
}

test('peak: argmax per minuta, nulový sloupec → null', () => {
  const data = layer([
    [1, 5, 2],
    [0, 0, 0],
  ])
  expect(peakSeries(data, 2, STRIKES)).toEqual([20, null])
})

test('center: vážené těžiště |hodnot|', () => {
  const data = layer([[1, 1, 2]])
  // (10·1 + 20·1 + 30·2) / 4 = 22.5
  expect(centerSeries(data, 1, STRIKES)![0]).toBeCloseTo(22.5, 5)
})

test('smooth: EMA drží stav přes null mezery (span 15 → α = 0.125)', () => {
  const result = smoothSeries([10, null, 20])
  expect(result[0]).toBe(10)
  expect(result[1]).toBe(10)
  expect(result[2]).toBeCloseTo(0.125 * 20 + 0.875 * 10, 5)
})

test('localMaxima: prominence filtr zahazuje šum', () => {
  const strikes = [10, 20, 30, 40, 50]
  expect(localMaxima([0, 5, 0, 4, 0], strikes)).toEqual([20, 40])
  // Vrchol s prominencí < 10 % globálního maxima se zahazuje
  expect(localMaxima([0, 100, 99.5, 99.8, 0], strikes, 0.1)).toEqual([20])
  expect(localMaxima([0, 0, 0], STRIKES)).toEqual([])
})

test('ridge: maxima spojená nejbližším strikem do souběžných hřebenů', () => {
  const strikes = [10, 20, 30, 40, 50]
  const minutes = 2
  const data = new Float32Array(minutes * strikes.length)
  const set = (t: number, strikeIdx: number, value: number) => {
    data[strikeIdx * minutes + t] = value
  }
  // t0: vrcholy na 20 a 40; t1: vrcholy na 30 a 50
  set(0, 1, 5)
  set(0, 3, 4)
  set(1, 2, 5)
  set(1, 4, 4)

  const tracks = ridgeTracks(data, minutes, strikes)
  expect(tracks).toHaveLength(2)
  expect(tracks[0].map((point) => point.strike)).toEqual([20, 30])
  expect(tracks[1].map((point) => point.strike)).toEqual([40, 50])
})

/** Syntetický profil pro dominantní hřeben (#238): 3 minuty × 5 strikes. */
function ridgeFixture(): { data: Float32Array; strikes: number[]; minutes: number } {
  const strikes = [10, 20, 30, 40, 50]
  const minutes = 3
  const data = new Float32Array(minutes * strikes.length)
  const set = (t: number, strikeIdx: number, value: number) => {
    data[strikeIdx * minutes + t] = value
  }
  // Hřeben A na 20: střední síla, drží všechny 3 minuty (kumulativně 12)
  set(0, 1, 4)
  set(1, 1, 4)
  set(2, 1, 4)
  // Hřeben B na 40: špičkový záblesk, jen 2 minuty (kumulativně 10)
  set(0, 3, 5)
  set(1, 3, 5)
  return { data, strikes, minutes }
}

test('ridge dominantní: vyhrává track s největší kumulovanou hodnotou, ne špička', () => {
  const { data, strikes, minutes } = ridgeFixture()
  const tracks = ridgeTracks(data, minutes, strikes)
  expect(tracks).toHaveLength(2)
  const dominant = dominantRidgeTrack(data, minutes, strikes, tracks)
  expect(dominant?.map((point) => point.strike)).toEqual([20, 20, 20])
})

test('ridge dominantní: při shodě vyhrává hřeben blíž aktuální ceně', () => {
  const strikes = [10, 20, 30, 40, 50]
  const minutes = 2
  const data = new Float32Array(minutes * strikes.length)
  const set = (t: number, strikeIdx: number, value: number) => {
    data[strikeIdx * minutes + t] = value
  }
  // Dva rovnocenné hřebeny (20 a 40), kumulativně 6 oba
  set(0, 1, 3)
  set(1, 1, 3)
  set(0, 3, 3)
  set(1, 3, 3)
  const tracks = ridgeTracks(data, minutes, strikes)
  expect(tracks).toHaveLength(2)
  expect(dominantRidgeTrack(data, minutes, strikes, tracks, 45)?.[0].strike).toBe(40)
  expect(dominantRidgeTrack(data, minutes, strikes, tracks, 12)?.[0].strike).toBe(20)
  // Bez referenční ceny první v pořadí vzniku
  expect(dominantRidgeTrack(data, minutes, strikes, tracks)?.[0].strike).toBe(20)
  expect(dominantRidgeTrack(data, minutes, strikes, [])).toBeNull()
})

test('walls módy: uložená hodnota `ridge` zůstává platná (= Ridge vše)', () => {
  const values = WALLS_MODES.map((item) => item.value)
  expect(values).toContain('ridge')
  expect(values).toContain('ridge_dominant')
  expect(WALLS_MODES.find((item) => item.value === 'ridge')?.label).toBe('Ridge vše')
})
