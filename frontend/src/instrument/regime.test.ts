/** Testy GEX režimu (#209): poloha spotu vůči flip zóně + dynamický flip z profilu. */
import { expect, test } from 'vitest'
import { gexRegime, profileZeroNearest, REGIME_LABELS } from './regime'

test('gexRegime: nad zónou pozitivní, pod negativní, uvnitř flip zóna', () => {
  // Zóna = interval mezi měřeným (7510) a dynamickým (7520) flipem
  expect(gexRegime(7530, 7510, 7520)).toBe('positive')
  expect(gexRegime(7500, 7510, 7520)).toBe('negative')
  expect(gexRegime(7515, 7510, 7520)).toBe('flipzone')
  // Hrany patří do zóny (kap. 18: uvnitř pásma neobchodovat)
  expect(gexRegime(7510, 7510, 7520)).toBe('flipzone')
  expect(gexRegime(7520, 7510, 7520)).toBe('flipzone')
})

test('gexRegime: jediný flip = bodová zóna; bez vstupů null', () => {
  expect(gexRegime(7530, 7510, null)).toBe('positive')
  expect(gexRegime(7500, null, 7510)).toBe('negative')
  expect(gexRegime(7510, 7510, null)).toBe('flipzone')
  expect(gexRegime(null, 7510, 7520)).toBeNull()
  expect(gexRegime(7530, null, null)).toBeNull()
})

test('profileZeroNearest: interpolovaný průchod nulou nejblíž spotu', () => {
  // Průchody: mezi 7500→7510 (+50 → −50 ⇒ nula na 7505) a 7520→7530 (−100 → +150 ⇒ 7524)
  const row = { gridStart: 7500, gridStep: 10, values: [50, -50, -100, 150] }
  expect(profileZeroNearest(row, 7504)).toBeCloseTo(7505)
  expect(profileZeroNearest(row, 7529)).toBeCloseTo(7524)
  // Bez změny znaménka žádný flip
  expect(profileZeroNearest({ gridStart: 7500, gridStep: 10, values: [10, 20, 30] }, 7510)).toBeNull() // prettier-ignore
})

test('REGIME_LABELS pokrývá všechny stavy', () => {
  expect(Object.keys(REGIME_LABELS).sort()).toEqual(['flipzone', 'negative', 'positive'])
})
