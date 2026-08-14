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
  profile: 'futures',
  trade: null,
  context: null,
  created_ts: '2026-08-14T14:31:00+00:00',
  updated_ts: null,
}

/** Prázdné podklady kontextu (#711) — jeden tvar pro všechny endpointy. */
const EMPTY_PAYLOAD = {
  journal: [],
  playbook: [],
  setups: [],
  bars: [],
  levels: [],
  tendency: [],
  days: [],
  today: null,
}

const PLAYBOOK = [
  {
    id: 1,
    key: 'wall_bounce',
    name: 'Odraz od zdi',
    profile: 'futures' as const,
    thesis: 'Hedging tlačí cenu zpět.',
    entry_conditions: '',
    invalidation: '',
    management: '',
    active: true,
    created_ts: '2026-08-14T08:00:00+00:00',
    updated_ts: null,
  },
]

function mockJournalApi(entries: JournalEntry[]) {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    if (String(url).includes('/playbook') && (!init || init.method === undefined)) {
      return Promise.resolve(new Response(JSON.stringify({ playbook: PLAYBOOK })))
    }
    if (String(url).includes('/setups')) {
      return Promise.resolve(new Response(JSON.stringify({ symbol: 'ES', setups: [] })))
    }
    if (String(url).includes('/journal') && (!init || init.method === undefined)) {
      return Promise.resolve(new Response(JSON.stringify({ journal: entries })))
    }
    if (String(url).includes('/journal') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      return Promise.resolve(
        new Response(JSON.stringify({ ...ENTRY, id: 2, ...body }), { status: 201 }),
      )
    }
    return Promise.resolve(new Response(JSON.stringify(EMPTY_PAYLOAD)))
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

test('profil se předvyplní podle symbolu a odejde se záznamem (#709)', async () => {
  mockJournalApi([])
  render(<JournalView />)
  // ES je futures symbol → profil Futures je předvybraný
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Futures' }).getAttribute('aria-pressed')).toBe(
      'true',
    ),
  )
  fireEvent.change(screen.getByLabelText('Text záznamu'), { target: { value: 'Pozoruji.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Přidat záznam' }))
  await waitFor(() => {
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    const body = JSON.parse(String(post![1]!.body)) as { profile: string }
    expect(body.profile).toBe('futures')
  })
})

test('přepnutí profilu na SMB přebije odvození ze symbolu (#709)', async () => {
  mockJournalApi([])
  render(<JournalView />)
  fireEvent.click(screen.getByRole('button', { name: 'SMB' }))
  fireEvent.change(screen.getByLabelText('Text záznamu'), { target: { value: 'Pozoruji.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Přidat záznam' }))
  await waitFor(() => {
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    const body = JSON.parse(String(post![1]!.body)) as { profile: string }
    expect(body.profile).toBe('smb')
  })
})

test('typ obchod odešle strukturovaný objekt trade (#709)', async () => {
  mockJournalApi([])
  render(<JournalView />)
  fireEvent.change(screen.getByLabelText('Typ záznamu'), { target: { value: 'obchod' } })
  // Pole obchodu se objeví až po přepnutí typu
  await waitFor(() => expect(screen.getByLabelText('Směr')).toBeTruthy())
  fireEvent.change(screen.getByLabelText('Setup z playbooku'), { target: { value: 'wall_bounce' } })
  fireEvent.change(screen.getByLabelText('Směr'), { target: { value: 'short' } })
  fireEvent.change(screen.getByLabelText('Plánovaný vstup'), { target: { value: '6810' } })
  fireEvent.change(screen.getByLabelText('Plánovaný stop'), { target: { value: '6813' } })
  fireEvent.change(screen.getByLabelText('Známka setupu'), { target: { value: 'A' } })
  fireEvent.change(screen.getByLabelText('Text záznamu'), { target: { value: 'Fade zdi.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Přidat záznam' }))
  await waitFor(() => {
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    const body = JSON.parse(String(post![1]!.body)) as {
      entry_type: string
      trade: {
        direction: string
        planned_entry: number
        planned_stop: number
        setup_grade: string
        setup_key: string
      }
    }
    expect(body.entry_type).toBe('obchod')
    expect(body.trade.direction).toBe('short')
    expect(body.trade.planned_entry).toBe(6810)
    expect(body.trade.setup_grade).toBe('A')
    expect(body.trade.setup_key).toBe('wall_bounce')
  })
})

