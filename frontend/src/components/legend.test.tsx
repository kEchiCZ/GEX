/** Testy legendy grafu (#346, #348): obsah, podmínky růstu/poklesu, shoda barev. */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, test, vi } from 'vitest'
import { LEGEND_SECTIONS, Legend } from './Legend'
import { LEVEL_COLORS, SETUP_COLORS } from '../heatmap/overlays'

function openLegend() {
  const onClose = vi.fn()
  render(<Legend onClose={onClose} />)
  return onClose
}

const allItems = LEGEND_SECTIONS.flatMap((section) => section.items)
const section = (title: string) => LEGEND_SECTIONS.find((s) => s.title === title)!

describe('obsah legendy', () => {
  test('pokrývá úrovně, heatmapu, značky i spodní panely', () => {
    const titles = LEGEND_SECTIONS.map((s) => s.title)
    expect(titles).toContain('Úrovně v hlavním grafu')
    expect(titles).toContain('Heatmapa')
    expect(titles).toContain('Panely pod grafem')
    expect(titles).toContain('Profil vpravo')
  })

  test('každá položka má ukázku i vysvětlení', () => {
    // Popisek bez ukázky legendu míjí — uživatel hledá čáru podle vzhledu
    for (const item of allItems) {
      expect(item.name.length).toBeGreaterThan(0)
      expect(item.what.length).toBeGreaterThan(20)
      expect(item.swatch.kind).toBeTruthy()
    }
  })

  test('GEX žebřík je popsaný — dřív v legendě chyběl', () => {
    const ladder = allItems.find((item) => item.name === 'GEX žebřík')
    expect(ladder).toBeDefined()
    expect(ladder!.up).toBeTruthy()
    expect(ladder!.down).toBeTruthy()
  })

  test('u úrovní i panelů je popsaný růst i pokles', () => {
    // Cílem legendy je, aby šlo vyčíst, kdy cena nejspíš poroste a kdy klesne
    for (const title of ['Úrovně v hlavním grafu', 'Panely pod grafem']) {
      for (const item of section(title).items) {
        if (item.name === 'Slabá zeď') continue // výslovné varování, ne směr
        expect(item.up, `${item.name} nemá popsaný růst`).toBeTruthy()
        expect(item.down, `${item.name} nemá popsaný pokles`).toBeTruthy()
      }
    }
  })

  test('GEX křivka říká, kde je a že to není Max Pain', () => {
    // Uživatel se ptal přímo na tuhle záměnu
    const curve = allItems.find((item) => item.name === 'GEX křivka')!
    expect(curve.swatch.kind).toBe('gex')
    expect(curve.where).toMatch(/profil/i)
    expect(curve.how).toMatch(/Max Pain/)
  })

  test('Sentiment vysvětluje risk-on i risk-off vůči ceně', () => {
    const sentiment = allItems.find((item) => item.name === 'Sentiment')!
    expect(sentiment.up).toMatch(/risk-on/)
    expect(sentiment.down).toMatch(/risk-off/)
  })

  test('Δ Flow říká, jaký tok měří, a nepředstírá znalost agresora', () => {
    const flow = allItems.find((item) => item.name === 'Δ Flow C/P')!
    expect(flow.what).toMatch(/delt/i)
    expect(flow.how).toMatch(/agresor/i)
  })

  test('barvy ukázek jsou sdílené s grafem, ne opsané', () => {
    const maxPain = allItems.find((item) => item.name === 'Max Pain')!
    const entry = allItems.find((item) => item.name === 'Vstup')!
    expect(maxPain.swatch).toMatchObject({ color: LEVEL_COLORS.max_pain })
    expect(entry.swatch).toMatchObject({ color: SETUP_COLORS.entry })
  })
})

describe('modál', () => {
  test('vykreslí názvy prvků, které uživatel v grafu vidí', () => {
    openLegend()
    expect(screen.getByText('Max Pain')).toBeDefined()
    expect(screen.getByText('Gamma Flip')).toBeDefined()
    expect(screen.getByText('GEX žebřík')).toBeDefined()
    expect(screen.getByText('GEX křivka')).toBeDefined()
    expect(screen.getByText('Cum Δ')).toBeDefined()
  })

  test('podmínky růstu a poklesu jsou vidět', () => {
    openLegend()
    expect(screen.getAllByText('▲ Roste').length).toBeGreaterThan(5)
    expect(screen.getAllByText('▼ Klesá').length).toBeGreaterThan(5)
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
