/** Vysvětlení zprávy na vyžádání (#1126 3d): tlačítko → POST → text; chyba z API se ukáže. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import type { NewsRow } from '../api/news'
import { NewsRowItem } from './NewsView'

const ROW: NewsRow = {
  id: 42,
  ts_event: '2026-09-15T08:00:00Z',
  ts_ingested: '2026-09-15T08:00:05Z',
  source: 'rss_news',
  kind: 'headline',
  category: 'FED',
  importance: 3,
  title: 'Fed holds rates',
  summary: null,
  sentiment_dir: 1,
  sentiment_score: 0.4,
  sentiment_source: 'rule',
  forecast: null,
  previous: null,
  actual: null,
}

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderRow() {
  return render(
    <NewsRowItem
      row={ROW}
      review={undefined}
      nowMs={Date.parse('2026-09-15T08:05:00Z')}
      onCorrect={() => undefined}
      onTopicClick={() => undefined}
    />,
  )
}

test('kliknutí pošle POST /news/{id}/explain a ukáže text; druhé kliknutí ho sbalí', async () => {
  const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
    expect(String(url)).toContain('/news/42/explain')
    expect(init?.method).toBe('POST')
    return {
      ok: true,
      status: 200,
      json: async () => ({
        event_id: 42,
        model: 'claude-opus-5',
        text: 'Fed drží sazby.',
        cached: false,
      }),
    }
  })
  vi.stubGlobal('fetch', fetchMock)
  renderRow()
  const button = screen.getByRole('button', { name: 'Vysvětlit zprávu: Fed holds rates' })
  expect(button.textContent).toBe('Vysvětlit')
  fireEvent.click(button)
  await waitFor(() =>
    expect(screen.getByTestId('news-explain-42').textContent).toBe('Fed drží sazby.'),
  )
  expect(fetchMock).toHaveBeenCalledTimes(1)
  fireEvent.click(button)
  expect(screen.queryByTestId('news-explain-42')).toBeNull()
})

test('vypnutá funkce (503) ukáže důvod z API místo textu', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: false,
      status: 503,
      json: async () => ({ detail: 'Chybí ANTHROPIC_API_KEY v .env' }),
    })),
  )
  renderRow()
  fireEvent.click(screen.getByRole('button', { name: 'Vysvětlit zprávu: Fed holds rates' }))
  await waitFor(() =>
    expect(screen.getByTestId('news-explain-42').textContent).toBe(
      'Vysvětlení není k dispozici: Chybí ANTHROPIC_API_KEY v .env',
    ),
  )
})
