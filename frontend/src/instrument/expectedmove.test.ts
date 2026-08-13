/** Testy expected move (#676): výběr referenční minuty, ATM fallback, hranice. */
import { describe, expect, it } from 'vitest'
import { computeExpectedMove, emUsage, straddleAt } from './expectedmove'
import type { StraddleRow } from './expectedmove'

const OPEN_MS = Date.parse('2026-08-13T13:30:00Z')

function iso(hourUtc: number, minute = 0): string {
  return `2026-08-13T${String(hourUtc).padStart(2, '0')}:${String(minute).padStart(2, '0')}:00Z`
}

function rows(mids: Record<number, [number, number]>): StraddleRow[] {
  return Object.entries(mids).map(([strike, [call, put]]) => ({
    strike: Number(strike),
    callMid: call,
    putMid: put,
  }))
}

describe('straddleAt', () => {
  it('vezme nejbližší strike se zaplacenou C i P', () => {
    const result = straddleAt(rows({ 6400: [30, 28], 6405: [27, 30], 6410: [24, 33] }), 6406)
    expect(result).toEqual({ strike: 6405, em: 57 })
  })

  it('ATM bez kotace → další nejbližší (mid 0 = kotace chybí)', () => {
    const result = straddleAt(rows({ 6400: [30, 28], 6405: [0, 30], 6410: [24, 33] }), 6406)
    expect(result).toEqual({ strike: 6410, em: 57 })
  })

  it('žádný strike s oběma stranami → null', () => {
    expect(straddleAt(rows({ 6400: [30, 0], 6405: [0, 30] }), 6402)).toBeNull()
  })
})

describe('computeExpectedMove', () => {
  const minutesIso = [iso(13, 28), iso(13, 29), iso(13, 30), iso(13, 31)]

  it('reference = první validní minuta od US openu, EM se zamkne', () => {
    const move = computeExpectedMove({
      minutesIso,
      spotSeries: [6400, 6401, 6402, 6410],
      rowsAt: () => rows({ 6400: [30, 27] }),
      usOpenMs: OPEN_MS,
    })
    expect(move).not.toBeNull()
    expect(move?.refMinuteIdx).toBe(2)
    expect(move?.preOpen).toBe(false)
    expect(move?.anchor).toBe(6402)
    expect(move?.em).toBe(57)
    expect(move?.upper).toBe(6459)
    expect(move?.lower).toBe(6345)
  })

  it('minuta openu bez spotu → další minuta seance', () => {
    const move = computeExpectedMove({
      minutesIso,
      spotSeries: [6400, 6401, null, 6410],
      rowsAt: () => rows({ 6400: [30, 27] }),
      usOpenMs: OPEN_MS,
    })
    expect(move?.refMinuteIdx).toBe(3)
  })

  it('před openem: poslední validní overnight minuta jako průběžný odhad', () => {
    const move = computeExpectedMove({
      minutesIso: [iso(10, 0), iso(11, 0), iso(12, 0)],
      spotSeries: [6400, 6401, 6403],
      rowsAt: () => rows({ 6400: [31, 27] }),
      usOpenMs: OPEN_MS,
    })
    expect(move?.refMinuteIdx).toBe(2)
    expect(move?.preOpen).toBe(true)
  })

  it('bez validního straddlu → null', () => {
    const move = computeExpectedMove({
      minutesIso,
      spotSeries: [6400, 6401, 6402, 6403],
      rowsAt: () => [],
      usOpenMs: OPEN_MS,
    })
    expect(move).toBeNull()
  })
})

describe('emUsage', () => {
  it('poloha spotu v pásmu 0–1, mimo pásmo přetéká', () => {
    const move = {
      refMinuteIdx: 0,
      preOpen: false,
      anchor: 6400,
      atmStrike: 6400,
      em: 50,
      upper: 6450,
      lower: 6350,
    }
    expect(emUsage(move, 6400)).toBe(0.5)
    expect(emUsage(move, 6450)).toBe(1)
    expect(emUsage(move, 6475)).toBe(1.25)
  })
})
