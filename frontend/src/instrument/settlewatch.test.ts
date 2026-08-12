/** Testy settle watch (#603): výběr klíčové zdi a formát. */
import { expect, test } from 'vitest'
import { formatSettleWatch, settleWatchLevel } from './settlewatch'

const WALLS = [
  { name: 'call_wall', level: 7800, weak: false },
  { name: 'put_wall', level: 7750, weak: true },
  { name: 'call_wall_2', level: 7795, weak: false },
]

test('silné zdi mají přednost; uvnitř třídy vyhrává nejbližší k ceně', () => {
  // Slabá put zeď 7750 je nejblíž, ale silné 7795/7800 mají přednost
  const watch = settleWatchLevel(WALLS, 7760)
  expect(watch?.name).toBe('call_wall_2') // 7795 blíž než 7800
  expect(watch?.level).toBe(7795)
  expect(watch?.distance).toBeCloseTo(-35)
  expect(watch?.weak).toBe(false)
})

test('bez silných zdí se bere nejbližší z dostupných (weak/neznámá dominance)', () => {
  const soft = [
    { name: 'call_wall', level: 7800, weak: true },
    { name: 'put_wall', level: 7750, weak: null },
  ]
  const watch = settleWatchLevel(soft, 7760)
  expect(watch?.name).toBe('put_wall')
  expect(watch?.weak).toBe(true)
})

test('null bez spotu nebo bez validních úrovní', () => {
  expect(settleWatchLevel(WALLS, null)).toBeNull()
  expect(settleWatchLevel([{ name: 'x', level: Number.NaN, weak: false }], 7760)).toBeNull()
})

test('formát: strana úrovně + odstup se znaménkem (#603 příklady)', () => {
  // Cena 7,3 b POD úrovní 7800 → „nad 7800 −7,3 b"
  expect(formatSettleWatch({ name: 'call_wall', level: 7800, distance: -7.3, weak: false })).toBe('nad 7800 −7.3 b') // prettier-ignore
  // Cena 2,1 b NAD úrovní → „pod 7800 +2,1 b" (strana se překlopí, znaménko taky)
  expect(formatSettleWatch({ name: 'call_wall', level: 7800, distance: 2.1, weak: false })).toBe('pod 7800 +2.1 b') // prettier-ignore
  // Interpolovaný GEX level se zaokrouhlí (#653) — žádný float vlak v hlavičce
  expect(formatSettleWatch({ name: 'gex_level', level: 7628.166920999555, distance: -55.0, weak: false })).toBe('nad 7628.2 −55.0 b') // prettier-ignore
})
