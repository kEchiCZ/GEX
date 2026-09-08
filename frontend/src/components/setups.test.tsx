/** Testy setup detektoru v UI (ADR-0004): obrazovka Setupy, hodnocení, WS refresh. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { CURRENT_MECHANICS_VERSION, bandGateStats, bandInfo, bandLabel, formatGateBucket, formatPct, formatPnlUsd, setupPnlPct, setupPnlUsd, setupRrr, templateLabel } from '../api/setups' // prettier-ignore
import type { SetupRow } from '../api/setups'
import { pointValue } from '../instrument/tick'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

const SETUP_ROW = {
  id: 7,
  symbol: 'ES',
  expiry: '20260717',
  template: 'failed_break',
  direction: 'long',
  created_ts: '2026-07-17T15:02:00+00:00',
  entry: 7501,
  target: 7515,
  stop: 7472,
  confidence: 55,
  reason: 'Neúspěšný průraz 7500 dolů (dno 7473 bez akceptace) a reclaim — spring.',
  status: 'closed_target',
  closed_ts: '2026-07-17T15:40:00+00:00',
  outcome_r: 0.48,
  mfe: 15,
  mae: 6,
  user_rating: null,
  user_note: null,
  mechanics_version: CURRENT_MECHANICS_VERSION,
  // Poloha v tlumící zóně a stínová brána (#1060): T2 základ 55, uvnitř +10 → 65
  context: {
    gex_regime: 'negative',
    band_depth: 1.62,
    band_metrics_version: 3,
    band_class: 'inside',
    confidence_band_adjust: 10,
    confidence_base: 55,
    confidence_template: 55,
    confidence_source: 'wilson ES·failed_break·negative n=35',
    band_gate_simple: 'pass',
    band_gate_regime: 'pass',
  },
}

function mockApi(setups: Array<Record<string, unknown>>) {
  const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
    const target = String(url)
    if (init?.method === 'PATCH' && target.includes('/review')) {
      return { ok: true, json: async () => ({ status: 'ok' }) }
    }
    if (target.includes('/setups/')) {
      return { ok: true, json: async () => ({ symbol: 'ES', setups }) }
    }
    if (target.includes('/expiries')) {
      return { ok: true, json: async () => ({ expiries: ['20260717'] }) }
    }
    if (target.includes('/watchlist')) {
      return { ok: true, json: async () => ({ watchlist: [{ id: 1, symbol: 'ES' }] }) }
    }
    if (target.includes('/annotations')) {
      return { ok: true, json: async () => ({ annotations: [] }) }
    }
    return { ok: false, status: 404, json: async () => ({}) }
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderApp() {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  return render(<App socket={socket} />)
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.restoreAllMocks()
})

test('výpočet RRR ze setupu', () => {
  expect(setupRrr({ entry: 7501, target: 7515, stop: 7472 })).toBeCloseTo(14 / 29)
  expect(setupRrr({ entry: 7501, target: 7515, stop: 7501 })).toBe(0)
})

test('T7 trend_continuation má český popisek, ne syrový kód šablony (#506)', () => {
  expect(templateLabel('trend_continuation')).toBe('Pokračování trendu')
})

test('P/L setupu v USD na 1 kontrakt (#185)', () => {
  // riziko 29 bodů, outcome +0.48 R → 13.92 bodu × 50 $ (ES) = 696 $
  expect(setupPnlUsd({ entry: 7501, stop: 7472, outcome_r: 0.48 }, pointValue('ES'))).toBeCloseTo(
    696,
  )
  // plný stop = −1 R → −29 bodů × 20 $ (NQ) = −580 $
  expect(setupPnlUsd({ entry: 7501, stop: 7472, outcome_r: -1 }, pointValue('NQ'))).toBeCloseTo(
    -580,
  )
  expect(setupPnlUsd({ entry: 7501, stop: 7472, outcome_r: null }, 50)).toBeNull()
  expect(formatPnlUsd(696)).toBe('+696 $')
  expect(formatPnlUsd(-580.125)).toBe('-580.12 $') // Math.round půlí k +∞
})

test('P/L v % startovního účtu 5 000 $ na ticker (#191)', () => {
  // 0.48 R × 29 b × 50 $ = 696 $ na účtu 5 000 $ → +13.92 %
  expect(setupPnlPct({ entry: 7501, stop: 7472, outcome_r: 0.48 }, pointValue('ES'))).toBeCloseTo(
    13.92,
  )
  expect(setupPnlPct({ entry: 7501, stop: 7472, outcome_r: null }, 50)).toBeNull()
  expect(formatPct(13.92)).toBe('+13.92 %')
  expect(formatPct(-1.5)).toBe('-1.50 %')
})

test('poloha v pásmu z contextu setupu (#1060): štítek, chybějící brána, rozpad pass/block', () => {
  const inside = bandInfo(SETUP_ROW as unknown as SetupRow)
  expect(inside).not.toBeNull()
  expect(bandLabel(inside!)).toBe('uvnitř pásma +10')
  expect(inside!.gateRegime).toBe('pass')
  // Přechod: posun 0 → „±0"
  const transition = bandInfo({
    context: {
      band_class: 'transition',
      confidence_band_adjust: 0,
      band_gate_simple: 'pass',
      band_gate_regime: 'unknown',
    },
  })
  expect(bandLabel(transition!)).toBe('přechod ±0')
  // Starší řádek bez brány (nebo bez contextu) → null, nic se nekreslí
  expect(bandInfo({ context: { gex_regime: 'positive' } })).toBeNull()
  expect(bandInfo({ context: null })).toBeNull()
  expect(bandInfo({ context: { band_class: 'inside', band_gate_simple: 'pass' } })).toBeNull()

  const rows = [
    SETUP_ROW, // inside, pass/pass, +0.48
    {
      ...SETUP_ROW,
      id: 8,
      outcome_r: -1,
      status: 'closed_stop',
      context: {
        band_class: 'outside',
        confidence_band_adjust: -15,
        band_gate_simple: 'block',
        band_gate_regime: 'block',
      },
    },
    {
      ...SETUP_ROW,
      id: 9,
      outcome_r: 0.5,
      status: 'closed_target',
      context: {
        band_class: 'transition',
        confidence_band_adjust: 0,
        band_gate_simple: 'pass',
        band_gate_regime: 'unknown', // neznámý režim — do regime skupin nevstupuje
      },
    },
    { ...SETUP_ROW, id: 10, status: 'active', outcome_r: null }, // aktivní se nepočítá
    { ...SETUP_ROW, id: 11, context: null }, // bez brány se nepočítá
  ] as unknown as SetupRow[]
  const stats = bandGateStats(rows)
  expect(stats).not.toBeNull()
  expect(stats!.simple.pass).toEqual({ n: 2, avgR: 0.49, winRate: 1 })
  expect(stats!.simple.block).toEqual({ n: 1, avgR: -1, winRate: 0 })
  expect(stats!.regime.pass.n).toBe(1)
  expect(stats!.regime.block.n).toBe(1)
  expect(formatGateBucket(stats!.simple.pass)).toBe('2 · +0.49 R')
  expect(formatGateBucket({ n: 0, avgR: 0, winRate: 0 })).toBe('—')
  // Žádný uzavřený setup s bránou → null (blok se nekreslí)
  expect(bandGateStats([{ ...SETUP_ROW, context: null }] as unknown as SetupRow[])).toBeNull()
})

test('obrazovka Setupy: historie s výsledkem a hodnocením', async () => {
  const fetchMock = mockApi([SETUP_ROW])
  renderApp()

  fireEvent.click(screen.getByRole('button', { name: 'Setupy' }))
  expect(await screen.findByText('Neúspěšný průraz')).toBeDefined()
  // '+0.48' je v R sloupci tabulky i v dlaždici Ø R (jediný uzavřený setup)
  expect(screen.getAllByText('+0.48').length).toBe(2)
  // 'Cíl' je hlavička sloupce i badge stavu — badge přidává druhý výskyt
  expect(screen.getAllByText('Cíl').length).toBe(2)
  // Čas uzavření a P/L v USD na 1 kontrakt (#185): 0.48 R × 29 b × 50 $ = 696 $
  const closedCell = document.querySelector('[data-part="closed-ts"]')
  expect(closedCell?.textContent).toMatch(/\d{1,2}:\d{2}/) // closed_ts se zobrazuje
  // P/L buňka nese dolary i % účtu 5 000 $ (#191)
  expect(document.querySelector('[data-part="pnl"]')?.textContent).toContain('+696 $')
  expect(document.querySelector('[data-part="pnl"]')?.textContent).toContain('+13.92 %')
  // Zvýrazněné souhrnné statistiky (#189/#191): Ø R, Σ P/L, % P/L vůči účtu
  expect(screen.getByTestId('setups-total-pnl').textContent).toBe('+696 $')
  expect(screen.getByTestId('setups-total-pct').textContent).toBe('+13.92 %')
  expect(screen.getByText('Ø R')).toBeDefined()
  // Od „Σ dnes (1 kontrakt)“ (27. 8.) nese text víc dlaždic — stačí, že existují
  expect(screen.getAllByText(/1 kontrakt/).length).toBeGreaterThan(0)
  expect(screen.getByText(/účet 5k/)).toBeDefined()
  // EV / obchod (#911): jediný uzavřený obchod +696 $ → EV = +696 $, tooltip s rozkladem
  const evTile = screen.getByTestId('setups-ev')
  expect(evTile.textContent).toBe('+696 $')
  expect(evTile.getAttribute('title')).toContain('dlouhodobě vydělává')
  // Poloha v pásmu (#1060): sloupec Pásmo se štítkem a stínová brána v dlaždicích
  const bandCell = document.querySelector('[data-part="band"]')
  expect(bandCell?.textContent).toBe('uvnitř pásma +10')
  expect(bandCell?.querySelector('.setup-band')?.getAttribute('title')).toContain('prošel by')
  expect(screen.getByTestId('gate-simple-pass').textContent).toBe('1 · +0.48 R')
  expect(screen.getByTestId('gate-simple-block').textContent).toBe('—')
  expect(screen.getByTestId('gate-regime-pass').textContent).toBe('1 · +0.48 R')

  // Ruční hodnocení: 👍 pošle PATCH na /setups/ES/7/review
  fireEvent.click(screen.getByRole('button', { name: 'Setup 7 vyšel' }))
  await waitFor(() => {
    const patch = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === 'PATCH',
    )
    expect(patch).toBeDefined()
    expect(String(patch?.[0])).toContain('/setups/ES/7/review')
    expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({
      rating: 1,
      note: null,
    })
  })
})

test('aktivní setup: karta nad grafem s úrovněmi a skrytím', async () => {
  mockApi([{ ...SETUP_ROW, status: 'active', closed_ts: null, outcome_r: null }])
  renderApp()

  expect(await screen.findByLabelText('Aktivní setupy')).toBeDefined()
  expect(screen.getByText('Entry 7501')).toBeDefined()
  expect(screen.getByText('Cíl 7515')).toBeDefined()
  expect(screen.getByText('Stop 7472')).toBeDefined()
  // Zdroj důvěry (#794 fáze 2B) v tooltipu čísla
  const confidence = screen.getByTestId('setup-confidence')
  expect(confidence.textContent).toBe('důvěra 55 %')
  expect(confidence.getAttribute('title')).toContain('Wilsonova dolní mez')
  expect(confidence.getAttribute('title')).toContain('ES·failed_break·negative n=35')
  // Štítek polohy v pásmu na kartě (#1060) s tooltipem stínové brány
  const band = screen.getByTestId('setup-band')
  expect(band.textContent).toBe('uvnitř pásma +10')
  expect(band.getAttribute('title')).toContain('základ šablony 55 %, po úpravě 65 %')
  // Čas vzniku setupu (created_ts) v kartě: datum + čas v lokální zóně (issue #113/#115)
  const cardTime = screen.getByLabelText('Aktivní setupy').querySelector('.setup-card-time')
  expect(cardTime?.textContent).toMatch(/\d{4}/) // rok = je tam datum
  expect(cardTime?.textContent).toMatch(/\d{1,2}:\d{2}/) // i čas

  fireEvent.click(screen.getByRole('button', { name: 'Skrýt setup 7' }))
  expect(screen.queryByLabelText('Aktivní setupy')).toBeNull()
})

test('alert ve zvonečku ukazuje čas notifikace (issue #113)', async () => {
  mockApi([])
  renderApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('alerts', {
      kind: 'setup',
      symbol: 'ES',
      message: 'Nový setup SHORT (failed_break): entry 29094, cíl 28780, stop 29109.8',
      ts: 1784301720,
    })
  })
  fireEvent.click(await screen.findByRole('button', { name: /Notifikace/ }))
  const time = document.querySelector('.alert-time')
  expect(time).not.toBeNull()
  expect(time!.textContent).toMatch(/\d{4}/) // datum
  expect(time!.textContent).toMatch(/\d{1,2}:\d{2}/) // čas
  // Globální zvoneček → u alertu i symbol instrumentu
  const dropdown = screen.getByRole('dialog', { name: 'Historie alertů' })
  expect(dropdown.textContent).toContain('ES')
})

test('setup alerty ve zvonečku proklikávají na graf / na Setupy (#186)', async () => {
  mockApi([])
  renderApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('alerts', {
      kind: 'setup',
      event: 'created',
      symbol: 'NQ',
      message: 'Nový setup LONG (wall_bounce): entry 23000, cíl 23060, stop 22970',
      ts: 1752822000,
    })
    ws.push('alerts', {
      kind: 'setup',
      event: 'closed',
      symbol: 'NQ',
      message: 'Setup #5 uzavřen: cíl zasažen, výsledek +2.00 R',
      ts: 1752823000,
    })
  })

  // Výsledek setupu → stránka Setupy daného instrumentu (vyhodnocení)
  fireEvent.click(screen.getByRole('button', { name: /Notifikace/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Otevřít vyhodnocení setupů NQ' }))
  expect(await screen.findByRole('heading', { name: 'Setupy — NQ' })).toBeDefined()

  // Nový setup → graf instrumentu (karta + entry/cíl/stop linie)
  fireEvent.click(screen.getByRole('button', { name: /Notifikace/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Otevřít graf NQ' }))
  expect(await screen.findByTestId('data-source')).toBeDefined()
  expect(screen.queryByRole('heading', { name: /Setupy —/ })).toBeNull()
})

test('WS událost setups.* přenačte setupy', async () => {
  const fetchMock = mockApi([])
  renderApp()
  await waitFor(() => {
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/setups/ES'))).toBe(true)
  })
  const before = fetchMock.mock.calls.filter(([url]) => String(url).includes('/setups/ES')).length

  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('setups.ES', { event: 'created', id: 9 })
  })
  await waitFor(() => {
    const after = fetchMock.mock.calls.filter(([url]) => String(url).includes('/setups/ES')).length
    expect(after).toBeGreaterThan(before)
  })
})

test('statistiky počítají jen aktuální mechaniku, starší jde zapnout (#311)', async () => {
  // Starý setup má jinou sémantiku stopů/cílů (Ø RRR 25–47) — do bilance
  // aktuálního systému nepatří, jinak by čísla popisovala mrtvý detektor
  const legacy = {
    ...SETUP_ROW,
    id: 1,
    outcome_r: -8,
    status: 'closed_stop',
    mechanics_version: 1,
  }
  mockApi([legacy, SETUP_ROW])
  renderApp()

  fireEvent.click(await screen.findByRole('button', { name: 'Setupy' }))
  await screen.findByRole('heading', { name: /Setupy —/ })

  // Default: jen aktuální verze → jeden řádek, ΣR z něj
  expect(screen.getAllByRole('row').length - 1).toBe(1)
  const toggle = screen.getByLabelText(/Včetně starší mechaniky/)
  expect(toggle).toBeDefined()

  // Po zapnutí se přidá i starý setup
  fireEvent.click(toggle)
  await waitFor(() => expect(screen.getAllByRole('row').length - 1).toBe(2))
})
