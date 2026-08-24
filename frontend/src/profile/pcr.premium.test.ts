/** #835: P/C v prémii nesmí vracet nulu nad zdravými řádky. */
import { expect, test } from 'vitest'
import { computePcr } from './pcr'
import type { ProfileRow } from './bars'

/** Řádek podle reálného tvaru dat (NQ 24. 8.): mid z bid/ask, stale jednotky sekund. */
function row(strike: number, callMid: number, putMid: number): ProfileRow {
  return {
    strike,
    callVolComponent: 0,
    callOiComponent: 0,
    putVolComponent: 0,
    putOiComponent: 0,
    callVolume: 100,
    putVolume: 100,
    callOi: 200,
    putOi: 200,
    distanceFromSpot: 0,
    staleAge: 4,
    callMid,
    putMid,
  }
}

test('prémie nad zdravými řádky nejsou nula (#835)', () => {
  const rows = [row(28900, 120, 30), row(29000, 80, 60), row(29100, 40, 110)]

  const all = computePcr(rows, 'vol_oi', 'premium', 20, 29000, undefined, 'all')
  expect(all.put).toBeGreaterThan(0)
  expect(all.call).toBeGreaterThan(0)

  // Výchozí kombinace panelu: Vol+OI · Prémie $ · Jen OTM
  const otm = computePcr(rows, 'vol_oi', 'premium', 20, 29000, undefined, 'otm')
  expect(otm.call).toBeGreaterThan(0)
  expect(otm.put).toBeGreaterThan(0)
})

test('nulový výsledek vzniká jen z chybějících mid nebo stale kotací (#835)', () => {
  const noMid = [row(28900, 0, 0), row(29100, 0, 0)]
  expect(computePcr(noMid, 'vol_oi', 'premium', 20, 29000, undefined, 'all').call).toBe(0)

  const stale = [{ ...row(28900, 120, 30), staleAge: 9999 }]
  expect(computePcr(stale, 'vol_oi', 'premium', 20, 29000, undefined, 'all').call).toBe(0)
})

test('panel rozliší chybějící prémie od nuly (#835)', () => {
  // Kotace ještě nedorazily (mid = 0) — všech N řádků, žádná použitelná cena.
  // Panel na to má reagovat hláškou, ne částkou $0: nula je měření, tohle je díra.
  const loading = [row(28900, 0, 0), row(29100, 0, 0)]
  const result = computePcr(loading, 'vol_oi', 'premium', 20, 29000, undefined, 'all')

  expect(result.put).toBe(0)
  expect(result.call).toBe(0)
  expect(result.ratio).toBeNull()
  // missingShare = 1 → panel ví, že vyloučil úplně všechno
  expect(result.missingShare).toBe(1)
})
