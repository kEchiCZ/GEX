/** Testy dialogu news markeru (#408): obsah, dopad Long/Short, zavírání. */
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import type { NewsRow } from '../api/news'
import type { NewsMarker } from '../heatmap/newsMarkers'
import { NewsMarkerDialog } from './NewsMarkerDialog'

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
