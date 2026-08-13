/** Deník (#673 fáze A): formulář, denní pár, timeline, export dat. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { JournalView } from './JournalView'
import { journalToMarkdown } from '../api/journal'
import type { JournalEntry } from '../api/journal'

const useAppStateMock = vi.fn()
vi.mock('../state/AppState', () => ({
  useAppState: () => useAppStateMock(),
}))

const fetchMock = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  fetchMock.mockReset()
  useAppStateMock.mockReturnValue({
    symbol: 'ES',
    journalDraft: null,
    setJournalDraft: vi.fn(),
  })
})

const ENTRY: JournalEntry = {
  id: 1,
  ts_ref: '2026-08-14T14:30:00+00:00',
  symbol: 'ES',
  entry_type: 'pozorovani',
  text: 'Cena respektuje flip.',
  tags: ['flip'],
  setup_id: null,
  news_event_id: null,
  created_ts: '2026-08-14T14:31:00+00:00',
  updated_ts: null,
}

function mockJournalApi(entries: JournalEntry[]) {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    if (String(url).includes('/journal') && (!init || init.method === undefined)) {
      return Promise.resolve(new Response(JSON.stringify({ journal: entries })))
    }
    if (String(url).includes('/journal') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      return Promise.resolve(
        new Response(JSON.stringify({ ...ENTRY, id: 2, ...body }), { status: 201 }),
      )
    }
    return Promise.resolve(new Response('{}'))
  })
}

test('timeline zobrazí záznamy a prázdný stav radí rychlý vstup', async () => {
  mockJournalApi([ENTRY])
  render(<JournalView />)
  await waitFor(() => expect(screen.getByText('Cena respektuje flip.')).toBeTruthy())
  expect(screen.getByText('#flip')).toBeTruthy()
})

test('denní pár: Ranní plán odešle retro_dne s tagem plan', async () => {
  mockJournalApi([])
  render(<JournalView />)
  fireEvent.change(screen.getByLabelText('Text záznamu'), {
    target: { value: 'Čekám fade den, flip 7780.' },
  })
  fireEvent.click(screen.getByRole('button', { name: '☀ Ranní plán' }))
  await waitFor(() => {
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(post).toBeTruthy()
    const body = JSON.parse(String(post![1]!.body)) as { entry_type: string; tags: string[] }
    expect(body.entry_type).toBe('retro_dne')
    expect(body.tags).toContain('plan')
  })
})

test('journalToMarkdown seskupí dny a nese typ i tagy', () => {
  const md = journalToMarkdown([ENTRY])
  expect(md).toContain('## 2026-08-14')
  expect(md).toContain('Pozorování')
  expect(md).toContain('#flip')
  expect(md).toContain('Cena respektuje flip.')
})
