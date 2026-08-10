/** Testy plánu timeframe košů: zarovnání na seanci, fallback, fáze osy (#584). */
import { expect, test } from 'vitest'
import { bucketPhaseMinutes, bucketStartMs, buildBucketPlan } from './buckets'

/** Osa `count` po sobě jdoucích minut od `startIso`. */
function axis(startIso: string, count: number): string[] {
  const start = Date.parse(startIso)
  return Array.from({ length: count }, (_, index) => new Date(start + index * 60_000).toISOString())
}

/** Hranice koše jako ISO — čitelnější očekávání než epoch ms. */
function startIso(iso: string, bucketMinutes: number): string {
  return new Date(bucketStartMs(Date.parse(iso), bucketMinutes)).toISOString()
}

test('bucketStartMs zaokrouhluje dolů na násobek timeframu', () => {
  expect(startIso('2026-08-10T11:03:00Z', 5)).toBe('2026-08-10T11:00:00.000Z')
  expect(startIso('2026-08-10T11:03:00Z', 15)).toBe('2026-08-10T11:00:00.000Z')
  expect(startIso('2026-08-09T23:59:00Z', 15)).toBe('2026-08-09T23:45:00.000Z')
  // Timeframy dělící hodinu vyjdou na celé minuty/hodiny — kotva seance je celá hodina
  expect(startIso('2026-08-10T11:03:00Z', 30)).toBe('2026-08-10T11:00:00.000Z')
  expect(startIso('2026-08-10T11:03:00Z', 60)).toBe('2026-08-10T11:00:00.000Z')
})

test('bucketStartMs: 45m/3h/4h kotví na otevření seance 17:00 CT, ne na půlnoc UTC (#584)', () => {
  // Léto (CDT): seance otevřela 2026-08-09 17:00 CT = 22:00Z
  // 4h koše 22:00Z / 02:00 / 06:00 / 10:00 / 14:00 / 18:00 = 18:00 ET, 22:00 ET, …
  expect(startIso('2026-08-10T11:03:00Z', 240)).toBe('2026-08-10T10:00:00.000Z')
  expect(startIso('2026-08-10T21:30:00Z', 240)).toBe('2026-08-10T18:00:00.000Z')
  // 3h koše 22:00Z / 01:00 / 04:00 / 07:00 / 10:00 = 18:00 ET, 21:00 ET, 00:00 ET, …
  expect(startIso('2026-08-10T11:03:00Z', 180)).toBe('2026-08-10T10:00:00.000Z')
  expect(startIso('2026-08-10T09:59:00Z', 180)).toBe('2026-08-10T07:00:00.000Z')
  // 45m: 17. krok od 18:00 ET → 06:45 ET = 10:45Z
  expect(startIso('2026-08-10T11:03:00Z', 45)).toBe('2026-08-10T10:45:00.000Z')
})

test('bucketStartMs: nová seance kotvu resetuje — koš na hranici seance končí', () => {
  // 22:00Z je otevření další seance: 4h koš nezačíná 18:00Z + 4 h, ale znovu na open
  expect(startIso('2026-08-10T21:59:00Z', 240)).toBe('2026-08-10T18:00:00.000Z')
  expect(startIso('2026-08-10T22:00:00Z', 240)).toBe('2026-08-10T22:00:00.000Z')
  expect(startIso('2026-08-10T23:30:00Z', 240)).toBe('2026-08-10T22:00:00.000Z')
})

test('bucketStartMs: v zimním čase drží stejnou hodinu burzy (DST, #511)', () => {
  // Zima (CST): seance otevřela 2026-01-14 17:00 CT = 23:00Z → 4h koše 23:00Z / 03:00 / …
  // 11:00Z = 06:00 ET, tedy stejná burzovní hodina jako v letním testu výše
  expect(startIso('2026-01-15T12:03:00Z', 240)).toBe('2026-01-15T11:00:00.000Z')
  expect(startIso('2026-01-15T12:03:00Z', 45)).toBe('2026-01-15T11:45:00.000Z')
  // A 15m koš je i tak na celé čtvrthodině
  expect(startIso('2026-01-15T12:03:00Z', 15)).toBe('2026-01-15T12:00:00.000Z')
})

