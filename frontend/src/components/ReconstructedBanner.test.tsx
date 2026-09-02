/** Banner rekonstrukce (#617) jde zavřít a pamatuje si to (#977). */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test } from 'vitest'
import { ReconstructedBanner } from './ReconstructedBanner'

const ISO = ['2026-08-31T22:00:00.000Z', '2026-08-31T22:01:00.000Z', '2026-08-31T23:58:00.000Z']

beforeEach(() => {
  window.localStorage.clear()
})
afterEach(() => {
  cleanup()
})

test('bez rekonstruovaných minut se nic neukáže', () => {
  render(<ReconstructedBanner symbol="ES" date="2026-09-01" reconstructedIso={[]} />)
  expect(screen.queryByTestId('reconstructed-banner')).toBeNull()
})

test('křížek banner zavře a po remountu (refresh) zůstane zavřený', () => {
  const { unmount } = render(
    <ReconstructedBanner symbol="ES" date="2026-09-01" reconstructedIso={ISO} />,
  )
  expect(screen.getByTestId('reconstructed-banner').textContent).toContain('Rekonstruováno 3 min')

  fireEvent.click(screen.getByRole('button', { name: /zavřít/i }))
  expect(screen.queryByTestId('reconstructed-banner')).toBeNull()

  // Refresh stránky = nový mount, persistence v localStorage (ADR-0007)
  unmount()
  render(<ReconstructedBanner symbol="ES" date="2026-09-01" reconstructedIso={ISO} />)
  expect(screen.queryByTestId('reconstructed-banner')).toBeNull()
})

test('jiná množina doplněných minut banner znovu ukáže', () => {
  // Zavřená informace nesmí schovat NOVOU rekonstrukci — další doplněné
  // minuty, jiný den nebo jiný symbol jsou jiný otisk
  render(<ReconstructedBanner symbol="ES" date="2026-09-01" reconstructedIso={ISO} />)
  fireEvent.click(screen.getByRole('button', { name: /zavřít/i }))
  expect(screen.queryByTestId('reconstructed-banner')).toBeNull()
  cleanup()

  render(
    <ReconstructedBanner
      symbol="ES"
      date="2026-09-01"
      reconstructedIso={[...ISO, '2026-09-01T00:30:00.000Z']}
    />,
  )
  expect(screen.getByTestId('reconstructed-banner')).toBeDefined()
  cleanup()

  render(<ReconstructedBanner symbol="ES" date="2026-09-02" reconstructedIso={ISO} />)
  expect(screen.getByTestId('reconstructed-banner')).toBeDefined()
})

test('rozbitá uložená hodnota nezavře nic', () => {
  window.localStorage.setItem('gexlens.reconstructedBannerDismissed', JSON.stringify({ x: 1 }))
  render(<ReconstructedBanner symbol="ES" date="2026-09-01" reconstructedIso={ISO} />)
  expect(screen.getByTestId('reconstructed-banner')).toBeDefined()
})
