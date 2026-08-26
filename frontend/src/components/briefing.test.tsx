/** Briefing (#674): smoke render + předvyplnění ranního plánu do deníku. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { BriefingView } from './BriefingView'

const useAppStateMock = vi.fn()
vi.mock('../state/AppState', () => ({
  useAppState: () => useAppStateMock(),
}))

const fetchMock = vi.fn()
const setJournalDraft = vi.fn()
const setView = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  fetchMock.mockReset()
  setJournalDraft.mockReset()
  setView.mockReset()
  useAppStateMock.mockReturnValue({
    symbol: 'ES',
    selectedExpiry: '20260813',
    setJournalDraft,
    setView,
  })
})

function mockApis(overrides: Record<string, unknown> = {}) {
  fetchMock.mockImplementation((url: string) => {
    const path = String(url)
    const body = (data: unknown) => Promise.resolve({ ok: true, json: () => Promise.resolve(data) })
    if (path.includes('/bars/')) return body(overrides['bars'] ?? { bars: [] })
    if (path.includes('/instruments/')) return body(overrides['days'] ?? { days: [] })
    if (path.includes('/levels/')) return body(overrides['levels'] ?? { levels: [] })
    if (path.includes('/oidelta/')) return body({ symbol: 'ES', expiry: '20260813', days: null })
    if (path.includes('/gammacliff/')) return body({ today: null })
    if (path.includes('/news/upcoming')) return body({ upcoming: [] })
    if (path.includes('/sentiment/state')) return body(null)
    if (path.includes('/gexforward/')) return body({ days: [] })
    if (path.includes('/volregime/')) return body(overrides['volregime'] ?? { rows: [] })
    if (path.includes('/emrespect/')) return body(overrides['emrespect'] ?? { summary: null })
    if (path.includes('/ivrank/')) return body(overrides['ivrank'] ?? { latest: [] })
    return body({})
  })
}

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
