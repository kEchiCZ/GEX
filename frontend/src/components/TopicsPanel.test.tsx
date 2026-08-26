/** Testy panelu Témata (#566): rozpad období, rozklik na zprávy, prázdný stav. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { TopicsPanel } from './TopicsPanel'

const SERIES_PAYLOAD = {
  topics: [
    {
      category: 'GEOPOLITICS',
      events: 6,
      weight: 3.2,
      share: 0.64,
      points: [
        { ts: '2026-08-25T10:00:00+00:00', value: -0.1 },
        { ts: '2026-08-25T11:00:00+00:00', value: -0.4 },
      ],
    },
    {
      category: 'FED',
      events: 3,
      weight: 1.8,
      share: 0.36,
      points: [
        { ts: '2026-08-25T10:00:00+00:00', value: 0.2 },
        { ts: '2026-08-25T11:00:00+00:00', value: 0.3 },
      ],
    },
  ],
}

const EVENTS_PAYLOAD = {
  events: [
    {
      id: 7,
      ts_event: '2026-08-25T09:12:00+00:00',
      title: 'Eskalace v úžině',
      source: 'rss_news',
      importance: 3,
      sentiment_dir: -1,
      sentiment_score: -0.6,
    },
  ],
}

function mockFetch(seriesPayload: unknown = SERIES_PAYLOAD) {
  return vi.fn().mockImplementation((url: string) => {
    const body = url.includes('/events') ? EVENTS_PAYLOAD : seriesPayload
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) })
  })
}

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch())
})

test('rozpad témat: pořadí podle podílu, procenta a počty zpráv', async () => {
  render(<TopicsPanel />)
  const geo = await screen.findByTestId('topic-row-GEOPOLITICS')
  expect(geo.textContent).toContain('64 %')
  expect(geo.textContent).toContain('6 zpráv')
  expect(screen.getByTestId('topic-row-FED').textContent).toContain('36 %')
})

test('rozklik tématu načte zprávy, které ho tvoří (fáze 3)', async () => {
  render(<TopicsPanel />)
  fireEvent.click(await screen.findByTestId('topic-row-GEOPOLITICS'))
  await waitFor(() => {
    expect(screen.getByTestId('topic-events-GEOPOLITICS').textContent).toContain('Eskalace v úžině')
  })
  expect(screen.getByTestId('topic-events-GEOPOLITICS').textContent).toContain('-0.60')
})

test('přepnutí období volá API s jiným rozsahem', async () => {
  const fetchMock = mockFetch()
  vi.stubGlobal('fetch', fetchMock)
  render(<TopicsPanel />)
  await screen.findByTestId('topic-row-FED')
  fireEvent.click(screen.getByText('Den'))
  await waitFor(() => {
    const urls = fetchMock.mock.calls.map((call) => String(call[0]))
    expect(urls.some((url) => url.includes('days=1'))).toBe(true)
  })
})

test('bez dat panel poctivě řekne, že není z čeho počítat', async () => {
  vi.stubGlobal('fetch', mockFetch({ topics: [] }))
  render(<TopicsPanel />)
  expect(await screen.findByText(/není z čeho spočítat/)).toBeTruthy()
})

test('focus z karty zprávy otevře téma i zdrojové zprávy (#656 bod 2)', async () => {
  render(<TopicsPanel focus={{ category: 'FED' }} />)
  await screen.findByTestId('topic-row-FED')
  await waitFor(() => {
    expect(screen.getByTestId('topic-events-FED')).toBeTruthy()
  })
})
