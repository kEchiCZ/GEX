/** Testy strike profil panelu (issue #25): geometrie, orientace, zoom, crosshair sync. */
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { StrikeProfile } from '../components/StrikeProfile'
import { CrosshairProvider, useCrosshair } from '../state/Crosshair'
import { barGeometry } from './bars'
import type { ProfileRow } from './bars'

function rows(): ProfileRow[] {
  return [
    {
      strike: 7590,
      callVolComponent: 10,
      callOiComponent: 5,
      putVolComponent: 40,
      putOiComponent: 20, // put strana je maximum: 60
      callVolume: 100,
      putVolume: 400,
      callOi: 500,
      putOi: 2000,
      distanceFromSpot: -10,
    },
    {
      strike: 7600,
      callVolComponent: 30,
      callOiComponent: 15,
      putVolComponent: 10,
      putOiComponent: 5,
      callVolume: 300,
      putVolume: 100,
      callOi: 1500,
      putOi: 500,
      distanceFromSpot: 0,
    },
  ]
}

// ── Geometrie ──────────────────────────────────────────────────────

test('barGeometry normalizuje největší stranou a zoom násobí šířky', () => {
  const base = barGeometry(rows(), 130, 1)
  const first = base.find((bar) => bar.strike === 7590)
  expect(first).toBeDefined()
  // max strana = 60 → scale 130/60; putVol 40 → 86.67
  expect(first!.putVolWidth).toBeCloseTo((40 / 60) * 130, 1)
  expect(first!.putOiWidth).toBeCloseTo((20 / 60) * 130, 1)

  const zoomed = barGeometry(rows(), 130, 2)
  const zoomedRow = zoomed.find((bar) => bar.strike === 7600)!
  expect(zoomedRow.callVolWidth).toBeCloseTo(Math.min(130, (30 / 60) * 130 * 2), 1)
  // ořez: skládaný pruh nikdy nepřeteče polovinu panelu
  const clipped = barGeometry(rows(), 130, 4).find((bar) => bar.strike === 7590)!
  expect(clipped.putVolWidth + clipped.putOiWidth).toBeLessThanOrEqual(130)
})

// ── Render a orientace (AC: rozložení a orientace dle Moodix) ─────

function renderPanel() {
  return render(
    <CrosshairProvider>
      <StrikeProfile rows={rows()} spot={7595} height={200} />
    </CrosshairProvider>,
  )
}

test('call pruhy jdou doprava od osy, put doleva; nejvyšší strike nahoře', () => {
  renderPanel()
  const half = 130

  const top = screen.getByTestId('profile-row-7600') // nejvyšší strike = první řádek
  const bottom = screen.getByTestId('profile-row-7590')
  const topY = Number(top.querySelector('[data-part="call-vol"]')!.getAttribute('y'))
  const bottomY = Number(bottom.querySelector('[data-part="call-vol"]')!.getAttribute('y'))
  expect(topY).toBeLessThan(bottomY)

  const callVol = top.querySelector('[data-part="call-vol"]')!
  expect(Number(callVol.getAttribute('x'))).toBe(half) // call začíná na ose, roste doprava
  const putVol = top.querySelector('[data-part="put-vol"]')!
  expect(Number(putVol.getAttribute('x'))).toBeLessThan(half) // put je vlevo od osy
  // skládání: OI Δ segment navazuje na Vol segment
  const callOi = top.querySelector('[data-part="call-oi"]')!
  expect(Number(callOi.getAttribute('x'))).toBeCloseTo(
    half + Number(callVol.getAttribute('width')),
    1,
  )
})

test('panel má popisky strikes u levého okraje', () => {
  renderPanel()
  const labels = screen
    .getByLabelText('Skládané pruhy strike profilu')
    .querySelectorAll('[data-part="strike-label"]')
  const texts = [...labels].map((node) => node.textContent)
  expect(texts).toContain('7600')
  expect(texts).toContain('7590')
})

test('cenová linka je vykreslená na interpolované pozici spotu', () => {
  renderPanel()
  const line = screen.getByTestId('profile-price-line')
  // spot 7595 je přesně mezi 7600 (řádek 0) a 7590 (řádek 1) → y = 100 (výška 200)
  expect(Number(line.getAttribute('y1'))).toBeCloseTo(100, 0)
})

test('zoom přepínače mění šířku pruhů', () => {
  renderPanel()
  const width = () =>
    Number(
      screen
        .getByTestId('profile-row-7600')
        .querySelector('[data-part="call-vol"]')!
        .getAttribute('width'),
    )
  const base = width()
  fireEvent.click(screen.getByRole('button', { name: '2×' }))
  expect(width()).toBeCloseTo(base * 2, 1)
})

// ── Crosshair sync + tooltip ───────────────────────────────────────

function CrosshairReader() {
  const { position } = useCrosshair()
  return <output data-testid="reader">{position ? position.strike : 'none'}</output>
}

test('hover řádku nastaví crosshair a ukáže tooltip; crosshair zvenku zvýrazní řádek', () => {
  render(
    <CrosshairProvider>
      <StrikeProfile rows={rows()} spot={7595} height={200} />
      <CrosshairReader />
    </CrosshairProvider>,
  )

  fireEvent.pointerEnter(screen.getByTestId('profile-row-7600'))
  expect(screen.getByTestId('reader').textContent).toBe('7600')

  const tooltip = screen.getByRole('tooltip')
  expect(tooltip.textContent).toContain('7600')
  expect(tooltip.textContent).toContain('OI C/P: 1500 / 500')
  expect(tooltip.textContent).toContain('Vol C/P: 300 / 100')

  expect(screen.getByTestId('profile-row-7600').getAttribute('opacity')).toBe('1')
  expect(screen.getByTestId('profile-row-7590').getAttribute('opacity')).not.toBe('1')
})
