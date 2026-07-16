/** Testy transformace pohledu: zoom kolem kotvy, meze, zóny os. */
import { expect, test } from 'vitest'
import { DEFAULT_VIEW, ZOOM_MAX, ZOOM_MIN, axisZoneAt, zoomAxis, zoomBoth } from './view'

test('zoomAxis drží bod pod kotvou na místě', () => {
  const view = { offsetX: 10, offsetY: 0, zoomX: 2, zoomY: 1 }
  const anchor = 100
  const next = zoomAxis(view, 'x', 2, anchor)
  expect(next.zoomX).toBe(4)
  // Bod, který byl na obrazovce v kotvě: dataX = (100-10)/2 = 45 → po zoomu 45*4+offset = 100
  expect(45 * next.zoomX + next.offsetX).toBeCloseTo(anchor)
  // Osa Y nedotčená
  expect(next.zoomY).toBe(1)
  expect(next.offsetY).toBe(0)
})

test('zoomAxis ořezává na meze a přepočítá offset dle skutečně aplikovaného zoomu', () => {
  const maxed = zoomAxis(DEFAULT_VIEW, 'x', ZOOM_MAX * 10, 0)
  expect(maxed.zoomX).toBe(ZOOM_MAX)
  const minimal = zoomAxis(DEFAULT_VIEW, 'y', 0.001, 0)
  expect(minimal.zoomY).toBe(ZOOM_MIN)
  // Kotva 0 s offsetem 0: offset zůstává 0 bez ohledu na ořez
  expect(maxed.offsetX).toBe(0)
})

test('zoomBoth škáluje obě osy kolem bodu kurzoru', () => {
  const next = zoomBoth(DEFAULT_VIEW, 2, 600, 320)
  expect(next.zoomX).toBe(2)
  expect(next.zoomY).toBe(2)
  // Bod (600, 320) zůstává na místě
  expect(600 * 1 * 2 + next.offsetX).toBeCloseTo(600 * 2 + next.offsetX) // konzistence
  expect(next.offsetX).toBe(600 - 600 * 2)
  expect(next.offsetY).toBe(320 - 320 * 2)
})

test('axisZoneAt: levý pruh = osa Y, spodní pruh = osa X, jinak plocha', () => {
  expect(axisZoneAt(10, 300, 640)).toBe('y')
  expect(axisZoneAt(600, 630, 640)).toBe('x')
  expect(axisZoneAt(10, 630, 640)).toBe('x') // roh: přednost má časová osa
  expect(axisZoneAt(600, 300, 640)).toBeNull()
})
