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
  // Stavový mock: PUT mění settings, další GET je vrací — jako skutečné API.
  // Bez toho by reload po uložení vrátil editor do původního stavu.
  const settings: Record<string, unknown> = {
    news_bluesky_authors: ['cnbc.com', 'did:plc:x'],
    news_reddit_subreddits: ['wallstreetbets', 'stocks'],
    news_rss_extra: [],
  }
  const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
    const target = String(url)
    if (init?.method === 'PATCH') return { ok: true, json: async () => ({}) }
    if (init?.method === 'PUT') {
      const key = target.slice(target.lastIndexOf('/') + 1)
      settings[key] = (JSON.parse(String(init.body)) as { value: unknown }).value
      return { ok: true, json: async () => ({}) }
    }
    if (target.includes('/news/sources')) {
      return { ok: true, json: async () => ({ days: 7, sources: SOURCES }) }
    }
    if (target.includes('/settings')) {
      return { ok: true, json: async () => ({ settings: { ...settings } }) }
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

function lastPutBody(fetchMock: ReturnType<typeof mockApi>): unknown {
  const puts = fetchMock.mock.calls.filter(
    ([, init]) => (init as RequestInit | undefined)?.method === 'PUT',
  )
  const last = puts[puts.length - 1]
  return last ? JSON.parse(String((last[1] as RequestInit).body)) : undefined
}

test('editor kurátorů: vypnutí položky je vratné (prefix #), smazání položku odebere', async () => {
  const fetchMock = mockApi()
  render(<NewsSourcesSection />)
  // Uložený seznam se předvyplní jako položky s checkboxy (#cnbc.com by byl vypnutý)
  const toggle = (await screen.findByLabelText(
    'Bluesky kurátoři: cnbc.com aktivní',
  )) as HTMLInputElement
  expect(toggle.checked).toBe(true)
  // Vypnutí (#918): položka zůstává v seznamu, uloží se s prefixem #
  fireEvent.click(toggle)
  await waitFor(() => {
    expect(lastPutBody(fetchMock)).toEqual({ value: ['#cnbc.com', 'did:plc:x'] })
  })
  // Zpětné zapnutí prefix zase sundá — vypnutí je vratné, na rozdíl od smazání
  fireEvent.click(screen.getByLabelText('Bluesky kurátoři: cnbc.com aktivní'))
  await waitFor(() => {
    expect(lastPutBody(fetchMock)).toEqual({ value: ['cnbc.com', 'did:plc:x'] })
  })
  // Smazání ✕ položku odebere úplně
  fireEvent.click(screen.getByRole('button', { name: 'Smazat did:plc:x' }))
  await waitFor(() => {
    expect(lastPutBody(fetchMock)).toEqual({ value: ['cnbc.com'] })
  })
})

test('editor kurátorů: přidání nové položky přes input (Enter i tlačítko)', async () => {
  const fetchMock = mockApi()
  render(<NewsSourcesSection />)
  const input = await screen.findByLabelText('Bluesky kurátoři: nová položka')
  fireEvent.change(input, { target: { value: '  bloomberg.com  ' } })
  fireEvent.keyDown(input, { key: 'Enter' })
  await waitFor(() => {
    expect(lastPutBody(fetchMock)).toEqual({
      value: ['cnbc.com', 'did:plc:x', 'bloomberg.com'],
    })
  })
})
