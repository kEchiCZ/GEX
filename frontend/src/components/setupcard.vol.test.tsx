/** Karta setupu × vol režim (#874): stop jako % rozsahu + caution zvýraznění. */
import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { SetupCard } from './SetupCard'
import type { SetupRow } from '../api/setups'
import type { VolRegimeRow } from '../api/briefing'

const SETUP: SetupRow = {
  id: 1,
  symbol: 'ES',
  expiry: '20260826',
  template: 'failed_break',
  direction: 'long',
  created_ts: '2026-08-26T14:00:00+00:00',
  entry: 7600,
  target: 7615,
  stop: 7592, // stop 8 b
  confidence: 60,
  reason: 'test',
  status: 'active',
  closed_ts: null,
  outcome_r: null,
  mfe: null,
  mae: null,
  user_rating: null,
  user_note: null,
  mechanics_version: 5,
}

function vol(bucket: string): VolRegimeRow {
  return { session_date: '2026-08-25', session_range: 40, percentile: 0.87, bucket, sample: 252 }
}

test('normal režim: řádek s % rozsahu, bez caution', () => {
  render(
    <SetupCard
      setups={[SETUP]}
      onDismiss={() => {}}
      riskAccountUsd={5000}
      riskPct={1}
      volRegime={vol('normal')}
    />,
  )
  const line = screen.getByTestId('setup-vol')
  expect(line.textContent).toContain('stop = 20 % rozsahu')
  expect(line.textContent).toContain('režim normální (p87)')
  expect(line.className).not.toContain('caution')
})

test('elevated režim: caution zvýraznění + ⚠', () => {
  render(
    <SetupCard
      setups={[SETUP]}
      onDismiss={() => {}}
      riskAccountUsd={5000}
      riskPct={1}
      volRegime={vol('elevated')}
    />,
  )
  const line = screen.getByTestId('setup-vol')
  expect(line.className).toContain('caution')
  expect(line.textContent).toContain('⚠')
})

test('bez vol režimu se řádek nekreslí — žádný default (ADR-0028)', () => {
  render(
    <SetupCard
      setups={[SETUP]}
      onDismiss={() => {}}
      riskAccountUsd={5000}
      riskPct={1}
      volRegime={null}
    />,
  )
  expect(screen.queryByTestId('setup-vol')).toBeNull()
  // Kalkulačka pozice (#679) běží dál
  expect(screen.getByText(/riziko 50 \$/)).toBeTruthy()
})
