/** Karta Breaking news (E-6.28c): stavy, barvy, štítky a sbalený panel bez dotazů. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import type { BreakingCard, BreakingImpact } from '../api/breaking'
import { BreakingNewsList, BreakingNewsOverlay } from './BreakingNewsCard'

const subscribe = vi.fn()
const unsubscribe = vi.fn()
vi.mock('../state/AppState', () => ({
  useAppState: () => ({ socket: { subscribe, unsubscribe } }),
}))

const NOW = Date.parse('2026-10-08T14:37:30Z')

function impact(overrides: Partial<BreakingImpact>): BreakingImpact {
  return {
    state: 'fixed',
    elapsed_min: 7,
    ret_bp: 20.1,
    range_bp: 25,
    excursion_bp: 22.3,
    excursion_direction: 1,
    contaminated: false,
    ...overrides,
  }
}

const CARD: BreakingCard = {
  as_of: '2026-10-08T14:37:30+00:00',
  live: true,
  market_closed: false,
  items: [
    {
      id: 3,
      ts_event: '2026-10-08T14:33:00+00:00',
      title: 'Iran seizes tanker in Strait of Hormuz',
      is_key: false,
      group: 'geopolitics',
      theme: 'iran',
      confirmed: false,
      sources: [
        {
          source: 'rss_news',
          content_tier: 3,
          published_at: '2026-10-08T14:33:00+00:00',
          fetched_at: '2026-10-08T14:33:30+00:00',
          delay_s: 30,
        },
      ],
      impact: {
        ES: impact({ state: 'running', elapsed_min: 4, ret_bp: -5.2, excursion_direction: -1 }),
        NQ: impact({ state: 'running', elapsed_min: 4, ret_bp: -7.9, excursion_bp: null }),
      },
    },
    {
      id: 1,
      ts_event: '2026-10-08T14:30:20+00:00',
      title: "Fed's Powell says rate cuts are not imminent",
      is_key: true,
      group: 'central_banks',
      theme: 'fed',
      confirmed: true,
      sources: [
        {
          source: 'alpaca',
          content_tier: 2,
          published_at: '2026-10-08T14:30:20+00:00',
          fetched_at: '2026-10-08T14:30:21+00:00',
          delay_s: 1,
        },
      ],
      impact: {
        ES: impact({ contaminated: true }),
        NQ: impact({ state: 'no_data', ret_bp: null }),
      },
    },
  ],
}

beforeEach(() => {
  subscribe.mockClear()
  unsubscribe.mockClear()
  window.localStorage.clear()
})

test('položky nesou štítky, stav dopadu, barvu podle znaménka a zdroje', () => {
  render(<BreakingNewsList card={CARD} error={null} nowMs={NOW} />)
  const article = screen.getByTestId('breaking-3')
  expect(article.textContent).toContain('článek, zatím nepotvrzeno')
  expect(article.textContent).toContain('Geopolitika')
  expect(article.textContent).toContain('Írán')
  expect(article.textContent).toContain('běží 4 min')
  expect(article.textContent).toContain('rss_news +30 s')
  const esRunning = article.querySelector('[data-testid="breaking-impact-ES"]')
  expect(esRunning?.className).toContain('negative')
  expect(esRunning?.textContent).toContain('-5.2 bp')

  const fed = screen.getByTestId('breaking-1')
  expect(fed.textContent).toContain('zásadní')
  expect(fed.textContent).not.toContain('nepotvrzeno')
  const esFixed = fed.querySelector('[data-testid="breaking-impact-ES"]')
  expect(esFixed?.className).toContain('positive')
  expect(esFixed?.textContent).toContain('+20.1 bp')
  expect(esFixed?.textContent).toContain('↑ 22.3 bp')
  expect(esFixed?.textContent).toContain('⚠')
  const nqGap = fed.querySelector('[data-testid="breaking-impact-NQ"]')
  expect(nqGap?.textContent).toContain('bez dat')
  expect(nqGap?.textContent).not.toContain('bp ·')
})

test('zpráva při zavřeném trhu ukazuje „trh zavřený“ bez čísla', () => {
  const closed: BreakingCard = {
    ...CARD,
    items: [{ ...CARD.items[1], impact: { ES: impact({ state: 'closed', ret_bp: null }) } }],
  }
  render(<BreakingNewsList card={closed} error={null} nowMs={NOW} />)
  const cell = screen.getByTestId('breaking-impact-ES')
  expect(cell.textContent).toContain('trh zavřený')
  expect(cell.textContent).not.toContain('bp')
})

test('chyba API je vidět, prázdná karta poctivě', () => {
  const { rerender } = render(<BreakingNewsList card={null} error="HTTP 500" nowMs={NOW} />)
  expect(screen.getByTestId('breaking-error').textContent).toContain('HTTP 500')
  rerender(<BreakingNewsList card={{ ...CARD, items: [] }} error={null} nowMs={NOW} />)
  expect(screen.getByText(/žádná zpráva/)).toBeTruthy()
})

test('sbalený panel u heatmapy se na API neptá, rozbalený ano a pamatuje si to', async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(CARD) })
  vi.stubGlobal('fetch', fetchMock)
  const { unmount } = render(<BreakingNewsOverlay />)
  expect(screen.getByTestId('breaking-open')).toBeTruthy()
  expect(fetchMock).not.toHaveBeenCalled()
  expect(subscribe).not.toHaveBeenCalled()

  fireEvent.click(screen.getByTestId('breaking-open'))
  await waitFor(() => expect(screen.getByTestId('breaking-1')).toBeTruthy())
  expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/news/breaking'))
  expect(subscribe).toHaveBeenCalledWith('news', expect.any(Function))
  unmount()

  // Rozbalení přežije refresh (localStorage)
  render(<BreakingNewsOverlay />)
  expect(screen.queryByTestId('breaking-open')).toBeNull()
  fireEvent.click(screen.getByTestId('breaking-close'))
  expect(screen.getByTestId('breaking-open')).toBeTruthy()
})

test('významná zpráva z WS spustí refetch, syrová páska z enginu ne', async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(CARD) })
  vi.stubGlobal('fetch', fetchMock)
  window.localStorage.setItem('gexlens.breakingOpen', 'true')
  render(<BreakingNewsOverlay />)
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  const handler = subscribe.mock.calls[0][1] as (data: Record<string, unknown>) => void
  handler({ id: 7, title: 'syrová páska', importance: null }) // bez klasifikace
  handler({ kind: 'retro_pass', message: 'hotovo' }) // provozní hláška bez id
  await new Promise((resolve) => setTimeout(resolve, 700))
  expect(fetchMock).toHaveBeenCalledTimes(1)
  handler({ id: 8, title: 'Fed cuts rates', significance: 1 })
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
})

test('po sbalení polling skončí a stav karty se zahodí', async () => {
  vi.useFakeTimers()
  try {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(CARD) })
    vi.stubGlobal('fetch', fetchMock)
    window.localStorage.setItem('gexlens.breakingOpen', 'true')
    render(<BreakingNewsOverlay />)
    await vi.advanceTimersByTimeAsync(15_000)
    expect(fetchMock).toHaveBeenCalledTimes(2) // hned + po 15 s
    expect(screen.getByTestId('breaking-as-of').textContent).toContain('stav k')
    fireEvent.click(screen.getByTestId('breaking-close'))
    expect(unsubscribe).toHaveBeenCalledWith('news', expect.any(Function))
    await vi.advanceTimersByTimeAsync(60_000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    fireEvent.click(screen.getByTestId('breaking-open'))
    expect(screen.getByText('Načítám…')).toBeTruthy() // žádná stará čísla
  } finally {
    vi.useRealTimers()
  }
})
