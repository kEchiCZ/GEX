/** Testy legendy grafu (#346): obsah, otevírání a shoda barev s grafem. */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, test, vi } from 'vitest'
import { LEGEND_SECTIONS, Legend } from './Legend'
import { LEVEL_COLORS, SETUP_COLORS } from '../heatmap/overlays'

function openLegend() {
  const onClose = vi.fn()
  render(<Legend onClose={onClose} />)
  return onClose
}

describe('obsah legendy', () => {
  test('pokrývá úrovně, heatmapu, značky i spodní panely', () => {
    const titles = LEGEND_SECTIONS.map((section) => section.title)
    expect(titles).toContain('Úrovně v hlavním grafu')
    expect(titles).toContain('Heatmapa')
    expect(titles).toContain('Panely pod grafem')
    expect(titles).toContain('Profil vpravo')
  })

  test('každá položka má ukázku i vysvětlení', () => {
    // Popisek bez ukázky legendu míjí — uživatel hledá čáru podle vzhledu
    for (const section of LEGEND_SECTIONS) {
      expect(section.items.length).toBeGreaterThan(0)
      for (const item of section.items) {
        expect(item.name.length).toBeGreaterThan(0)
        expect(item.what.length).toBeGreaterThan(20)
        expect(item.swatch.kind).toBeTruthy()
      }
    }
  })

  test('u úrovní je vysvětlené i chování ceny', () => {
    // Samotný název „Gamma Flip" traderovi nepomůže
    const levels = LEGEND_SECTIONS.find((s) => s.title === 'Úrovně v hlavním grafu')
    expect(levels).toBeDefined()
    for (const item of levels!.items) {
      expect(item.how, `${item.name} nemá vysvětlené chování`).toBeTruthy()
    }
  })

  test('barvy ukázek jsou sdílené s grafem, ne opsané', () => {
    const levels = LEGEND_SECTIONS.find((s) => s.title === 'Úrovně v hlavním grafu')!
    const maxPain = levels.items.find((item) => item.name === 'Max Pain')!
    const setup = LEGEND_SECTIONS.find((s) => s.title === 'Navržený setup')!
    const entry = setup.items.find((item) => item.name === 'Vstup')!
    expect(maxPain.swatch).toMatchObject({ color: LEVEL_COLORS.max_pain })
    expect(entry.swatch).toMatchObject({ color: SETUP_COLORS.entry })
  })
})

describe('modál', () => {
  test('vykreslí názvy čar, které uživatel v grafu vidí', () => {
    openLegend()
    expect(screen.getByText('Max Pain')).toBeDefined()
    expect(screen.getByText('Gamma Flip')).toBeDefined()
    expect(screen.getByText('Call zeď')).toBeDefined()
    expect(screen.getByText('2. call zeď / 2. put zeď')).toBeDefined()
    expect(screen.getByText('Cum Δ')).toBeDefined()
  })

  test('Esc zavírá', () => {
    const onClose = openLegend()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  test('křížek zavírá', () => {
    const onClose = openLegend()
    fireEvent.click(screen.getByLabelText('Zavřít legendu'))
    expect(onClose).toHaveBeenCalled()
  })
})
