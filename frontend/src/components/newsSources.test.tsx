/** Zdroje zpráv v záložce News (#578): audit, přepínač enabled, editace seznamů. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
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

/** Odložená odpověď: `gate` drží GET daného endpointu, dokud test nezavolá `release`. */
interface Gate {
  sources?: Promise<void>
  settings?: Promise<void>
}

function mockApi(gate: Gate = {}) {
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
      await gate.sources
      return { ok: true, json: async () => ({ days: 7, sources: SOURCES }) }
    }
    if (target.includes('/settings')) {
      await gate.settings
      return { ok: true, json: async () => ({ settings: { ...settings } }) }
    }
    return { ok: false, status: 404, json: async () => ({}) }
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function deferred(): { promise: Promise<void>; release: () => void } {
  let release!: () => void
  const promise = new Promise<void>((resolve) => {
    release = resolve
  })
  return { promise, release }
}

/** Vyprázdní frontu mikrotasků (řetězy `await` ve fetch mocku a klientu API). */
async function flushMicrotasks(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve()
}

// Po testu, ne před ním: spy na performance.now nesmí přežít do cleanupu dalšího testu
afterEach(() => {
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

test('editor kurátorů: klik hned po prvním vykreslení seznamu se neztratí (regrese CI flaku)', async () => {
  // Souběh, který v CI náhodně shazoval test „vypnutí položky je vratné":
  // settings dorazí dřív než registr zdrojů → ListEditor se namountuje už se
  // seznamem, ale jeho pasivní efekty React pouští až v dalším tasku
  // scheduleru (commit mimo act je DefaultLane, efekty nejsou synchronní).
  // Klik uživatele mezi commitem a efekty pak zpracuje nejdřív svůj update a
  // teprve za ním doběhne mount efekt — dřívější `useEffect(!edited)` s
  // uzávěrem edited=false tím vrátil seznam do původní podoby.
  //
  // Okno se otevírá, jen když render překročí frame budget scheduleru (5 ms)
  // a ten ustoupí (yield) před spuštěním efektů — na rychlém stroji nikdy,
  // na CI běžně. Tady se yield vynutí posunem performance.now o 2 ms na
  // každé čtení; scheduler i React čas jen měří, na chování to nemá vliv.
  const sourcesGate = deferred()
  const fetchMock = mockApi({ sources: sourcesGate.promise })
  render(<NewsSourcesSection />)
  await flushMicrotasks()
  let clock = 0
  vi.spyOn(performance, 'now').mockImplementation(() => (clock += 2))
  sourcesGate.release()
  await flushMicrotasks()
  // Krokuje se po tascích scheduleru (ne waitFor — to po splnění ještě čeká
  // setTimeout(0), během něhož mohou efekty doběhnout): v tasku, kde proběhl
  // commit seznamu, pasivní efekty ještě čekají na task další.
  let toggle: HTMLInputElement | null = null
  for (let tick = 0; tick < 10 && toggle === null; tick++) {
    await new Promise((resolve) => setImmediate(resolve))
    toggle = screen.queryByLabelText(
      'Bluesky kurátoři: cnbc.com aktivní',
    ) as HTMLInputElement | null
  }
  if (toggle === null) throw new Error('seznam se do 10 tasků scheduleru nevykreslil')
  expect(toggle.checked).toBe(true)
  fireEvent.click(toggle)
  expect(lastPutBody(fetchMock)).toEqual({ value: ['#cnbc.com', 'did:plc:x'] })
  expect(toggle.checked).toBe(false)
  fireEvent.click(toggle)
  expect(lastPutBody(fetchMock)).toEqual({ value: ['cnbc.com', 'did:plc:x'] })
  expect(toggle.checked).toBe(true)
})

test('editor kurátorů: opožděné první načtení settings nepřepíše rozpracovanou editaci', async () => {
  // Editory se vykreslí i před odpovědí /settings (registr zdrojů už dorazil);
  // přidá-li uživatel položku dřív, opožděná odpověď ji nesmí tiše zahodit.
  const settingsGate = deferred()
  const fetchMock = mockApi({ settings: settingsGate.promise })
  render(<NewsSourcesSection />)
  const input = await screen.findByLabelText('Bluesky kurátoři: nová položka')
  fireEvent.change(input, { target: { value: 'bloomberg.com' } })
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(lastPutBody(fetchMock)).toEqual({ value: ['bloomberg.com'] })
  settingsGate.release()
  // Ostatní seznamy se z odpovědi naplní, editovaný zůstane
  expect(await screen.findByLabelText('Reddit subreddity: stocks aktivní')).toBeDefined()
  expect(screen.getByLabelText('Bluesky kurátoři: bloomberg.com aktivní')).toBeDefined()
  expect(screen.queryByLabelText('Bluesky kurátoři: cnbc.com aktivní')).toBeNull()
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
