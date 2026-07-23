/** Testy overlayů (issue #24): mapování, viditelnost dle checkboxů, crosshair sync. */
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { Heatmap } from '../components/Heatmap'
import { CrosshairProvider, useCrosshair } from '../state/Crosshair'
import { DEFAULT_VIEW } from './view'
import type { ViewTransform } from './view'
import { demoGrid } from './demo'
import { breaksOnJump, formatLevel, fractionalRow, isLevelJump, pairWallSeries, pricePolyline, resolveSecondaryWalls, tickIndices, visibleOverlays } from './overlays' // prettier-ignore
import type { LevelLine, OverlayData } from './overlays'

// ── Čisté helpery ──────────────────────────────────────────────────

test('tickIndices vybírá popisky s minimálním rozestupem', () => {
  // 100 položek, krok 3 px, minimálně 30 px → každá 10.
  expect(tickIndices(100, 3, 30)).toEqual([0, 10, 20, 30, 40, 50, 60, 70, 80, 90])
  // Dost místa → každá položka
  expect(tickIndices(5, 50, 30)).toEqual([0, 1, 2, 3, 4])
  expect(tickIndices(0, 10, 30)).toEqual([])
  expect(tickIndices(10, 0, 30)).toEqual([])
})

test('formatLevel: cenovka bez plovoucího šumu', () => {
  expect(formatLevel(7628.166920999555)).toBe('7628.17')
  expect(formatLevel(7600)).toBe('7600')
  expect(formatLevel(7581.5)).toBe('7581.5')
})

test('pairWallSeries: přeskakující zeď → dvě úrovňově stabilní linie (ADR-0008, #92)', () => {
  // Reálný případ 17. 7.: primární put wall alternoval 7450 ↔ 7500,
  // sekundární je vždy ta druhá — spárováno po úrovních žádná linie neskáče
  const primary = [7450, 7500, 7450, 7500, 7450]
  const secondary = [7500, 7450, 7500, 7450, 7500]
  const paired = pairWallSeries(primary, secondary)
  expect(paired.upper).toEqual([7500, 7500, 7500, 7500, 7500])
  expect(paired.lower).toEqual([7450, 7450, 7450, 7450, 7450])
  expect(paired.primaryIsUpper).toBe(false) // poslední primární (7450) leží dole

  // Minuty s jediným kandidátem PO té, co byly vidět obě zdi: hodnota jde na
  // linii s bližší poslední hodnotou — linie zůstávají úrovňově stabilní
  const mixed = pairWallSeries([7500, 7450, 7500, 7450], [7450, null, null, 7500])
  expect(mixed.upper).toEqual([7500, null, 7500, 7500])
  expect(mixed.lower).toEqual([7450, 7450, null, 7450])

  const empty = pairWallSeries([null, null], [null, null])
  expect(empty.upper).toEqual([null, null])
  expect(empty.lower).toEqual([null, null])
})

test('resolveSecondaryWalls: zapnuto páruje, vypnuto vrací dnešní chování (ADR-0008)', () => {
  const walls: LevelLine[] = [
    { name: 'put_wall', color: '#f0616d', series: [7450, 7500, 7450] },
    { name: 'put_wall_2', color: 'x', dash: [2, 3], series: [7500, 7450, 7500] },
    { name: 'call_wall', color: '#3ecf8e', series: [7650, 7650, 7650] },
    { name: 'call_wall_2', color: 'y', dash: [2, 3], series: [null, null, null] },
  ]
  const on = resolveSecondaryWalls(walls, true)
  // put pár se spáruje: primární styl na linii s poslední primární hodnotou (7450 dole)
  const primaryPut = on.find((line) => line.name === 'put_wall')!
  expect(primaryPut.series).toEqual([7450, 7450, 7450])
  const altPut = on.find((line) => line.name === 'walls:put_wall_2')!
  expect(altPut.series).toEqual([7500, 7500, 7500])
  expect(altPut.dash).toEqual([2, 3])
  // call bez sekundárních hodnot zůstává jedna linie beze změny
  expect(on.find((line) => line.name === 'call_wall')!.series).toEqual([7650, 7650, 7650])
  expect(on.some((line) => line.name === 'walls:call_wall_2')).toBe(false)

  // Vypnuto: _2 linie zmizí, primární se nemění (včetně přeskakování)
  const off = resolveSecondaryWalls(walls, false)
  expect(off.map((line) => line.name)).toEqual(['put_wall', 'call_wall'])
  expect(off[0].series).toEqual([7450, 7500, 7450])
})

