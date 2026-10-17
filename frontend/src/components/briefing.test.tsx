/** Briefing (#674): smoke render + předvyplnění ranního plánu do deníku. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { BriefingView } from './BriefingView'

const useAppStateMock = vi.fn()
vi.mock('../state/AppState', () => ({
  useAppState: () => useAppStateMock(),
}))

const fetchMock = vi.fn()
const setJournalDraft = vi.fn()
const setView = vi.fn()

function appState(overrides: Record<string, unknown> = {}) {
  return {
    regimeInfo: { state: null, measuredFlip: null, dynamicFlip: null },
    symbol: 'ES',
    selectedExpiry: '20260813',
    setJournalDraft,
    setView,
    status: { engine: 'online' },
    ...overrides,
  }
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  fetchMock.mockReset()
  setJournalDraft.mockReset()
  setView.mockReset()
  useAppStateMock.mockReturnValue(appState())
})

afterEach(() => {
  vi.useRealTimers()
})

/** Pevné „teď" pro testy verdiktu dne: stav karty závisí na obchodním dni a US openu,
takže test nesmí záviset na dni běhu. Falšuje se jen Date — časovače běží reálně. */
function freezeDate(iso: string) {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(iso))
}

/** Uložený verdikt enginu (#1595): čtvrtek 24. 9. 2026, US open 13:30 UTC = 15:30 SELČ. */
const STORED_ROW = {
  id: 7,
  session_date: '2026-09-24',
  symbol: 'ES',
  verdict: 'long',
  score: 5,
  votes: [
    { name: 'trend_higher', vote: 2, reason: 'vyšší TF (týden, den) rostoucí' },
    { name: 'overnight', vote: -1, reason: 'cena 6446 pod včerejším close 6450' },
    { name: 'gamma', vote: 0, reason: 'pozitivní gamma, ale po útesu 83 % gammy — tlumení tenké, nehlasuje' }, // prettier-ignore
  ],
  rules_version: 3,
  created_at: '2026-09-23T22:05:00+00:00',
  updated_at: '2026-09-24T13:25:00+00:00',
}

function mockApis(overrides: Record<string, unknown> = {}) {
  fetchMock.mockImplementation((url: string) => {
    const path = String(url)
    const body = (data: unknown, status = 200) =>
      Promise.resolve({ ok: status < 300, status, json: () => Promise.resolve(data) })
    if (path.includes('/bars/')) return body(overrides['bars'] ?? { bars: [] })
    if (path.includes('/instruments/')) return body(overrides['days'] ?? { days: [] })
    if (path.includes('/levels/')) return body(overrides['levels'] ?? { levels: [] })
    if (path.includes('/oidelta/')) return body({ symbol: 'ES', expiry: '20260813', days: null })
    if (path.includes('/gammacliff/'))
      return body(overrides['gammacliff'] ?? { today: null, rows: [] })
    if (path.includes('/news/upcoming')) return body({ upcoming: [] })
    if (path.includes('/sentiment/state')) return body(null)
    if (path.includes('/gexforward/')) return body({ days: [] })
    if (path.includes('/volregime/')) return body(overrides['volregime'] ?? { rows: [] })
    if (path.includes('/emrespect/')) return body(overrides['emrespect'] ?? { summary: null })
    if (path.includes('/ivrank/')) return body(overrides['ivrank'] ?? { latest: [] })
    if (path.includes('/tendency/')) return body(overrides['tendency'] ?? { tendency: [] })
    if (path.includes('/news/reactions/typical')) return body(overrides['typical'] ?? { typical: [] }) // prettier-ignore
    if (path.includes('/briefing/verdicts'))
      return body(overrides['verdicts'] ?? { verdicts: [] }, (overrides['verdictsStatus'] as number) ?? 200) // prettier-ignore
    if (path.includes('/candles/')) {
      const tf = new URL(path, 'http://x').searchParams.get('tf') ?? 'D'
      const byTf = (overrides['candles'] ?? {}) as Record<string, unknown[]>
      return body({ candles: byTf[tf] ?? [] })
    }
    return body({})
  })
}

/** Briefing verdikt nikdy nepočítá ani neposílá (#1595) — zapisuje jen engine. */
function postCalls() {
  return fetchMock.mock.calls.filter((call) => (call[1] as RequestInit | undefined)?.method === 'POST') // prettier-ignore
}

