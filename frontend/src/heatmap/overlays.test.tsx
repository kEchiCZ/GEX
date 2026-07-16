/** Testy overlayů (issue #24): mapování, viditelnost dle checkboxů, crosshair sync. */
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { Heatmap } from '../components/Heatmap'
import { CrosshairProvider, useCrosshair } from '../state/Crosshair'
import { demoGrid } from './demo'
import { fractionalRow, pricePolyline, tickIndices, visibleOverlays } from './overlays'
import type { OverlayData } from './overlays'

// ── Čisté helpery ──────────────────────────────────────────────────

test('tickIndices vybírá popisky s minimálním rozestupem', () => {
  // 100 položek, krok 3 px, minimálně 30 px → každá 10.
  expect(tickIndices(100, 3, 30)).toEqual([0, 10, 20, 30, 40, 50, 60, 70, 80, 90])
  // Dost místa → každá položka
  expect(tickIndices(5, 50, 30)).toEqual([0, 1, 2, 3, 4])
  expect(tickIndices(0, 10, 30)).toEqual([])
  expect(tickIndices(10, 0, 30)).toEqual([])
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
