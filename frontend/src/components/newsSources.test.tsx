/** Zdroje zpráv v záložce News (#578): audit, přepínač enabled, editace seznamů. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { NewsSourcesSection } from './NewsSourcesSection'

const SOURCES = [
  {
    source: 'bluesky',
    tier: 'test',
    expected_daily_volume: 200,
    enabled: true,
    notes: 'Jetstream firehose',
    events_window: 42,
    events_today: 7,
    daily_avg: 6,
    significant_share: 0.25,
    last_event_ts: '2026-08-27T13:00:00+00:00',
  },
  {
    source: 'reddit_rss',
    tier: 'test',
    expected_daily_volume: 50,
    enabled: false,
    notes: null,
    events_window: 0,
    events_today: 0,
    daily_avg: 0,
    significant_share: null,
    last_event_ts: null,
  },
]

function mockApi() {
  const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
    const target = String(url)
    if (init?.method === 'PATCH') return { ok: true, json: async () => ({}) }
    if (init?.method === 'PUT') return { ok: true, json: async () => ({}) }
    if (target.includes('/news/sources')) {
      return { ok: true, json: async () => ({ days: 7, sources: SOURCES }) }
    }
    if (target.includes('/settings')) {
      return {
        ok: true,
        json: async () => ({
          settings: {
            news_bluesky_authors: ['cnbc.com', 'did:plc:x'],
            news_reddit_subreddits: ['wallstreetbets', 'stocks'],
            news_rss_extra: [],
          },
        }),
      }
    }
    return { ok: false, status: 404, json: async () => ({}) }
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
})

test('audit zdrojů: tabulka s realitou a stavem enabled', async () => {
  mockApi()
  render(<NewsSourcesSection />)
  expect(await screen.findByText('Bluesky Jetstream')).toBeDefined()
  const bluesky = screen.getByTestId('news-source-bluesky')
  expect(bluesky.textContent).toContain('7 / 6')
  expect(bluesky.textContent).toContain('25 %')
  const redditToggle = screen.getByLabelText('Zdroj Reddit RSS aktivní') as HTMLInputElement
  expect(redditToggle.checked).toBe(false)
})

test('přepnutí zdroje pošle PATCH /news/sources/{source}', async () => {
  const fetchMock = mockApi()
  render(<NewsSourcesSection />)
  fireEvent.click(await screen.findByLabelText('Zdroj Reddit RSS aktivní'))
  await waitFor(() => {
    const patch = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === 'PATCH',
    )
    expect(patch).toBeDefined()
    expect(String(patch?.[0])).toContain('/news/sources/reddit_rss')
    expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({ enabled: true })
  })
})

test('editor kurátorů: předvyplní uložený seznam, uloží PUT bez prázdných řádků', async () => {
  const fetchMock = mockApi()
  render(<NewsSourcesSection />)
  const editor = (await screen.findByLabelText('Bluesky kurátoři')) as HTMLTextAreaElement
  await waitFor(() => expect(editor.value).toBe('cnbc.com\ndid:plc:x'))
  // Uživatel smaže default a přidá vlastní DID — mazání je legální operace
  fireEvent.change(editor, { target: { value: 'did:plc:muj\n\n  bloomberg.com  \n' } })
  fireEvent.click(screen.getAllByRole('button', { name: 'Uložit' })[0])
  await waitFor(() => {
    const put = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === 'PUT',
    )
    expect(put).toBeDefined()
    expect(String(put?.[0])).toContain('/settings/news_bluesky_authors')
    expect(JSON.parse(String((put?.[1] as RequestInit).body))).toEqual({
      value: ['did:plc:muj', 'bloomberg.com'],
    })
  })
})