/** Rostoucí zigzag: swingy > trend, ať struktura HH/HL vyjde (viz trend.test.ts). */
function risingCandles(count: number, step = 2): Array<Record<string, unknown>> {
  return Array.from({ length: count }, (_, i) => {
    const phase = (i % 8) / 8
    const swing = (phase < 0.5 ? phase * 2 : (1 - phase) * 2) * 12
    const close = 6000 + step * i + swing
    return {
      ts: new Date(Date.UTC(2026, 7, 1) + i * 3_600_000).toISOString(),
      open: close,
      high: close + 1,
      low: close - 1,
      close,
      volume: 100,
      partial: false,
    }
  })
}

test('karta Trend (#1089): směr per timeframe a čtení shora dolů', async () => {
  mockApis({
    candles: {
      W: risingCandles(60),
      D: risingCandles(60),
      '240': risingCandles(60),
      '60': risingCandles(60, -2),
      '15': risingCandles(5),
    },
  })
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByTestId('trend-reading').textContent).toContain('Vyšší TF: rostoucí.')
  })
  expect(screen.getByTestId('trend-row-W').textContent).toContain('rostoucí ●')
  expect(screen.getByTestId('trend-row-60').textContent).toContain('klesající')
  // Málo dat se přizná, nic se nedosazuje
  expect(screen.getByTestId('trend-row-15').textContent).toContain('málo dat (5/20 svíček)')
})

test('Shrnutí dne (#1595): uložený verdikt enginu před openem — předběžný se stářím, hlasy z řádku, úrovně obratu', async () => {
  freezeDate('2026-09-24T13:28:00Z') // čtvrtek, 2 min před US openem (13:30 UTC)
  const rising = risingCandles(60)
  mockApis({
    candles: { W: rising, D: rising, '240': rising, '60': rising, '15': rising },
    bars: { bars: [{ ts_min: '2026-08-13T13:00:00Z', open: 6440, high: 6450, low: 6430, close: 6446, volume: 10 }] }, // prettier-ignore
    levels: { levels: [{ ts_min: '2026-08-13T14:00:00Z', flip: 6430, call_wall: 6500, put_wall: 6400, centroid: 6445, total_gex: -900 }] }, // prettier-ignore
    verdicts: { verdicts: [{ ...STORED_ROW, session_date: '2026-09-23', verdict: 'short' }, STORED_ROW] }, // prettier-ignore
  })
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByTestId('summary-verdict').textContent).toBe('Spíše LONG den')
  })
  expect(screen.getByTestId('summary-verdict-text').textContent).toContain('skóre +5')
  expect(screen.getByTestId('summary-verdict-state').textContent).toBe(
    'předběžný: engine ho přepočítává každých 5 min, od 15:15 každou minutu; platí poslední stav před openem 15:30. Uloženo 15:25, před 3 min.',
  )
  // Hlasy a důvody z uloženého řádku — Briefing nic nepřepočítává
  expect(screen.getByText(/cena 6446 pod včerejším close 6450/)).toBeTruthy()
  expect(screen.getByText(/tlumení tenké, nehlasuje/)).toBeTruthy()
  // Tooltip v odrážkách s odřádkováním (vzor ivRankTooltip)
  const title = screen.getByTestId('summary-verdict').getAttribute('title') ?? ''
  expect(title).toContain('\n• ')
  expect(title).toContain('engine')
  const verdictGet = fetchMock.mock.calls.find((call) => String(call[0]).includes('/briefing/verdicts')) // prettier-ignore
  expect(String(verdictGet?.[0])).toContain('/briefing/verdicts?symbol=ES&days=1')
  const levelsText = screen.getByTestId('summary-levels').textContent ?? ''
  expect(levelsText).toContain('Těžiště GEX · podpora')
  expect(levelsText).toContain('Call wall · odpor')
  expect(postCalls()).toHaveLength(0)
})

test('po US openu je uložený verdikt zmrazený — poslední zápis (#1585, #1595)', async () => {
  freezeDate('2026-09-24T15:00:00Z') // čtvrtek, 1,5 h po US openu
  mockApis({ verdicts: { verdicts: [{ ...STORED_ROW, updated_at: '2026-09-24T13:29:05Z' }] } })
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByTestId('summary-verdict').textContent).toBe('Spíše LONG den')
  })
  expect(screen.getByTestId('summary-verdict-state').textContent).toBe(
    'zmrazený — poslední zápis 15:29',
  )
  expect(postCalls()).toHaveLength(0)
})

