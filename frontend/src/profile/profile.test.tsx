/** Testy strike profil panelu (issue #25): geometrie, orientace, zoom, crosshair sync. */
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { StrikeProfile } from '../components/StrikeProfile'
import { CrosshairProvider, useCrosshair } from '../state/Crosshair'
import { barGeometry, niceCeil } from './bars'
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

test('niceCeil zaokrouhluje na 1/2/5×10^n (absolutní škála)', () => {
  expect(niceCeil(60)).toBe(100)
  expect(niceCeil(12)).toBe(20)
  expect(niceCeil(3)).toBe(5)
  expect(niceCeil(500)).toBe(500)
  expect(niceCeil(1)).toBe(1)
  expect(niceCeil(0)).toBe(1)
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

test('yView: pruhy i cenová linka sledují Y transformaci hlavního grafu', () => {
  render(
    <CrosshairProvider>
      <StrikeProfile
        rows={rows()}
        spot={7595}
        height={200}
        yView={{ offsetY: 100, zoomY: 2, baseHeight: 200 }}
      />
    </CrosshairProvider>,
  )
  // scaleY = (200/2)·2 = 200; řádek 0 (7600) má střed 0.5·200 + 100 = 200
  const callVol = screen.getByTestId('profile-row-7600').querySelector('[data-part="call-vol"]')!
  const barHeight = Number(callVol.getAttribute('height'))
  expect(Number(callVol.getAttribute('y'))).toBeCloseTo(200 - barHeight / 2, 1)
  // spot 7595 → vzestupný řádek 0.5 → (2−1−0.5+0.5)·200 + 100 = 300 (jako heatmap rowToY)
  expect(Number(screen.getByTestId('profile-price-line').getAttribute('y1'))).toBeCloseTo(300, 1)
})

test('drag i kolečko na profilu upravují Y osu grafu (issue #116)', () => {
  const changes: Array<{ offsetY: number; zoomY: number }> = []
  render(
    <CrosshairProvider>
      <StrikeProfile
        rows={rows()}
        spot={7595}
        height={200}
        yView={{ offsetY: 0, zoomY: 1, baseHeight: 200 }}
        onYViewChange={(next) => changes.push(next)}
      />
    </CrosshairProvider>,
  )
  const svg = screen.getByLabelText('Skládané pruhy strike profilu')
  // Kolečko nahoru → zoom Y in (zoomY > 1)
  fireEvent.wheel(svg, { deltaY: -100 })
  expect(changes.at(-1)!.zoomY).toBeGreaterThan(1)
  // Tažení nahoru (deltaY < 0) → roztažení cenové osy (zoomY roste)
  changes.length = 0
  fireEvent.pointerDown(svg, { clientY: 100, pointerId: 1 })
  fireEvent.pointerMove(svg, { clientY: 60, pointerId: 1 })
  expect(changes.at(-1)!.zoomY).toBeGreaterThan(1)
  fireEvent.pointerUp(svg, { pointerId: 1 })
  // Bez tažení (jen pohyb) se Y osa nemění
  changes.length = 0
  fireEvent.pointerMove(svg, { clientY: 40, pointerId: 1 })
  expect(changes).toHaveLength(0)
})

test('bez onYViewChange profil Y osu neupravuje (legacy)', () => {
  render(
    <CrosshairProvider>
      <StrikeProfile
        rows={rows()}
        spot={7595}
        height={200}
        yView={{ offsetY: 0, zoomY: 1, baseHeight: 200 }}
      />
    </CrosshairProvider>,
  )
  const svg = screen.getByLabelText('Skládané pruhy strike profilu')
  // Nesmí spadnout ani nic nevolat — jen ověříme, že interakce projde bez chyby
  fireEvent.wheel(svg, { deltaY: -100 })
  fireEvent.pointerDown(svg, { clientY: 100, pointerId: 1 })
  fireEvent.pointerMove(svg, { clientY: 60, pointerId: 1 })
  expect(svg).toBeDefined()
})

test('přepínač Abs/Rel: absolutní škála zaokrouhlí osu (issue #120)', () => {
  renderPanel()
  const panel = () => screen.getByLabelText('Skládané pruhy strike profilu')
  const ticks = () =>
    [...panel().querySelectorAll('[data-part="amount-tick"]')].map((node) => node.textContent)
  expect(ticks()).toEqual(['60', '30', '0', '30', '60']) // Rel = max ve výřezu (60)
  fireEvent.click(screen.getByLabelText('Absolutní / relativní škála'))
  expect(ticks()).toEqual(['100', '50', '0', '50', '100']) // Abs = niceCeil(60) = 100
})

test('číselné popisky hodnot u pruhů (issue #120)', () => {
  renderPanel()
  const panel = screen.getByLabelText('Skládané pruhy strike profilu')
  const callVals = [...panel.querySelectorAll('[data-part="value-call"]')].map((n) => n.textContent)
  const putVals = [...panel.querySelectorAll('[data-part="value-put"]')].map((n) => n.textContent)
  // 7600: call 30+15=45, put 10+5=15 ; 7590: call 10+5=15, put 40+20=60
  expect(callVals).toContain('45')
  expect(putVals).toContain('60')
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

test('osa množství: hodnoty stran, Put/Call popisky a vliv zoomu', () => {
  renderPanel() // maxSide fixture = 60 (put strana 40+20)
  const panel = () => screen.getByLabelText('Skládané pruhy strike profilu')
  const ticks = () =>
    [...panel().querySelectorAll('[data-part="amount-tick"]')].map((node) => node.textContent)
  expect(ticks()).toEqual(['60', '30', '0', '30', '60'])
  expect(panel().textContent).toContain('Put')
  expect(panel().textContent).toContain('Call')
  fireEvent.click(screen.getByRole('button', { name: '2×' })) // zoom → plná šířka = polovina
  expect(ticks()).toEqual(['30', '15', '0', '15', '30'])
})

test('Σ přepínač: viditelný jen s aggregate prop, mění hlavičku a volá callback', () => {
  const onToggle = vi.fn()
  const { rerender } = render(
    <CrosshairProvider>
      <StrikeProfile
        rows={rows()}
        spot={7595}
        height={200}
        aggregate={false}
        onAggregateToggle={onToggle}
      />
    </CrosshairProvider>,
  )
  fireEvent.click(screen.getByLabelText('Souhrn přes expirace'))
  expect(onToggle).toHaveBeenCalledOnce()

  rerender(
    <CrosshairProvider>
      <StrikeProfile
        rows={rows()}
        spot={7595}
        height={200}
        aggregate={true}
        onAggregateToggle={onToggle}
      />
    </CrosshairProvider>,
  )
  expect(screen.getByText('Vol + OI Δ · Σ expirací')).toBeDefined()

  rerender(
    <CrosshairProvider>
      <StrikeProfile rows={rows()} spot={7595} height={200} aggregate={null} />
    </CrosshairProvider>,
  )
  expect(screen.queryByLabelText('Souhrn přes expirace')).toBeNull() // demo data — bez Σ
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