test('buildBucketPlan: koše začínají na hranici timeframu, ne na začátku osy', () => {
  // Osa 23:59 … 00:05 (7 minut) → koše 23:55 (jen 23:59), 00:00 (00:00–00:04) a 00:05
  const plan = buildBucketPlan(axis('2026-08-09T23:59:00Z', 7), 7, 5)
  expect(plan.buckets).toBe(3)
  expect(Array.from(plan.bucketOf)).toEqual([0, 1, 1, 1, 1, 1, 2])
  expect(Array.from(plan.starts)).toEqual([0, 1, 6])
  expect(Array.from(plan.ends)).toEqual([0, 5, 6])
  expect(plan.startMs).not.toBeNull()
  expect(new Date(plan.startMs![0]).toISOString()).toBe('2026-08-09T23:55:00.000Z')
  expect(new Date(plan.startMs![1]).toISOString()).toBe('2026-08-10T00:00:00.000Z')
  expect(new Date(plan.startMs![2]).toISOString()).toBe('2026-08-10T00:05:00.000Z')
})

test('buildBucketPlan: zarovnaná osa dá plné koše (shodně s indexovým dělením)', () => {
  const plan = buildBucketPlan(axis('2026-08-10T00:00:00Z', 10), 10, 5)
  expect(plan.buckets).toBe(2)
  expect(Array.from(plan.bucketOf)).toEqual([0, 0, 0, 0, 0, 1, 1, 1, 1, 1])
})

test('buildBucketPlan: díra v ose nezakládá prázdný koš', () => {
  // 11:00–11:02, pak výpadek, pak 11:20–11:21 → koše 11:00 a 11:20, nic mezi
  const minutesIso = [...axis('2026-08-10T11:00:00Z', 3), ...axis('2026-08-10T11:20:00Z', 2)]
  const plan = buildBucketPlan(minutesIso, minutesIso.length, 5)
  expect(plan.buckets).toBe(2)
  expect(Array.from(plan.bucketOf)).toEqual([0, 0, 0, 1, 1])
  expect(new Date(plan.startMs![1]).toISOString()).toBe('2026-08-10T11:20:00.000Z')
})

test('buildBucketPlan: osa bez ISO nebo nečitelná spadne na indexové koše', () => {
  // Demo den / Daily pohled: `minutesIso` je prázdné
  const fallback = buildBucketPlan([], 5, 2)
  expect(fallback.startMs).toBeNull()
  expect(Array.from(fallback.bucketOf)).toEqual([0, 0, 1, 1, 2])
  // Nečitelná i nerostoucí osa: radši indexy než tichem zamíchané minuty
  expect(buildBucketPlan(['nesmysl', 'taky ne'], 2, 2).startMs).toBeNull()
  const unsorted = ['2026-08-10T11:05:00Z', '2026-08-10T11:00:00Z']
  expect(buildBucketPlan(unsorted, 2, 2).startMs).toBeNull()
})

test('bucketPhaseMinutes: posun začátku osy proti hranici jejího koše', () => {
  expect(bucketPhaseMinutes(axis('2026-08-09T23:59:00Z', 3), 5)).toBe(4)
  expect(bucketPhaseMinutes(axis('2026-08-09T23:59:00Z', 3), 15)).toBe(14)
  expect(bucketPhaseMinutes(axis('2026-08-10T00:00:00Z', 3), 5)).toBe(0)
  expect(bucketPhaseMinutes([], 5)).toBe(0) // bez osy (demo, Daily)
  expect(bucketPhaseMinutes(axis('2026-08-09T23:59:00Z', 3), 1)).toBe(0) // 1m koš == minuta
})
