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
  daily: null,
  missed_reason: null,
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

test('strukturovaný ranní plán se zamkne a odejde jako daily (#712)', async () => {
  mockJournalApi([])
  render(<JournalView />)
  fireEvent.change(screen.getByLabelText('Typ záznamu'), { target: { value: 'retro_dne' } })
  await waitFor(() => expect(screen.getByLabelText('Podmínka 1')).toBeTruthy())

  fireEvent.change(screen.getByLabelText('Podmínka 1'), { target: { value: 'nad flipem' } })
  fireEvent.change(screen.getByLabelText('Akce 1'), { target: { value: 'long, risk 3 b' } })
  fireEvent.change(screen.getByLabelText('Procesní cíl'), { target: { value: 'max 3 obchody' } })
  fireEvent.click(screen.getByRole('button', { name: '☀ Ranní plán' }))

  await waitFor(() => {
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(post).toBeTruthy()
    const body = JSON.parse(String(post![1]!.body)) as {
      entry_type: string
      tags: string[]
      text: string
      daily: { plan: { locked_ts: string | null; scenarios: unknown[]; process_goal: string } }
    }
    expect(body.entry_type).toBe('retro_dne')
    expect(body.tags).toContain('plan')
    // Zamčení je nevratné — plán nejde dopsat po faktu
    expect(body.daily.plan.locked_ts).toBeTruthy()
    expect(body.daily.plan.scenarios).toHaveLength(1)
    expect(body.daily.plan.process_goal).toBe('max 3 obchody')
    // Text se dopočítá z struktury, deník zůstane čitelný i bez UI
    expect(body.text).toContain('Když nad flipem → long, risk 3 b')
  })
})

test('report card známkuje segmenty seance, ne kalendářní den (#712)', async () => {
  mockJournalApi([])
  render(<JournalView />)
  fireEvent.change(screen.getByLabelText('Typ záznamu'), { target: { value: 'retro_dne' } })
  await waitFor(() => expect(screen.getByLabelText('Známka US open +30')).toBeTruthy())
  // Futures profil má Globex noc i power hour — akciové dělení by tu bylo mrtvé
  expect(screen.getByLabelText('Známka Globex noc')).toBeTruthy()
  expect(screen.getByLabelText('Známka Power hour')).toBeTruthy()

  fireEvent.change(screen.getByLabelText('Známka US open +30'), { target: { value: 'B' } })
  fireEvent.change(screen.getByLabelText('Cíl na zítřek'), { target: { value: 'držet stop' } })
  fireEvent.click(screen.getByRole('button', { name: '☾ Vyhodnocení dne' }))

  await waitFor(() => {
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    const body = JSON.parse(String(post![1]!.body)) as {
      tags: string[]
      daily: { review: { segments: Array<{ key: string; grade: string }>; tomorrow_goal: string } }
    }
    expect(body.tags).toContain('vyhodnoceni')
    const open30 = body.daily.review.segments.find((s) => s.key === 'open30')
    expect(open30?.grade).toBe('B')
    expect(body.daily.review.tomorrow_goal).toBe('držet stop')
  })
})

test('promeškaný setup vyžaduje důvod a odešle ho (#715)', async () => {
  mockJournalApi([])
  render(<JournalView />)
  fireEvent.change(screen.getByLabelText('Typ záznamu'), { target: { value: 'promeskane' } })
  await waitFor(() => expect(screen.getByLabelText('Proč jsem setup nevzal')).toBeTruthy())
  fireEvent.change(screen.getByLabelText('Text záznamu'), { target: { value: 'Viděl, nevzal.' } })

  // Bez důvodu se neodešle a řekne proč
  fireEvent.click(screen.getByRole('button', { name: 'Přidat záznam' }))
  await waitFor(() => expect(screen.getByText(/Vyber důvod/)).toBeTruthy())
  expect(fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')).toBeUndefined()

  fireEvent.change(screen.getByLabelText('Proč jsem setup nevzal'), {
    target: { value: 'nedovera' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Přidat záznam' }))
  await waitFor(() => {
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    const body = JSON.parse(String(post![1]!.body)) as {
      entry_type: string
      missed_reason: string
    }
    expect(body.entry_type).toBe('promeskane')
    expect(body.missed_reason).toBe('nedovera')
  })
})

test('hledání v textu jde na server jako filtr q (#715)', async () => {
  mockJournalApi([])
  render(<JournalView />)
  fireEvent.change(screen.getByLabelText('Hledat'), { target: { value: 'flip' } })
  await waitFor(() => {
    const get = fetchMock.mock.calls.find(
      ([url, init]) => String(url).includes('q=flip') && (!init || init.method === undefined),
    )
    expect(get).toBeTruthy()
  })
})

test('export MD nese strukturovaná pole i kontext (#715)', () => {
  const md = journalToMarkdown([
    {
      ...ENTRY,
      entry_type: 'obchod',
      missed_reason: null,
      context: { regime: 'negativní gamma', flip: 6805, session_segment: 'open30' },
      trade: {
        direction: 'short',
        planned_entry: 6810,
        planned_stop: 6813,
        planned_target: null,
        actual_entry: 6810,
        actual_exit: 6798,
        size: null,
        opened_ts: null,
        closed_ts: null,
        setup_key: 'wall_bounce',
        failure_mode: null,
        setup_grade: 'A',
        execution_grade: null,
        mistake_tags: ['late_exit'],
        emotion: null,
        mfe: null,
        mae: null,
        gross_pnl: null,
        net_pnl: null,
        fees: null,
      },
    },
  ])
  expect(md).toContain('setup wall_bounce')
  expect(md).toContain('+4.00R')
  expect(md).toContain('Pozdní výstup')
  expect(md).toContain('session_segment open30')
})
