/** Testy sekce Volatilita zpráv (#567): pásma, vrcholy, prázdný stav. */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { NewsVolSection } from './NewsVolSection'

const PAYLOAD = {
  symbol: 'ES',
  window_min: 5,
  series: [
    { date: '2024-08-05', value: 89.2, sample: 40 },
    { date: '2026-08-26', value: 12.1, sample: 55 },
    { date: '2026-08-27', value: 18.4, sample: 21 },
  ],
  bands: { min: 3.2, max: 89.2, mean: 14.5 },
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(PAYLOAD) }),
  )
})

test('ukazuje dnešní hodnotu s pásmy a jmenovité vrcholy', async () => {
  render(<NewsVolSection symbol="ES" />)
  await waitFor(() => {
    expect(screen.getByTestId('newsvol-latest').textContent).toContain('18.4 bp (2026-08-27)')
  })
  expect(screen.getByText(/průměr 14.5/)).toBeTruthy()
  // Vrchol 5. 8. 2024 (VIX spike) musí být jmenovitě vidět — ověření extrémů
  expect(screen.getByTestId('newsvol-peaks').textContent).toContain('2024-08-05 (89.2 bp)')
  expect(screen.getByTestId('newsvol-chart')).toBeTruthy()
})

test('bez dat poctivý prázdný stav', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ series: [], bands: null }),
    }),
  )
  render(<NewsVolSection symbol="ES" />)
  await waitFor(() => {
    expect(screen.getByText(/Bez dat/)).toBeTruthy()
  })
})
