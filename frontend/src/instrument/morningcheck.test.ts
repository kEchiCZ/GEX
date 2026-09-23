/** Ranní checklist (#1241) nad daty 21. 9. 2026 15:15 CEST (NQ, po kvartálním OPEX). */
import { expect, test } from 'vitest'
import { morningChecklist } from './morningcheck'
import type { TrendReport } from './trend'

const trendUp: TrendReport = {
  byTimeframe: [],
  higher: 'up',
  lower: 'up',
  aligned: 5,
  decided: 5,
  expected: 'up',
  reading: 'rostoucí',
}

const nq2109 = {
  prevCliffShare: 0.437,
  prevCliffOpex: true,
  mapState: null,
  trend: trendUp,
  price: 30240,
  prevClose: 30029.5,
  flip: 29834,
  callWall: 30300,
  putWall: 29875,
  callWallDom: 0.18,
  putWallDom: 0.2,
  tendencyBand: 'short',
  minutesToExpiry: 405,
}

test('21. 9. 2026 před openem: útes NQ 44 % pod prahem, slabá call zeď = průraz, gap-and-hold nad flipem', () => {
  const items = Object.fromEntries(morningChecklist(nq2109).map((item) => [item.key, item]))
  expect(items.cliff.status).toBe('calm')
  expect(items.trend.status).toBe('go')
  expect(items.trend.action).toContain('LONG')
  expect(items.wall.status).toBe('go')
  expect(items.wall.value).toBe('call 30300 (+60 b, dominance 18 %)')
  expect(items.wall.action).toContain('průraz')
  expect(items.gap.status).toBe('go')
  expect(items.gap.value).toBe('+211 b (0.70 %), nad flipem')
  expect(items.pin.status).toBe('calm')
  expect(items.pin.action).toContain('nehlasují proti trendu')
  expect(items.rules.value).toContain('50 $')
})

test('ES po OPEX: útes 83 % = den rozsahu; v posledních 90 min pin má váhu; bez dat = na', () => {
  const es = morningChecklist({
    ...nq2109,
    prevCliffShare: 0.826,
    price: 7760,
    prevClose: 7734.5,
    callWall: 7770,
    putWall: 7710,
    callWallDom: 0.4,
    putWallDom: 0.3,
    minutesToExpiry: 60,
  })
  const items = Object.fromEntries(es.map((item) => [item.key, item]))
  expect(items.cliff.status).toBe('go')
  expect(items.cliff.value).toBe('odpadlo 83 % (OPEX)')
  expect(items.wall.status).toBe('calm')
  expect(items.pin.status).toBe('watch')
  // Stav mapy (#1245): tenká = signál, se strukturou = v normálu, bez kolektoru = bez dat
  const thinState = { thin: true, thin_gamma: true, weak_walls: true, fused: false, reasons: [], gex_abs: 1, gamma_abs: 0.1, spread_pct: 0.01, version: 1 } // prettier-ignore
  const thin = Object.fromEntries(morningChecklist({ ...nq2109, mapState: thinState }).map((i) => [i.key, i])) // prettier-ignore
  expect(thin.map.status).toBe('go')
  expect(thin.map.value).toBe('tenká mapa (2/3)')
  expect(thin.map.action).toContain('nic nepinuje')
  const solid = Object.fromEntries(morningChecklist({ ...nq2109, mapState: { ...thinState, thin: false, thin_gamma: false, weak_walls: true } }).map((i) => [i.key, i])) // prettier-ignore
  expect(solid.map.status).toBe('calm')
  expect(items.map.status).toBe('na')
  const empty = Object.fromEntries(
    morningChecklist({
      prevCliffShare: null,
      prevCliffOpex: false,
      mapState: null,
      trend: null,
      price: null,
      prevClose: null,
      flip: null,
      callWall: null,
      putWall: null,
      callWallDom: null,
      putWallDom: null,
      tendencyBand: null,
      minutesToExpiry: null,
    }).map((item) => [item.key, item]),
  )
  expect([empty.cliff.status, empty.trend.status, empty.wall.status, empty.gap.status]).toEqual(['na', 'na', 'na', 'na']) // prettier-ignore
})
