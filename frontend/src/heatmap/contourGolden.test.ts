/** Golden kontur (#1420): výstup `computeContourPolylines` na pevných polích.

Otisk je FNV-1a přes Float64 bajty všech bodů, příznaků uzavření a délek —
změna výpočtu, která pohne jediným bitem souřadnice nebo pořadím polylinií,
ho změní. Hodnoty spočtené kódem před zrychlením (#1420); zrychlení musí dát
bit po bitu totéž. Pole jsou deterministická (LCG), tvarem jako 1m den:
souvislé kopce, plató, nuly, obě znaménka a konstantní projekce. */
import { expect, test } from 'vitest'
import { computeContourPolylines } from './contourCompute'
import type { ContoursMode } from './contours'
import type { Polyline } from './polylines'

const WIDTH = 300
const HEIGHT = 40
const DATA = 230 // od tohoto sloupce projekce = poslední naměřený sloupec

function lcg(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0
    return state / 2 ** 32
  }
}

/** Kladné pole (OI): kopce podél striků s driftem v čase, šum, plató a nuly. */
function positiveField(seed: number): Float32Array {
  const random = lcg(seed)
  const field = new Float32Array(WIDTH * HEIGHT)
  const hills = [8, 19, 31].map((center) => ({ center, amp: 0.4 + random() * 0.6 }))
  for (let y = 0; y < HEIGHT; y += 1) {
    for (let x = 0; x < DATA; x += 1) {
      let value = 0
      for (const hill of hills) {
        value += hill.amp * Math.exp(-((y - hill.center - x / 120) ** 2) / 8)
      }
      // Plató: blok stejných hodnot (shoda s prahem i mezi sousedy)
      if (x >= 60 && x < 90 && y >= 10 && y < 16) value = 0.5
      // Nuly: řádky bez dat
      if (y >= 36) value = 0
      field[y * WIDTH + x] = Math.min(1, value + random() * 0.05)
    }
    field.fill(field[y * WIDTH + DATA - 1], y * WIDTH + DATA, (y + 1) * WIDTH)
  }
  return field
}

/** Znaménkové pole (Vol ±, Dyn GEX): náhodná procházka s vlnou, obě strany. */
function signedField(seed: number): Float32Array {
  const random = lcg(seed)
  const field = new Float32Array(WIDTH * HEIGHT)
  for (let y = 0; y < HEIGHT; y += 1) {
    let walk = 0
    for (let x = 0; x < DATA; x += 1) {
      walk += (random() - 0.5) * 0.08
      const wave = Math.sin(x / 23 + y / 5) * 0.6
      field[y * WIDTH + x] = Math.max(-1, Math.min(1, wave + walk))
    }
    field.fill(field[y * WIDTH + DATA - 1], y * WIDTH + DATA, (y + 1) * WIDTH)
  }
  return field
}

function fingerprint(polylines: Polyline[]): string {
  let size = 1
  for (const polyline of polylines) size += 2 + polyline.points.length
  const values = new Float64Array(size)
  values[0] = polylines.length
  let offset = 1
  for (const polyline of polylines) {
    values[offset] = polyline.points.length
    values[offset + 1] = polyline.closed ? 1 : 0
    values.set(polyline.points, offset + 2)
    offset += 2 + polyline.points.length
  }
  let hash = 0x811c9dc5
  for (const byte of new Uint8Array(values.buffer)) {
    hash ^= byte
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  const points = polylines.reduce((sum, polyline) => sum + polyline.points.length / 2, 0)
  const closed = polylines.filter((polyline) => polyline.closed).length
  return `${polylines.length} polylinií, ${points} bodů, ${closed} uzavřených, ${hash.toString(16)}`
}

const CASES: [string, () => Float32Array, ContoursMode][] = [
  ['kladné pole, Major', () => positiveField(11), 'major'],
  ['kladné pole, All', () => positiveField(12), 'all'],
  ['znaménkové pole, Major', () => signedField(21), 'major'],
  ['znaménkové pole, All', () => signedField(22), 'all'],
  ['znaménkové pole, flip', () => signedField(23), 'flip'],
]

test.each(CASES)('golden kontur: %s', (_name, field, mode) => {
  expect(fingerprint(computeContourPolylines(field(), WIDTH, HEIGHT, mode))).toMatchSnapshot()
})
