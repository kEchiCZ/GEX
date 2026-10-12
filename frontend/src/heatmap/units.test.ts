/** Testy jednotek Dyn ploch (#569) — parita s enginem přes golden fixture. */
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test } from 'vitest'
import {
  GEX_UNIT_LABELS,
  GREEK_DOLLAR_DELTA_LABELS,
  planeUnits,
  priceWeight,
  weightProfileRow,
} from './units'

// Fixture sdílená s enginem (tests/golden/, čte ho test_p2_weight_569.py) —
// obě implementace váhy musí dát tatáž čísla. Cwd je frontend/ (vitest)
// nebo kořen repa — vezme se první existující cesta.
const fixturePath =
  ['../engine/tests/golden/p2_weight_569.json', 'engine/tests/golden/p2_weight_569.json'] // prettier-ignore
    .map((candidate) => resolve(process.cwd(), candidate))
    .find((candidate) => existsSync(candidate))!
const fixture = JSON.parse(readFileSync(fixturePath, 'utf-8')) as {
  contract: { gamma: number; oi: number; multiplier: number }
  per_point: number
  levels: { price: number; per_percent: number }[]
}

test('priceWeight: parita s enginovou fixture — cena hladiny, ne spot (#569)', () => {
  const { gamma, oi, multiplier } = fixture.contract
  const perPoint = gamma * oi * multiplier
  expect(perPoint).toBe(fixture.per_point)
  for (const level of fixture.levels) {
    // Přesná shoda — fixture je volená tak, aby byla exaktní v float64
    expect(perPoint * priceWeight(level.price, 'per_percent')).toBe(level.per_percent)
  }
})

test('priceWeight: $/bod je identita', () => {
  expect(priceWeight(6800, 'per_point')).toBe(1)
})

test('weightProfileRow: $/1 % váží per hladina mřížky dle fixture', () => {
  // gridStart/gridStep zvolené tak, aby hladiny sedly na ceny fixture
  const row = {
    gridStart: fixture.levels[0].price,
    gridStep: fixture.levels[1].price - fixture.levels[0].price,
    values: fixture.levels.map(() => fixture.per_point),
  }
  const weighted = weightProfileRow(row, 'per_percent')
  expect(weighted.values).toEqual(fixture.levels.map((level) => level.per_percent))
})

test('weightProfileRow: $/bod vrací TENTÝŽ objekt (bitová identita + memo)', () => {
  const row = { gridStart: 100, gridStep: 5, values: [1, -2, 3] }
  expect(weightProfileRow(row, 'per_point')).toBe(row)
})

test('weightProfileRow: znaménkový vzor je invariantní — nuly se nehýbou (#569)', () => {
  // P²/100 je pro P > 0 striktně kladný násobitel: f(P)=0 ⟺ f(P)·P²/100=0.
  // Průchod nulou zůstává mezi týmiž uzly mřížky v obou jednotkách.
  const row = { gridStart: 6000, gridStep: 100, values: [5, 2, -1, -4, 3] }
  const weighted = weightProfileRow(row, 'per_percent')
  const signs = (values: number[]) => values.map((value) => Math.sign(value))
  expect(signs(weighted.values)).toEqual(signs(row.values))
})

test('popisky jednotek', () => {
  expect(GEX_UNIT_LABELS.per_point).toBe('$/bod')
  expect(GEX_UNIT_LABELS.per_percent).toBe('$/1 %')
})

// Golden #1401 sdílený s enginem (test_greek_dollar_delta_1401.py): Charm a Vanna
// v dolarové deltě = hodnota enginu × cena hladiny, ne váha P²/100
const greekFixturePath =
  ['../engine/tests/golden/greek_dollar_delta_1401.json', 'engine/tests/golden/greek_dollar_delta_1401.json'] // prettier-ignore
    .map((candidate) => resolve(process.cwd(), candidate))
    .find((candidate) => existsSync(candidate))!
const greekFixture = JSON.parse(readFileSync(greekFixturePath, 'utf-8')) as {
  levels: {
    price: number
    charm_per_point: number
    vanna_per_point: number
    charm_dollar_delta: number
    vanna_dollar_delta: number
  }[]
}

test('dolarová delta Charm a Vanna: golden #1401 — hodnota × cena hladiny', () => {
  for (const level of greekFixture.levels) {
    const weight = priceWeight(level.price, 'dollar_delta')
    expect(level.charm_per_point * weight).toBe(level.charm_dollar_delta)
    expect(level.vanna_per_point * weight).toBe(level.vanna_dollar_delta)
    // $/1 % (P²/100) by dalo P/100× víc — na ES ~79×
    expect(priceWeight(level.price, 'per_percent') / weight).toBeCloseTo(level.price / 100, 9)
  }
})

test('planeUnits: přepínač jen pro Dyn GEX, Charm a Vanna vždy dolarová delta (#1401)', () => {
  expect(planeUnits('gex', 'per_percent')).toBe('per_percent')
  expect(planeUnits('gex', 'per_point')).toBe('per_point')
  expect(planeUnits('charm', 'per_percent')).toBe('dollar_delta')
  expect(planeUnits('vanna', 'per_point')).toBe('dollar_delta')
  expect(GREEK_DOLLAR_DELTA_LABELS.charm).toBe('$ delty / den')
  expect(GREEK_DOLLAR_DELTA_LABELS.vanna).toBe('$ delty / 1 bod IV')
})
