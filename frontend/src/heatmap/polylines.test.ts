/** Polylinie kontur (#1222): napojení úseček, uzavřené ostrovy, Chaikin, kódování. */
import { expect, test } from 'vitest'
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
