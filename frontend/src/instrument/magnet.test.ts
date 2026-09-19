/** Magnet úrovně (#1223): put wall v negativní gammě, těžiště v pozitivní, flip na hraně. */
import { expect, test } from 'vitest'
import { magnetChipText, magnetLevel, magnetSentence } from './magnet'

const base = { spot: 7700, flip: 7650, putWall: 7600, callWall: 7725, centroid: 7655 }

test('negativní gamma: tlačí k put wall pod cenou; bez zdi pod cenou call wall nad ní', () => {
  const magnet = magnetLevel({ ...base, regime: 'negative' })
  expect(magnet).toEqual({ kind: 'push', source: 'put_wall', level: 7600, distance: -100 })
  const above = magnetLevel({ ...base, spot: 7590, regime: 'negative' })
  expect(above?.source).toBe('call_wall')
  expect(
    magnetLevel({ ...base, spot: 7590, putWall: null, callWall: null, regime: 'negative' }),
  ).toBeNull()
})

test('pozitivní gamma: lepí k těžišti, bez těžiště k flipu', () => {
  expect(magnetLevel({ ...base, regime: 'positive' })).toEqual({
    kind: 'pin',
    source: 'centroid',
    level: 7655,
    distance: -45,
  })
  expect(magnetLevel({ ...base, centroid: null, regime: 'positive' })?.source).toBe('flip')
})

test('na hraně flipu (±5 b) nebo flipzone: magnet je flip; bez spotu/režimu null', () => {
  expect(magnetLevel({ ...base, spot: 7653, regime: 'negative' })).toEqual({
    kind: 'edge',
    source: 'flip',
    level: 7650,
    distance: -3,
  })
  expect(magnetLevel({ ...base, regime: 'flipzone' })?.kind).toBe('edge')
  expect(magnetLevel({ ...base, regime: null })).toBeNull()
  expect(magnetLevel({ ...base, spot: null, regime: 'negative' })).toBeNull()
})

test('texty: chip a věta do Briefingu', () => {
  const magnet = magnetLevel({ ...base, regime: 'negative' })!
  expect(magnetChipText(magnet, '≈ za 13 h 40 m')).toBe(
    'tlačí k 7600 (put wall, −100 b) · zmizí za 13 h 40 m',
  )
  expect(magnetChipText(magnet, null)).toBe('tlačí k 7600 (put wall, −100 b)')
  expect(magnetSentence(magnet, 'negative', '≈ za 13 h 40 m')).toBe(
    'Negativní gamma tlačí cenu k 7600 (put wall, −100 b); tento tlak zmizí s expirací řetězu ≈ za 13 h 40 m.',
  )
  const pin = magnetLevel({ ...base, regime: 'positive' })!
  expect(magnetSentence(pin, 'positive', null)).toBe(
    'Pozitivní gamma lepí cenu k 7655 (těžiště GEX, −45 b); platí do expirace řetězu.',
  )
})