test('bez uloženého řádku po openu je vidět mezera, žádný náhradní výpočet (#1595)', async () => {
  freezeDate('2026-09-24T15:00:00Z')
  const rising = risingCandles(60)
  mockApis({ candles: { W: rising, D: rising, '240': rising, '60': rising, '15': rising } })
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByTestId('summary-verdict').textContent).toBe(
      'Verdikt pro tuto seanci nevznikl.',
    )
  })
  expect(screen.queryByTestId('summary-verdict-text')).toBeNull()
  expect(postCalls()).toHaveLength(0)
})

test('o víkendu se verdikt nevydává — sobota nemá US open ani settle (#1309)', async () => {
  freezeDate('2026-09-26T15:00:00Z') // sobota
  mockApis()
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByTestId('summary-verdict').textContent).toBe(
      'Verdikt se nevydává — sobota 26. 9. není obchodní seance.',
    )
  })
  expect(postCalls()).toHaveLength(0)
})

test('symbol bez pipeline enginu: verdikt nikdo nepočítá (#1595)', async () => {
  freezeDate('2026-09-24T12:00:00Z')
  useAppStateMock.mockReturnValue(appState({ symbol: 'AAPL', status: { engine: 'online', symbols: 'ES,NQ' } })) // prettier-ignore
  mockApis()
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByTestId('summary-verdict').textContent).toBe(
      'Engine pro tento symbol verdikt nepočítá.',
    )
  })
})

test('chyba čtení verdiktu je výslovná s kódem, ne „bez verdiktu" (#1595)', async () => {
  freezeDate('2026-09-24T12:00:00Z')
  mockApis({ verdicts: { detail: 'db down' }, verdictsStatus: 503 })
  render(<BriefingView />)
  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toBe('Verdikt se nepodařilo načíst (HTTP 503)')
  expect(alert.getAttribute('data-testid')).toBe('summary-verdict')
})

test('karta Trend bez svíček říká, že se načítají / chybí', async () => {
  mockApis()
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByTestId('trend-reading').textContent).toBe(
      'Zatím málo svíček pro čtení trendu.',
    )
  })
})

test('prázdný stav drží tvar — všechny karty s fallback texty', async () => {
  mockApis()
  render(<BriefingView />)
  expect(screen.getByText(/Ranní briefing · ES/)).toBeTruthy()
  await waitFor(() => {
    expect(screen.getByText('Levels dnešní seance zatím nejsou.')).toBeTruthy()
  })
  expect(screen.getByText(/ΔOI bude po ranním OI archivu/)).toBeTruthy()
  expect(screen.getByText(/Odpad gammy se spočítá/)).toBeTruthy()
  expect(screen.getByText('Dnes žádné plánované eventy v kalendáři.')).toBeTruthy()
})

test('karta Volatilita (#873): hodnoty z /volregime a /emrespect, EM z prop', async () => {
  mockApis({
    volregime: {
      rows: [
        { session_date: '2026-08-25', session_range: 42, percentile: 0.54, bucket: 'normal', sample: 252 }, // prettier-ignore
      ],
    },
    emrespect: {
      summary: { window_days: 90, n: 12, close_in_band_share: 0.75, touch_upper_share: 0.25, touch_lower_share: 0.17 }, // prettier-ignore
    },
  })
  render(
    <BriefingView
      expectedMove={{ refMinuteIdx: 0, preOpen: true, anchor: 7600, atmStrike: 7600, em: 38.5, upper: 7638.5, lower: 7561.5 }} // prettier-ignore
    />,
  )
  await waitFor(() => {
    expect(screen.getByTestId('vol-bucket').textContent).toContain('normální · p54 (252 seancí)')
  })
  expect(screen.getByTestId('vol-em').textContent).toContain('±38.5 b (0.51 % spotu)')
  expect(screen.getByTestId('vol-em').textContent).toContain('pre-open odhad')
  expect(screen.getByTestId('vol-emrespect').textContent).toContain('close uvnitř pásma 75 % dnů (n=12/90 d)') // prettier-ignore
})

test('IV percentil (#871): primárně ibkr percentil, rank a tasty v tooltipu', async () => {
  mockApis({
    ivrank: {
      latest: [
        { session_date: '2026-08-26', source: 'ibkr', iv: 0.1191, iv_rank: 0.31, iv_percentile: 0.19, sample: 252 }, // prettier-ignore
        { session_date: '2026-08-26', source: 'tasty', iv: 0.1557, iv_rank: 0.3119, iv_percentile: 0.1883, sample: 0 }, // prettier-ignore
      ],
    },
  })
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByTestId('vol-ivr').textContent).toContain('p19 · IV 11.9 % (252 dnů)')
  })
  const title = screen.getByTestId('vol-ivr').getAttribute('title') ?? ''
  expect(title).toContain('IV Rank')
  expect(title).toContain('31')
  expect(title).toContain('tasty')
  // Řada pod MIN_SAMPLE (percentil null) se chová jako bez dat
})

