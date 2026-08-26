/** Testy dialogu news markeru (#408): obsah, dopad Long/Short, zavírání. */
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import type { NewsRow } from '../api/news'
import type { NewsMarker } from '../heatmap/newsMarkers'
import { NewsMarkerDialog, surpriseVerdict } from './NewsMarkerDialog'

function row(overrides: Partial<NewsRow> & { id: number }): NewsRow {
  return {
    ts_event: new Date(2026, 6, 28, 14, 30).toISOString(),
    ts_ingested: new Date(2026, 6, 28, 14, 30).toISOString(),
    source: 'rss_news',
    kind: 'headline',
    category: 'FED',
    importance: 2,
    title: `Zpráva ${overrides.id}`,
    summary: null,
    sentiment_dir: null,
    sentiment_score: 0.4,
    sentiment_source: 'rule',
    forecast: null,
    previous: null,
    actual: null,
    ...overrides,
  }
}

function marker(rows: NewsRow[], upcoming = false): NewsMarker {
  return {
    minuteIdx: 0,
    count: rows.length,
    score: 0,
    importance: 2,
    glyph: '🏛',
    upcoming,
    titles: rows.map((item) => item.title),
    rows,
  }
}

test('ukazuje všechny zprávy clusteru s dopadem Long/Short/Neutrální', () => {
  const rows = [
    row({ id: 1, sentiment_score: 0.5 }),
    row({ id: 2, sentiment_dir: -1 }),
    row({ id: 3, sentiment_score: 0, sentiment_dir: null }),
  ]
  render(<NewsMarkerDialog marker={marker(rows)} onClose={() => {}} />)

  expect(screen.getByRole('dialog')).toBeDefined()
  expect(screen.getByText('Zpráva 1')).toBeDefined()
  expect(screen.getByText('Zpráva 2')).toBeDefined()
  expect(screen.getByText('Long ▲')).toBeDefined()
  expect(screen.getByText('Short ▼')).toBeDefined()
  expect(screen.getByText('Neutrální')).toBeDefined()
})

test('scheduled event ukazuje očekávání/minule/výsledek', () => {
  const rows = [row({ id: 1, kind: 'scheduled', forecast: '2.9', previous: '3.0', actual: '2.7' })]
  render(<NewsMarkerDialog marker={marker(rows)} onClose={() => {}} />)
  expect(screen.getByText(/očekávání 2\.9 · minule 3\.0 · výsledek 2\.7/)).toBeDefined()
})

test('nadcházející event nemá dopad — o výsledku se ještě nic neví', () => {
  const rows = [row({ id: 1, kind: 'scheduled', sentiment_score: 0.8 })]
  render(<NewsMarkerDialog marker={marker(rows, true)} onClose={() => {}} />)
  expect(screen.queryByText('Long ▲')).toBeNull()
  expect(screen.getByText('Nadcházející událost', { exact: false })).toBeDefined()
})

test('zavírá se Escape i klikem na pozadí', () => {
  const onClose = vi.fn()
  render(<NewsMarkerDialog marker={marker([row({ id: 1 })])} onClose={onClose} />)
  fireEvent.keyDown(window, { key: 'Escape' })
  expect(onClose).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('presentation'))
  expect(onClose).toHaveBeenCalledTimes(2)
})

test('surpriseVerdict (#462): prahy, směr jen nad 0,5σ, chybějící data', () => {
  const base = { id: 9, kind: 'scheduled' as const, title: 'USD CPI m/m' }
  // Pod prahem: dle očekávání, bez směru i když konvence směr zná
  expect(surpriseVerdict(row({ ...base, surprise_z: -0.2, surprise_direction: 1 }))).toEqual({
    text: 'dle očekávání (-0.2σ)',
    direction: null,
  })
  // Nad prahem: nižší + směr z API (CPI −1,4σ → risk-on)
  expect(surpriseVerdict(row({ ...base, surprise_z: -1.4, surprise_direction: 1 }))).toEqual({
    text: 'nižší než očekávání (-1.4σ)',
    direction: 1,
  })
  // Velké překvapení: „výrazně"
  expect(
    surpriseVerdict(row({ ...base, surprise_z: 1.8, surprise_direction: -1 }))?.text,
  ).toContain('výrazně vyšší')
  // Bez surprise_z (actual ještě nedorazil) → nic
  expect(surpriseVerdict(row({ ...base, surprise_z: null }))).toBeNull()
})

test('dialog kreslí verdikt se šipkou u vydaného scheduled eventu (#462)', () => {
  const released = row({
    id: 11,
    kind: 'scheduled',
    title: 'USD CPI m/m',
    forecast: 2.9,
    actual: 2.7,
    surprise_z: -1.4,
    surprise_direction: 1,
  })
  render(<NewsMarkerDialog marker={marker([released])} onClose={() => {}} />)
  const verdict = screen.getByTestId('scheduled-verdict')
  expect(verdict.textContent).toContain('nižší než očekávání (-1.4σ)')
  expect(verdict.textContent).toContain('risk-on ▲')
})
