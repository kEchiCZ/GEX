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
})
