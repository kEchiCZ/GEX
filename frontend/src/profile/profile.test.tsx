/** Testy strike profil panelu (issue #25): geometrie, orientace, zoom, crosshair sync. */
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { StrikeProfile } from '../components/StrikeProfile'
import { CrosshairProvider, useCrosshair } from '../state/Crosshair'
import { barGeometry, gexCurvePaths, niceCeil, volLeaders } from './bars'
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

test('volLeaders: top strany podle volume, nuly se vynechávají (#208)', () => {
  const leaders = volLeaders(rows())
  // rows(): 7590 C100/P400, 7600 (viz fixture) — seřazeno sestupně podle volume
  expect(leaders[0].volume).toBeGreaterThanOrEqual(leaders[1]?.volume ?? 0)
  expect(leaders.length).toBeLessThanOrEqual(3)
  expect(leaders[0]).toMatchObject({ strike: expect.any(Number), right: expect.any(String) })
  expect(volLeaders([])).toEqual([])
})

test('Vol leadeři readout se vykreslí v hlavičce profilu (#208)', () => {
  render(
    <CrosshairProvider>
      <StrikeProfile rows={rows()} spot={7600} />
    </CrosshairProvider>,
  )
  const readout = screen.getByTestId('vol-leaders')
  expect(readout.textContent).toContain('Vol leadeři:')
  expect(readout.textContent).toContain('7590P')
})

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

test('gexCurvePaths: kladná doprava, záporná doleva, flip interpolovaný (ADR-0009)', () => {
  const row = { gridStart: 100, gridStep: 1, values: [4, 2, -2, -4] }
  const paths = gexCurvePaths(row, (price) => price, 50, 25) // priceToY = identita
  // max |v| = 4 → x = 50 + (v/4)·25
  expect(paths.positive).toBe('M75.0,100.0L62.5,101.0')
  expect(paths.negative).toBe('M37.5,102.0L25.0,103.0')
  // Průchod nulou mezi 101 (v=2) a 102 (v=−2) → cena 101.5
  expect(paths.flipYs).toEqual([101.5])
})

