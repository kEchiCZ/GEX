/** Polylinie kontur (#1222): napojení úseček, uzavřené ostrovy, Chaikin, kódování. */
import { expect, test } from 'vitest'
import { marchingSquares } from './contours'
import type { Segment } from './contours'
import { computeContourPolylines } from './contourCompute'
import { chaikin, decodePolylines, encodePolylines, joinSegments } from './polylines'

test('joinSegments: rozházené úsečky jedné čáry → jedna otevřená polylinie', () => {
  const segments: Segment[] = [
    [2, 0, 3, 0.5],
    [0, 0, 1, 0],
    [1, 0, 2, 0],
  ]
  const polylines = joinSegments(segments)
  expect(polylines).toHaveLength(1)
  expect(polylines[0].closed).toBe(false)
  // Orientace je věc implementace (start = koncový bod s jediným sousedem)
  const forward = [0, 0, 1, 0, 2, 0, 3, 0.5]
  const backward = [3, 0.5, 2, 0, 1, 0, 0, 0]
  expect([forward, backward]).toContainEqual(polylines[0].points)
})

test('joinSegments: uzavřený čtverec je jeden ostrov, dvě nezávislé čáry = dvě polylinie', () => {
  const square: Segment[] = [
    [0, 0, 1, 0],
    [1, 0, 1, 1],
    [1, 1, 0, 1],
    [0, 1, 0, 0],
  ]
  const island = joinSegments(square)
  expect(island).toHaveLength(1)
  expect(island[0].closed).toBe(true)
  expect(island[0].points).toHaveLength(8)
  const two = joinSegments([...square, [5, 5, 6, 5], [6, 5, 7, 5]])
  expect(two).toHaveLength(2)
})

test('chaikin: vyhlazení zachová koncové body otevřené křivky a zjemní roh', () => {
  const corner = [0, 0, 1, 0, 1, 1]
  const smooth = chaikin(corner, false, 1)
  expect(smooth.slice(0, 2)).toEqual([0, 0])
  expect(smooth.slice(-2)).toEqual([1, 1])
  // roh (1,0) už v bodech není — nahrazen body na 3/4 a 1/4 sousedních hran
  expect(smooth).not.toContain(1 - 1e-9)
  expect(smooth.length).toBeGreaterThan(corner.length)
})

test('sinusové pole → per úroveň jedna souvislá vlnící se křivka bez mezer', () => {
  const width = 120
  const height = 40
  const field = new Float32Array(width * height)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      // Vlna v čase: hodnota kladná nad vlnou, záporná pod ní
      const wave = 20 + 6 * Math.sin(x / 9)
      field[y * width + x] = (y - wave) * 10
    }
  }
  const flip = computeContourPolylines(field, width, height, 'flip')
  expect(flip).toHaveLength(1)
  expect(flip[0].closed).toBe(false)
  // Křivka jde přes celou šířku a vlní se (rozsah y ≈ amplituda vlny)
  const xs = flip[0].points.filter((_, index) => index % 2 === 0)
  const ys = flip[0].points.filter((_, index) => index % 2 === 1)
  expect(Math.min(...xs)).toBeLessThan(1)
  expect(Math.max(...xs)).toBeGreaterThan(width - 2)
  expect(Math.max(...ys) - Math.min(...ys)).toBeGreaterThan(8)
  // Hladkost: žádný ostrý zlom mezi sousedními úseky
  let worstTurn = 0
  for (let index = 4; index < flip[0].points.length; index += 2) {
    const ax = flip[0].points[index - 2] - flip[0].points[index - 4]
    const ay = flip[0].points[index - 1] - flip[0].points[index - 3]
    const bx = flip[0].points[index] - flip[0].points[index - 2]
    const by = flip[0].points[index + 1] - flip[0].points[index - 1]
    const turn = Math.abs(Math.atan2(ay, ax) - Math.atan2(by, bx))
    worstTurn = Math.max(worstTurn, Math.min(turn, 2 * Math.PI - turn))
  }
  expect(worstTurn).toBeLessThan(Math.PI / 4)
})