test('IV percentil bez ibkr řady říká, že se plní po settle', async () => {
  mockApis({
    ivrank: {
      latest: [
        { session_date: '2026-08-26', source: 'ibkr', iv: 0.12, iv_rank: null, iv_percentile: null, sample: 10 }, // prettier-ignore
      ],
    },
  })
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByTestId('vol-ivr').textContent).toContain('bez dat')
  })
})

test('karta Volatilita bez dat říká proč — žádný dosazený default (ADR-0028)', async () => {
  mockApis()
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByTestId('vol-bucket').textContent).toContain('bez dat')
  })
  expect(screen.getByTestId('vol-em').textContent).toContain('bez straddlu')
  expect(screen.getByTestId('vol-emrespect').textContent).toContain('statistika se teprve sbírá')
})

test('☀ založí ranní plán s kostrou textu a přepne na Deník', async () => {
  mockApis({
    levels: {
      levels: [
        {
          ts_min: '2026-08-13T14:00:00Z',
          flip: 6430,
          call_wall: 6500,
          put_wall: 6400,
          centroid: 6445,
          total_gex: 900,
        },
      ],
    },
  })
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByText('6430')).toBeTruthy()
  })

  fireEvent.click(screen.getByText('☀ Založit ranní plán do deníku'))

  expect(setView).toHaveBeenCalledWith('journal')
  const draft = setJournalDraft.mock.calls[0][0] as { text: string }
  expect(draft.text).toContain('Plán dne ES:')
  expect(draft.text).toContain('flip 6430')
  // Volatility box (#873) je v kostře vždy — i bez dat s poctivým „bez dat"
  expect(draft.text).toContain('- Volatilita: ')
  expect(draft.text).toContain('- [ ] riziko přizpůsobeno režimu')
})

test('☀ plán bere verdikt z uloženého řádku enginu (#1595)', async () => {
  freezeDate('2026-09-24T13:28:00Z')
  mockApis({ verdicts: { verdicts: [STORED_ROW] } })
  render(<BriefingView />)
  await waitFor(() => {
    expect(screen.getByTestId('summary-verdict').textContent).toBe('Spíše LONG den')
  })

  fireEvent.click(screen.getByText('☀ Založit ranní plán do deníku'))

  const draft = setJournalDraft.mock.calls[0][0] as { text: string }
  expect(draft.text).toContain('- Verdikt dne: Spíše long den (skóre +5)')
  expect(draft.text).toContain('(předběžný, uloženo 15:25)')
  expect(postCalls()).toHaveLength(0)
})

test('Ranní checklist (#1241): útes minulé seance, slabá zeď, gap-and-hold', async () => {
  mockApis({
    candles: { W: risingCandles(60), D: risingCandles(60), '4h': risingCandles(60), '1h': risingCandles(60), '15m': risingCandles(60) }, // prettier-ignore
    bars: { bars: [{ ts_min: '2026-09-21T13:15:00Z', open: 30240, high: 30245, low: 30235, close: 30240, volume: 1 }] }, // prettier-ignore
    days: { days: [{ date: '2026-09-18' }] },
    levels: { levels: [{ ts_min: '2026-09-21T13:15:00Z', flip: 29834, call_wall: 30300, put_wall: 29875, centroid: 30150, total_gex: 74, call_wall_dom: 0.18, put_wall_dom: 0.2 }] }, // prettier-ignore
    gammacliff: { today: null, rows: [{ session_date: '2026-09-18', cliff_share: 0.826, is_opex: true }] }, // prettier-ignore
  })
  render(<BriefingView />)
  const cliff = await screen.findByTestId('check-cliff')
  expect(cliff.textContent).toContain('odpadlo 83 % (OPEX)')
  expect(cliff.className).toContain('checklist-go')
  const wall = screen.getByTestId('check-wall')
  expect(wall.textContent).toContain('dominance 18 %')
  // Stav mapy (#1245) bez kolektoru: bod je, ale „bez dat" — žádný dosazený verdikt
  expect(screen.getByTestId('check-map').className).toContain('checklist-na')
  expect(wall.textContent).toContain('průraz')
  expect(screen.getByTestId('check-rules').textContent).toContain('50 $')
})