test('Dyn GEX křivka v profilu: chip přepíná vrstvu (ADR-0009)', () => {
  render(
    <CrosshairProvider>
      <StrikeProfile
        rows={rows()}
        spot={7595}
        height={200}
        gexProfile={{ tsIso: 't', gridStart: 7590, gridStep: 5, values: [100, -50, 80] }}
      />
    </CrosshairProvider>,
  )
  const panel = screen.getByLabelText('Skládané pruhy strike profilu')
  expect(panel.querySelector('[data-part="gex-positive"]')).not.toBeNull()
  expect(panel.querySelector('[data-part="gex-negative"]')).not.toBeNull()
  expect(panel.querySelectorAll('[data-part="gex-flip"]').length).toBeGreaterThan(0)
  // Chip vypne vrstvu (persistuje se dle ADR-0007)
  fireEvent.click(screen.getByRole('button', { name: 'Dyn GEX profil' }))
  expect(panel.querySelector('[data-part="gex-curve"]')).toBeNull()
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

test('sdílená osa (#213): řádky se kotví k strikes heatmapy, ne k vlastnímu pořadí', () => {
  // Σ souhrn: řádky 7590/7600, ale osa grafu má 4 strikes 7585..7615 (krok 10)
  render(
    <CrosshairProvider>
      <StrikeProfile
        rows={rows()}
        spot={7595}
        height={200}
        yView={{ offsetY: 0, zoomY: 1, baseHeight: 200 }}
        axisStrikes={[7585, 7595, 7605, 7615]}
      />
    </CrosshairProvider>,
  )
  // rowHeight = 200/4 = 50; strike 7600 je na vzestupném zlomku 1.5
  // → y střed = (4−1−1.5+0.5)·50 = 100 (bez kotvení by vyšel 25)
  const callVol = screen.getByTestId('profile-row-7600').querySelector('[data-part="call-vol"]')!
  const barHeight = Number(callVol.getAttribute('height'))
  expect(Number(callVol.getAttribute('y'))).toBeCloseTo(100 - barHeight / 2, 1)
  // spot 7595 sedí přesně na strike ose: (4−1−1+0.5)·50 = 125 — shodně s heatmapou
  expect(Number(screen.getByTestId('profile-price-line').getAttribute('y1'))).toBeCloseTo(125, 1)
})

test('sdílená osa (#213): řádek mimo obálku osy se nekreslí (nevrší se na kraji)', () => {
  render(
    <CrosshairProvider>
      <StrikeProfile
        rows={rows()}
        spot={null}
        height={200}
        yView={{ offsetY: 0, zoomY: 1, baseHeight: 200 }}
        axisStrikes={[7600, 7625]} // 7590 je pod obálkou → skip
      />
    </CrosshairProvider>,
  )
  expect(screen.queryByTestId('profile-row-7590')).toBeNull()
  expect(screen.getByTestId('profile-row-7600')).toBeDefined()
})

test('cenová linka je šedá — žlutá patří flipům (#213)', () => {
  renderPanel()
  expect(screen.getByTestId('profile-price-line').getAttribute('stroke')).toBe('#d7dce6')
})

test('popisky hodnot se nikdy nepřekrývají se strike popisky ani okrajem (#181)', () => {
  renderPanel() // šířka 260, halfWidth 130, barHalf 90; max strana = put 7590 (60)
  const panel = screen.getByLabelText('Skládané pruhy strike profilu')
  const putVals = [...panel.querySelectorAll('[data-part="value-put"]')]
  // 7590: plný put pruh končí na 40 (LABEL_SPACE) — hodnota by zasáhla do strike
  // popisků, takže se překlopí DOVNITŘ pruhu (anchor start, tmavý text)
  const fullPut = putVals.find((node) => node.textContent === '60')!
  expect(fullPut.getAttribute('text-anchor')).toBe('start')
  expect(Number(fullPut.getAttribute('x'))).toBeCloseTo(43, 1)
  expect(fullPut.getAttribute('fill')).toBe('#12151c')
  // 7600: krátký put pruh (15/60) má místa dost → hodnota vně pruhu jako dřív
  const shortPut = putVals.find((node) => node.textContent === '15')!
  expect(shortPut.getAttribute('text-anchor')).toBe('end')
  const shortPutEnd = 130 - (15 / 60) * 90
  expect(Number(shortPut.getAttribute('x'))).toBeCloseTo(shortPutEnd - 3, 1)
  // Call strana se do pravého okraje vejde → vně pruhu
  const call = [...panel.querySelectorAll('[data-part="value-call"]')].find(
    (node) => node.textContent === '45',
  )!
  expect(call.getAttribute('text-anchor')).toBe('start')
})

test('Y osa profilu se roztahuje jen nad pruhem s hodnotami strikes (#181)', () => {
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
  // Kolečko i drag MIMO zónu osy (x=100) osu nehýbou
  fireEvent.wheel(svg, { deltaY: -100, clientX: 100 })
  fireEvent.pointerDown(svg, { clientX: 100, clientY: 100, pointerId: 1 })
  fireEvent.pointerMove(svg, { clientX: 100, clientY: 60, pointerId: 1 })
  fireEvent.pointerUp(svg, { pointerId: 1 })
  expect(changes).toHaveLength(0)
  // V zóně osy (x=10) fungují jako dřív
  fireEvent.wheel(svg, { deltaY: -100, clientX: 10 })
  expect(changes.at(-1)!.zoomY).toBeGreaterThan(1)
  fireEvent.pointerDown(svg, { clientX: 10, clientY: 100, pointerId: 1 })
  fireEvent.pointerMove(svg, { clientX: 10, clientY: 60, pointerId: 1 })
  fireEvent.pointerUp(svg, { pointerId: 1 })
  expect(changes.at(-1)!.zoomY).toBeGreaterThan(1)
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