test('resolveSecondaryWalls: párování zahazuje weak flagy (ADR-0010, #223)', () => {
  // Párování prohazuje hodnoty zdí po úrovních — per-minutové weak flagy by po
  // prohození patřily jiné zdi; nepárovaná linie si je naopak drží
  const walls: LevelLine[] = [
    { name: 'put_wall', color: '#f0616d', series: [7450, 7500], weak: [true, false] },
    { name: 'put_wall_2', color: 'x', dash: [2, 3], series: [7500, 7450], weak: [false, false] },
    { name: 'call_wall', color: '#3ecf8e', series: [7650, 7650], weak: [false, true], labelSuffix: ' · 34 %' }, // prettier-ignore
    { name: 'call_wall_2', color: 'y', dash: [2, 3], series: [null, null] },
  ]
  const paired = resolveSecondaryWalls(walls, true)
  expect(paired.find((line) => line.name === 'put_wall')?.weak).toBeUndefined()
  expect(paired.find((line) => line.name === 'walls:put_wall_2')?.weak).toBeUndefined()
  // call se nepáruje (sekundární bez hodnot) → weak i cenovka zůstávají
  const callWall = paired.find((line) => line.name === 'call_wall')!
  expect(callWall.weak).toEqual([false, true])
  expect(callWall.labelSuffix).toBe(' · 34 %')
})

test('isLevelJump/breaksOnJump: flip se při velkém skoku přerušuje (#197)', () => {
  // Práh 10 kroků × strike krok 5 = 50 bodů
  expect(isLevelJump(7577, 7650, 5)).toBe(true) // 73 b > 50 → mezera
  expect(isLevelJump(7577, 7600, 5)).toBe(false) // 23 b → spojené
  expect(isLevelJump(7577, 7340, 5)).toBe(true) // skok na okraj pásma
  expect(isLevelJump(7577, 7650, 0)).toBe(false) // bez kroku (1 strike) neřešíme
  expect(breaksOnJump('flip')).toBe(true)
  expect(breaksOnJump('walls:flip')).toBe(true)
  expect(breaksOnJump('call_wall')).toBe(false) // zdi řeší párování (ADR-0008)
  expect(breaksOnJump('centroid')).toBe(false)
})

test('fractionalRow interpoluje mezi strikes a ořezává okraje', () => {
  const strikes = [7590, 7595, 7600]
  expect(fractionalRow(strikes, 7595)).toBe(1)
  expect(fractionalRow(strikes, 7592.5)).toBeCloseTo(0.5)
  expect(fractionalRow(strikes, 7000)).toBe(0) // pod rozsahem
  expect(fractionalRow(strikes, 9000)).toBe(2) // nad rozsahem
  expect(fractionalRow([], 7600)).toBeNull()
})

test('pricePolyline mapuje close na řádky a nese tick směr', () => {
  const strikes = [7590, 7595, 7600]
  const points = pricePolyline(
    [
      { minuteIdx: 0, close: 7590, up: true },
      { minuteIdx: 1, close: 7597.5, up: false },
    ],
    strikes,
  )
  expect(points).toHaveLength(2)
  expect(points[0].row).toBe(0)
  expect(points[1].row).toBeCloseTo(1.5)
  expect(points[1].up).toBe(false)
})

test('visibleOverlays: přepínače odpovídají checkboxům (AC)', () => {
  const data: OverlayData = {
    price: [{ minuteIdx: 0, close: 7600, up: true }],
    sessions: [{ minuteIdx: 60, label: 'London' }],
    levels: [{ name: 'flip', color: '#fff', series: [7600] }],
    walls: [{ name: 'call_wall', color: '#fff', series: [7650] }],
    timestamp: 't',
  }

  const allOff = visibleOverlays(data, { gexLevels: false, sessions: false, dynGex: false })
  expect(allOff.sessions).toBeUndefined()
  expect(allOff.levels).toBeUndefined()
  expect(allOff.walls).toBeUndefined()
  expect(allOff.price).toBeDefined() // cenová křivka je vždy viditelná

  const levelsOnly = visibleOverlays(data, { gexLevels: true, sessions: false, dynGex: false })
  expect(levelsOnly.levels).toHaveLength(1)
  expect(levelsOnly.walls).toBeUndefined()

  const everything = visibleOverlays(data, { gexLevels: true, sessions: true, dynGex: true })
  expect(everything.sessions).toHaveLength(1)
  expect(everything.walls).toHaveLength(1)
})