test('obchod bez setupu se neodešle a řekne proč (#710)', async () => {
  mockJournalApi([])
  render(<JournalView />)
  fireEvent.change(screen.getByLabelText('Typ záznamu'), { target: { value: 'obchod' } })
  await waitFor(() => expect(screen.getByLabelText('Setup z playbooku')).toBeTruthy())
  fireEvent.change(screen.getByLabelText('Text záznamu'), { target: { value: 'Fade zdi.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Přidat záznam' }))
  await waitFor(() => expect(screen.getByText(/Vyber setup z playbooku/)).toBeTruthy())
  expect(fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')).toBeUndefined()
})

test('PlayBook ukáže setupy z playbooku (#710)', async () => {
  mockJournalApi([])
  render(<JournalView />)
  fireEvent.click(screen.getByRole('button', { name: 'PlayBook' }))
  await waitFor(() => expect(screen.getByText('Odraz od zdi')).toBeTruthy())
})

test('detekovaný setup u zapisované minuty nabídne převzetí plánu (#710)', async () => {
  const now = new Date().toISOString()
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    if (String(url).includes('/playbook')) {
      return Promise.resolve(new Response(JSON.stringify({ playbook: PLAYBOOK })))
    }
    if (String(url).includes('/setups')) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            symbol: 'ES',
            setups: [
              {
                id: 7,
                symbol: 'ES',
                expiry: '20260814',
                template: 'wall_bounce',
                direction: 'short',
                created_ts: now,
                entry: 6810,
                target: 6798,
                stop: 6813,
                confidence: 3,
                reason: 'zeď',
                status: 'active',
                closed_ts: null,
                outcome_r: null,
                mfe: null,
                mae: null,
                user_rating: null,
                user_note: null,
              },
            ],
          }),
        ),
      )
    }
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      return Promise.resolve(
        new Response(JSON.stringify({ ...ENTRY, id: 2, ...body }), { status: 201 }),
      )
    }
    return Promise.resolve(new Response(JSON.stringify(EMPTY_PAYLOAD)))
  })
  render(<JournalView />)
  await waitFor(() => expect(screen.getByText(/Detektor tu nabídl/)).toBeTruthy())

  fireEvent.change(screen.getByLabelText('Typ záznamu'), { target: { value: 'obchod' } })
  fireEvent.click(screen.getByRole('button', { name: 'Převzít plán' }))
  await waitFor(() =>
    expect((screen.getByLabelText('Plánovaný vstup') as HTMLInputElement).value).toBe('6810'),
  )
  expect((screen.getByLabelText('Plánovaný stop') as HTMLInputElement).value).toBe('6813')

  // Vazba na detekovaný setup odejde se záznamem — sloupec se dosud neplnil
  fireEvent.change(screen.getByLabelText('Setup z playbooku'), { target: { value: 'wall_bounce' } })
  fireEvent.change(screen.getByLabelText('Text záznamu'), { target: { value: 'Vzal jsem ho.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Přidat záznam' }))
  await waitFor(() => {
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    const body = JSON.parse(String(post![1]!.body)) as { setup_id: number }
    expect(body.setup_id).toBe(7)
  })
})

test('neuložený záznam se ohlásí, nezmizí potichu', async () => {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    if (String(url).includes('/journal') && init?.method === 'POST') {
      return Promise.resolve(new Response('{}', { status: 422 }))
    }
    return Promise.resolve(new Response(JSON.stringify(EMPTY_PAYLOAD)))
  })
  render(<JournalView />)
  fireEvent.change(screen.getByLabelText('Text záznamu'), { target: { value: 'Pozoruji.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Přidat záznam' }))
  await waitFor(() => expect(screen.getByText(/nepodařilo uložit/)).toBeTruthy())
  // Text zůstane ve formuláři, ať se nemusí psát znovu
  expect((screen.getByLabelText('Text záznamu') as HTMLTextAreaElement).value).toBe('Pozoruji.')
})

test('snímek kontextu odejde se záznamem a chybějící zdroj nedosadí nulu (#711)', async () => {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    const path = String(url)
    if (path.includes('/playbook')) {
      return Promise.resolve(new Response(JSON.stringify({ playbook: PLAYBOOK })))
    }
    if (path.includes('/levels/')) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            levels: [
              {
                ts_min: '2026-08-14T14:30:00+00:00',
                flip: 6805,
                call_wall: 6850,
                put_wall: 6750,
                centroid: 6800,
                total_gex: -50,
              },
            ],
          }),
        ),
      )
    }
    if (path.includes('/bars/')) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            bars: [
              {
                ts_min: '2026-08-14T14:30:00+00:00',
                open: 6810,
                high: 6812,
                low: 6808,
                close: 6810,
                volume: 100,
              },
            ],
          }),
        ),
      )
    }
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      return Promise.resolve(
        new Response(JSON.stringify({ ...ENTRY, id: 3, ...body }), { status: 201 }),
      )
    }
    return Promise.resolve(new Response(JSON.stringify(EMPTY_PAYLOAD)))
  })

  useAppStateMock.mockReturnValue({
    symbol: 'ES',
    selectedExpiry: '20260814',
    journalDraft: { tsRef: '2026-08-14T14:30:00+00:00' },
    setJournalDraft: vi.fn(),
  })
  render(<JournalView />)
  fireEvent.change(screen.getByLabelText('Text záznamu'), { target: { value: 'Fade zdi.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Přidat záznam' }))

  await waitFor(() => {
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(post).toBeTruthy()
    const body = JSON.parse(String(post![1]!.body)) as {
      context: {
        version: number
        flip: number | null
        spot: number | null
        dist_to_flip: number | null
        cliff_share: number | null
        tendency_band: string | null
      }
    }
    expect(body.context.version).toBe(1)
    expect(body.context.flip).toBe(6805)
    expect(body.context.spot).toBe(6810)
    expect(body.context.dist_to_flip).toBeCloseTo(5)
    // Zdroje, které nic nevrátily, zůstanou null — nikdy nula
    expect(body.context.cliff_share).toBeNull()
    expect(body.context.tendency_band).toBeNull()
  })
})