test('kódování polylinií pro worker je bezeztrátové', () => {
  const polylines = [
    { points: [0, 0, 1, 0.5, 2, 0.25], closed: false },
    { points: [5, 5, 6, 5, 6, 6, 5, 6], closed: true },
  ]
  const roundtrip = decodePolylines(encodePolylines(polylines))
  expect(roundtrip).toEqual(polylines)
  expect(decodePolylines(new Float32Array(0))).toEqual([])
})

/** Původní napojení přes řetězcové klíče `x|y` (před #1420) — reference shody. */
function joinSegmentsReference(segments: Segment[]): { points: number[]; closed: boolean }[] {
  const QUANT = 1000
  const keyOf = (x: number, y: number) => `${Math.round(x * QUANT)}|${Math.round(y * QUANT)}`
  if (segments.length === 0) return []
  const adjacency = new Map<string, number[]>()
  const endpoints: [string, string][] = []
  segments.forEach(([x1, y1, x2, y2], index) => {
    const a = keyOf(x1, y1)
    const b = keyOf(x2, y2)
    endpoints.push([a, b])
    for (const key of [a, b]) {
      const list = adjacency.get(key)
      if (list) list.push(index)
      else adjacency.set(key, [index])
    }
  })
  const used = new Uint8Array(segments.length)
  const polylines: { points: number[]; closed: boolean }[] = []
  const walk = (startIndex: number, startKey: string) => {
    const keys = [startKey]
    let index = startIndex
    let key = startKey
    for (;;) {
      used[index] = 1
      const [a, b] = endpoints[index]
      const next = a === key ? b : a
      keys.push(next)
      const following = (adjacency.get(next) ?? []).find((candidate) => !used[candidate])
      if (following === undefined) return { keys, closed: next === startKey }
      index = following
      key = next
    }
  }
  const emit = (keys: string[], closed: boolean) => {
    const points: number[] = []
    const last = closed ? keys.length - 1 : keys.length
    for (let index = 0; index < last; index += 1) {
      const [x, y] = keys[index].split('|').map(Number)
      points.push(x / QUANT, y / QUANT)
    }
    if (points.length >= 4) polylines.push({ points, closed })
  }
  for (const [key, list] of adjacency) {
    if (list.length !== 1) continue
    if (used[list[0]]) continue
    const { keys, closed } = walk(list[0], key)
    emit(keys, closed)
  }
  for (let index = 0; index < segments.length; index += 1) {
    if (used[index]) continue
    const { keys, closed } = walk(index, endpoints[index][0])
    emit(keys, closed)
  }
  return polylines
}

test('joinSegments s číselnými klíči = původní řetězcové klíče (#1420): NaN, −0, záporné, velký rozsah', () => {
  // Pole s NaN buňkami: marching squares dá úsečky s NaN souřadnicí
  const width = 12
  const height = 9
  const field = Float32Array.from({ length: width * height }, (_, index) =>
    index % 17 === 5 ? NaN : Math.sin(index / 3) + Math.cos(index / 7),
  )
  const fromNaNField = marchingSquares(field, width, height, 0.3)
  expect(fromNaNField.some((segment) => segment.some(Number.isNaN))).toBe(true)
  const handmade: Segment[] = [
    [-0, 0, 1, 0], // −0 a +0 jsou týž bod
    [1, 0, 1.0004, 1], // kvantování spojí 1 a 1.0004
    [1.0004, 1, -2.5, -3], // záporné souřadnice
    [NaN, 2, 3, 2],
    [3, 2, NaN, 2], // NaN|2 = týž klíč jako v řetězci
    [NaN, NaN, 4, 4],
  ]
  // Velký rozsah klíčů (až ~1e15): korekce celočíselného dělení klíče
  const wide: Segment[] = [
    [0, 0, 999_999.123, 999.5],
    [999_999.123, 999.5, 999_999.123, 0.25],
    [999_999.123, 0.25, 0, 0],
  ]
  for (const segments of [fromNaNField, handmade, wide]) {
    expect(joinSegments(segments)).toEqual(joinSegmentsReference(segments))
  }
})

test('joinSegments: nekonečná souřadnice je chyba, ne tichý výsledek', () => {
  expect(() => joinSegments([[0, 0, Infinity, 1]])).toThrow(/nekonečná/)
})
