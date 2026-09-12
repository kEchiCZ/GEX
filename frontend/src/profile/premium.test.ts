/** Prémie $ ve strike profilu (#1126 bod 3c): přepočet komponent, vyloučení
zmrzlého/chybějícího midu, multiplikátor, sdílené pravidlo s P/C panelem. */
import { expect, test } from 'vitest'
import type { ProfileRow } from './bars'
import { computePcr } from './pcr'
import { premiumRows, usableMid } from './premium'

function row(overrides: Partial<ProfileRow> & { strike: number }): ProfileRow {
  return {
    callVolComponent: 1,
    callOiComponent: 1,
    putVolComponent: 1,
    putOiComponent: 1,
    callVolume: 100,
    putVolume: 200,
    callOi: 1000,
    putOi: 2000,
    distanceFromSpot: 0,
    staleAge: 4,
    callMid: 10,
    putMid: 5,
    ...overrides,
  }
}

test('premiumRows: složky = počet × mid × multiplikátor per strana', () => {
  const result = premiumRows([row({ strike: 7600 })], 50)
  const [converted] = result.rows
  expect(converted.callVolComponent).toBe(100 * 10 * 50)
  expect(converted.callOiComponent).toBe(1000 * 10 * 50)
  expect(converted.putVolComponent).toBe(200 * 5 * 50)
  expect(converted.putOiComponent).toBe(2000 * 5 * 50)
  // Surové počty a mid zůstávají (tooltip, P/C panel)
  expect(converted.callVolume).toBe(100)
  expect(converted.putOi).toBe(2000)
  expect(result.available).toBe(true)
  expect(result.missingShare).toBe(0)
})

test('premiumRows: multiplikátor škáluje obě strany lineárně', () => {
  const [es] = premiumRows([row({ strike: 7600 })], 50).rows
  const [nq] = premiumRows([row({ strike: 7600 })], 20).rows
  expect(nq.callVolComponent * 2.5).toBeCloseTo(es.callVolComponent)
  expect(nq.putOiComponent * 2.5).toBeCloseTo(es.putOiComponent)
})

test('premiumRows: zmrzlá kotace a chybějící mid stranu vylučují (nula + podíl)', () => {
  const stale = row({ strike: 7500, staleAge: 9999 })
  const noPutMid = row({ strike: 7600, putMid: 0 })
  const undefinedMid = row({ strike: 7700, callMid: undefined, putMid: undefined })
  const result = premiumRows([stale, noPutMid, undefinedMid], 50)
  const [staleRow, noPutRow, undefRow] = result.rows
  expect(staleRow.callVolComponent + staleRow.callOiComponent).toBe(0)
  expect(staleRow.putVolComponent + staleRow.putOiComponent).toBe(0)
  expect(noPutRow.callVolComponent).toBe(100 * 10 * 50)
  expect(noPutRow.putVolComponent + noPutRow.putOiComponent).toBe(0)
  expect(undefRow.callVolComponent + undefRow.putOiComponent).toBe(0)
  // Vyloučeno: stale (3300) + put noPutMid (2200) + undefined (3300) z 3 × 3300
  expect(result.missingShare).toBeCloseTo((3300 + 2200 + 3300) / 9900)
  expect(result.available).toBe(true)
})

test('premiumRows: bez jediného midu → available=false (volající spadne na kontrakty)', () => {
  const rows = [row({ strike: 7500, callMid: 0, putMid: 0 }), row({ strike: 7600, staleAge: 9999 })]
  const result = premiumRows(rows, 50)
  expect(result.available).toBe(false)
  expect(result.missingShare).toBe(1)
  // Řádek jen z archivu (#849) mid nemá z principu — do podílu nevstupuje
  const archive = premiumRows([row({ strike: 7400, callMid: 0, putMid: 0, archiveOnly: true })], 50)
  expect(archive.available).toBe(false)
  expect(archive.missingShare).toBe(0)
})

test('usableMid: pravidlo sdílené s computePcr — součet složek = P/C prémie Vol + OI · Vše', () => {
  const rows = [
    row({ strike: 7500 }),
    row({ strike: 7600, putMid: 0 }),
    row({ strike: 7700, staleAge: 9999 }),
    row({ strike: 7800, callMid: 3, putMid: 7 }),
  ]
  expect(usableMid(rows[1], 'put')).toBeNull()
  expect(usableMid(rows[2], 'call')).toBeNull()
  expect(usableMid(rows[3], 'put')).toBe(7)
  const pcr = computePcr(rows, 'vol_oi', 'premium', 50, null, undefined, 'all')
  const profile = premiumRows(rows, 50)
  const call = profile.rows.reduce((sum, r) => sum + r.callVolComponent + r.callOiComponent, 0)
  const put = profile.rows.reduce((sum, r) => sum + r.putVolComponent + r.putOiComponent, 0)
  expect(call).toBeCloseTo(pcr.call)
  expect(put).toBeCloseTo(pcr.put)
  expect(profile.missingShare).toBeCloseTo(pcr.missingShare)
})
