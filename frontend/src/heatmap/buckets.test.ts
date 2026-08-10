/** Testy plánu timeframe košů: wall-clock zarovnání, fallback, fáze osy (#584). */
import { expect, test } from 'vitest'
import { bucketPhaseMinutes, bucketStartMs, buildBucketPlan } from './buckets'

/** Osa `count` po sobě jdoucích minut od `startIso`. */
function axis(startIso: string, count: number): string[] {
  const start = Date.parse(startIso)
  return Array.from({ length: count }, (_, index) => new Date(start + index * 60_000).toISOString())
}

test('bucketStartMs zaokrouhluje dolů na násobek timeframu', () => {
  expect(bucketStartMs(Date.parse('2026-08-10T11:03:00Z'), 5)).toBe(
    Date.parse('2026-08-10T11:00:00Z'),
  )
  expect(bucketStartMs(Date.parse('2026-08-10T11:03:00Z'), 15)).toBe(
    Date.parse('2026-08-10T11:00:00Z'),
  )
  expect(bucketStartMs(Date.parse('2026-08-09T23:59:00Z'), 15)).toBe(
    Date.parse('2026-08-09T23:45:00Z'),
  )
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
