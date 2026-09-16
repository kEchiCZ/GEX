/** Chip ⌛ fáze expiračního týdne v hlavičce (#1189). */
import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { ExpiryPhaseChip } from './ExpiryPhaseChip'
import type { ExpiryCalendar } from '../api/calendar'

const CAL: ExpiryCalendar = {
  today: '2026-09-18',
  phase: 'expiry_day',
  quarterly_expiry: '2026-09-18',
  roll_date: '2026-09-10',
  soq_ts: '2026-09-18T13:30:00+00:00',
  days_to_expiry: 0,
  is_opex_week: true,
  vix_expiry: '2026-09-16',
  previous_expiry: '2026-06-19',
  markers: [],
}

test('chip ⌛ v den expirace, tooltip vysvětluje SOQ; běžný den bez chipu', () => {
  render(<ExpiryPhaseChip calendar={CAL} />)
  const chip = screen.getByTestId('expiry-phase-chip')
  expect(chip.textContent).toMatch(/^⌛ kvartální expirace U6 dnes .*\(SOQ\)$/)
  expect(chip.getAttribute('title')).toContain('SOQ v 9:30 ET')
  render(<ExpiryPhaseChip calendar={{ ...CAL, phase: 'normal' }} />)
  expect(screen.getAllByTestId('expiry-phase-chip').length).toBe(1)
})

test('bez dat (API nedostupné) se nic nekreslí', () => {
  const { container } = render(<ExpiryPhaseChip calendar={null} />)
  expect(container.textContent).toBe('')
})