// ── Crosshair sdílený napříč panely (AC: crosshair sync test) ─────

function CrosshairReader() {
  const { position } = useCrosshair()
  return (
    <output data-testid="crosshair-reader">
      {position ? `${position.minuteIdx}@${position.strike}` : 'none'}
    </output>
  )
}

test('crosshair z heatmapy se propaguje do jiného panelu přes kontext', () => {
  const grid = demoGrid(100, 10) // strikes 7400..7445
  render(
    <CrosshairProvider>
      <Heatmap grid={grid} style="gradient" contours="off" />
      <CrosshairReader />
    </CrosshairProvider>,
  )

  const reader = screen.getByTestId('crosshair-reader')
  expect(reader.textContent).toBe('none')

  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  // Canvas 1200×640, grid 100×10 → buňka 12×64 px; bod (66, 66) = minuta 5, řádek 1 shora
  fireEvent.pointerMove(overlay, { clientX: 66, clientY: 66 })
  expect(reader.textContent).toBe(`5@${grid.strikes[10 - 1 - 1]}`)

  // Opuštění plochy crosshair ruší — panely se synchronně vyčistí
  fireEvent.pointerLeave(overlay)
  expect(reader.textContent).toBe('none')
})

test('crosshair drží i mimo svíce (prázdná/budoucí plocha) — issue #109', () => {
  const grid = demoGrid(100, 10) // canvas 1200×640, krok 12 px → data končí na 1200 px
  render(
    <CrosshairProvider>
      <Heatmap grid={grid} style="gradient" contours="off" />
      <CrosshairReader />
    </CrosshairProvider>,
  )
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  // x=1300 je za posledním barem (minuta ~108, mimo 100) — crosshair nesmí zmizet
  fireEvent.pointerMove(overlay, { clientX: 1300, clientY: 66 })
  expect(screen.getByTestId('crosshair-reader').textContent).not.toBe('none')
  expect(screen.getByTestId('crosshair-reader').textContent).toMatch(/^108@/)
})

test('auto-fit jen při změně datasetu (resetKey), ne při živém růstu/resize — issue #118', () => {
  const calls: ViewTransform[] = []
  const onViewChange = (view: ViewTransform) => calls.push(view)
  const fitRange = { low: 7400, high: 7445 }
  const draw = (grid: ReturnType<typeof demoGrid>, resetKey: string) => (
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours="off"
        fitRange={fitRange}
        view={DEFAULT_VIEW}
        onViewChange={onViewChange}
        resetKey={resetKey}
      />
    </CrosshairProvider>
  )
  const { rerender } = render(draw(demoGrid(100, 10), 'ES|e|intraday|1m|d'))
  const afterInitial = calls.length
  expect(afterInitial).toBeGreaterThan(0) // úvodní fit proběhl

  // Živý přírůstek minuty (stejný dataset) → žádný nový fit, pohled se nepřepíše
  rerender(draw(demoGrid(101, 10), 'ES|e|intraday|1m|d'))
  expect(calls.length).toBe(afterInitial)

  // Změna timeframe (jiný resetKey) → refit
  rerender(draw(demoGrid(20, 10), 'ES|e|intraday|5m|d'))
  expect(calls.length).toBeGreaterThan(afterInitial)
})

test('heatmapa má reset zobrazení (tlačítko i dvojklik neshodí render)', () => {
  const grid = demoGrid(100, 10)
  render(
    <CrosshairProvider>
      <Heatmap grid={grid} style="gradient" contours="off" />
    </CrosshairProvider>,
  )
  const reset = screen.getByRole('button', { name: 'Reset zobrazení' })
  fireEvent.click(reset)
  fireEvent.doubleClick(screen.getByRole('img', { name: 'GEX heatmapa' }))
})

test('tooltip buňky zobrazuje hodnoty vrstev', () => {
  const grid = demoGrid(100, 10)
  render(
    <CrosshairProvider>
      <Heatmap grid={grid} style="gradient" contours="off" />
    </CrosshairProvider>,
  )
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  fireEvent.pointerMove(overlay, { clientX: 66, clientY: 66 })

  const tooltip = screen.getByRole('tooltip')
  expect(tooltip.textContent).toContain('min 5')
  expect(tooltip.textContent).toContain('strike')
  expect(tooltip.textContent).toContain('call')
  expect(tooltip.textContent).toContain('put')
})
