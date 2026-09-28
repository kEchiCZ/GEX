/** Testy setup detektoru v UI (ADR-0004): obrazovka Setupy, hodnocení, WS refresh. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { CURRENT_MECHANICS_VERSION, bandInfo, bandLabel, formatGateBucket, formatPct, formatPnlUsd, setupPnlPct, setupPnlUsd, setupRrr, templateLabel } from '../api/setups' // prettier-ignore
import type { SetupRow, SummaryGroup } from '../api/setups'
import { pointValue } from '../instrument/tick'
import { sessionDateIso } from '../instrument/tz'
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

const GROUP: SummaryGroup = {
  count: 0,
  active: 0,
  closed: 0,
  wins: 0,
  losses: 0,
  win_rate: null,
  sum_r: 0,
  avg_r: null,
  gross_usd: 0,
  fees_usd: 0,
  net_usd: 0,
  ev_r: null,
  ev_usd: null,
}

/** Serverový souhrn (#1319) — UI ho jen vykresluje, test dodá hotová čísla. */
function summary(overrides: Record<string, unknown> = {}) {
  return {
    symbols: ['ES'],
    mechanics_version: CURRENT_MECHANICS_VERSION,
    all_versions: false,
    total_count: 1,
    legacy_count: 0,
    fee_per_contract_usd: 10,
    account_usd: 50000,
    unpriced_symbols: [],
    all: GROUP,
    tradeable: GROUP,
    shadow: GROUP,
    unruled: GROUP,
    shadow_reasons: {},
    account: null,
    today: {
      session: '2026-07-17',
      trades: 0,
      closed: 0,
      active: 0,
      wins: 0,
      losses: 0,
      win_rate: null,
      best_usd: null,
      worst_usd: null,
      gross_usd: 0,
      fees_usd: 0,
      net_usd: 0,
      gross_pct: 0,
      max_risk_pct: 0,
      total_risk_pct: 0,
      account: null,
    },
    band_gates: null,
    regimes: [],
    performance: {
      daily: [],
      sharpe_all: { sharpe: null, days: 0 },
      sharpe_30: { sharpe: null, days: 0 },
      max_drawdown_r: 0,
      simulation: null,
    },
    ...overrides,
  }
}

/** Jediný uzavřený SETUP_ROW (+0,48 R × 29 b × 50 $ = +696 $), bez risk kontextu. */
const CLOSED_ONE: SummaryGroup = {
  ...GROUP,
  count: 1,
  closed: 1,
  wins: 1,
  win_rate: 1,
  sum_r: 0.48,
  avg_r: 0.48,
  gross_usd: 696,
  fees_usd: 10,
  net_usd: 686,
  ev_usd: { ev: 696, win_rate: 1, loss_rate: 0, avg_win: 696, avg_loss: 0, n: 1 },
}

function mockApi(
  setups: Array<Record<string, unknown>>,
  summaryPayload: Record<string, unknown> | null = summary(),
  totalCount: number = setups.length,
) {
  const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
    const target = String(url)
    if (init?.method === 'PATCH' && target.includes('/review')) {
      return { ok: true, json: async () => ({ status: 'ok' }) }
    }
    if (target.includes('/setups/summary')) {
      return summaryPayload === null
        ? { ok: false, status: 503, json: async () => ({ detail: 'DB' }) }
        : { ok: true, json: async () => summaryPayload }
    }
    if (target.includes('/setups/')) {
      return { ok: true, json: async () => ({ symbol: 'ES', setups, total_count: totalCount }) }
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

test('P/L v % startovního účtu 50 000 $ v jednotkách aplikace (#191, #1185)', () => {
  // 0.48 R × 29 b × 50 $ = 696 $ na účtu 50 000 $ → +1.392 %
  expect(setupPnlPct({ entry: 7501, stop: 7472, outcome_r: 0.48 }, pointValue('ES'))).toBeCloseTo(
    1.392,
  )
  expect(setupPnlPct({ entry: 7501, stop: 7472, outcome_r: null }, 50)).toBeNull()
  expect(formatPct(13.92)).toBe('+13.92 %')
  expect(formatPct(-1.5)).toBe('-1.50 %')
})

test('poloha v pásmu z contextu setupu (#1060): štítek, chybějící brána, formát dlaždice', () => {
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

  // Rozpad pass/block počítá server (#1319) — dlaždice jen formátuje
  expect(formatGateBucket({ n: 2, avg_r: 0.49, win_rate: 1 })).toBe('2 · +0.49 R')
  expect(formatGateBucket({ n: 0, avg_r: 0, win_rate: 0 })).toBe('—')
})

test('obrazovka Setupy: historie s výsledkem a hodnocením', async () => {
  const gate = { n: 1, avg_r: 0.48, win_rate: 1 }
  const empty = { n: 0, avg_r: 0, win_rate: 0 }
  const fetchMock = mockApi(
    [SETUP_ROW],
    summary({
      all: CLOSED_ONE,
      unruled: CLOSED_ONE,
      band_gates: { simple: { pass: gate, block: empty }, regime: { pass: gate, block: empty } },
    }),
  )
  renderApp()

  fireEvent.click(screen.getByRole('button', { name: 'Setupy' }))
  expect(await screen.findByText('Neúspěšný průraz')).toBeDefined()
  expect(await screen.findByTestId('setups-total-pnl')).toBeDefined()
  // '+0.48' je v R sloupci tabulky, v dlaždici Ø R a v Ø R řádku „Bez risk pravidel"
  expect(screen.getAllByText('+0.48').length).toBe(3)
  // 'Cíl' je hlavička sloupce i badge stavu — badge přidává druhý výskyt
  expect(screen.getAllByText('Cíl').length).toBe(2)
  // Čas uzavření a P/L v USD na 1 kontrakt (#185): 0.48 R × 29 b × 50 $ = 696 $
  const closedCell = document.querySelector('[data-part="closed-ts"]')
  expect(closedCell?.textContent).toMatch(/\d{1,2}:\d{2}/) // closed_ts se zobrazuje
  // P/L buňka nese dolary i % účtu 50 000 $ (#191, #1185)
  expect(document.querySelector('[data-part="pnl"]')?.textContent).toContain('+696 $')
  expect(document.querySelector('[data-part="pnl"]')?.textContent).toContain('+1.39 %')
  // Souhrn ze serveru (#1319): hrubý výsledek, poplatky, čistý (1 kontrakt)
  expect(screen.getByTestId('setups-total-pnl').textContent).toBe('+696 $')
  expect(screen.getByTestId('setups-fees').textContent).toBe('-10 $')
  expect(screen.getByTestId('setups-net-pnl').textContent).toBe('+686 $')
  // 'Ø R' je dlaždice souhrnu i sloupec tabulky rozdělení
  expect(screen.getAllByText('Ø R').length).toBe(2)
  expect(screen.getAllByText(/1 kontrakt/).length).toBeGreaterThan(0)
  // Rozdělení obchodovatelné / stínové / bez pravidel
  expect(screen.getByTestId('split-unruled').textContent).toContain('+686 $')
  expect(screen.getByTestId('split-tradeable').textContent).toContain('—')
  // Bez risk kontextu se účet nekreslí (nic se nevymýšlí)
  expect(screen.queryByTestId('setups-account-pnl')).toBeNull()
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
  const fetchMock = mockApi([legacy, SETUP_ROW], summary({ legacy_count: 1 }))
  renderApp()

  fireEvent.click(await screen.findByRole('button', { name: 'Setupy' }))
  await screen.findByRole('heading', { name: /Setupy —/ })
  const historyRows = () =>
    document.querySelectorAll('.setups-table:not(.setups-split) tbody tr').length

  // Default: jen aktuální verze (ze serveru) → jeden řádek v tabulce
  const toggle = await screen.findByLabelText(/Včetně starší mechaniky \(1\)/)
  expect(historyRows()).toBe(1)

  // Po zapnutí se přidá i starý setup a souhrn se přepočítá na serveru se všemi verzemi
  fireEvent.click(toggle)
  await waitFor(() => expect(historyRows()).toBe(2))
  await waitFor(() =>
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('all_versions=true'))).toBe(
      true,
    ),
  )
})

test('přepnutí mechaniky drží přepínač i souhrn ztlumeně do odpovědi, fokus zůstane (#1319)', async () => {
  // Dřív souhrn i přepínač po kliknutí zmizely, dokud nedorazil nový dotaz —
  // prvek uživateli ujel pod kurzorem, fokus se ztratil a stránka poskočila
  const legacy = { ...SETUP_ROW, id: 1, outcome_r: -8, status: 'closed_stop', mechanics_version: 1 }
  const baseFetch = mockApi([legacy, SETUP_ROW], summary({ legacy_count: 1, all: CLOSED_ONE }))
  let release: () => void = () => undefined
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown, init?: RequestInit) => {
      if (String(url).includes('all_versions=true')) await pending
      return baseFetch(url, init)
    }),
  )
  renderApp()
  fireEvent.click(await screen.findByRole('button', { name: 'Setupy' }))
  const toggle = (await screen.findByTestId('setups-all-versions')) as HTMLInputElement
  const stats = await screen.findByRole('group', { name: 'Souhrnné statistiky' })
  expect(stats.getAttribute('aria-busy')).toBe('false')

  act(() => toggle.focus())
  fireEvent.click(toggle)
  // Dotaz se všemi verzemi visí: nic se neodpojí, souhrn je jen ztlumený
  await waitFor(() => expect(stats.getAttribute('aria-busy')).toBe('true'))
  expect(stats.classList.contains('summary-stale')).toBe(true)
  expect(toggle.isConnected).toBe(true)
  expect(stats.isConnected).toBe(true)
  expect(toggle.checked).toBe(true)
  expect(document.activeElement).toBe(toggle)

  await act(async () => {
    release()
  })
  await waitFor(() => expect(stats.getAttribute('aria-busy')).toBe('false'))
  expect(stats.classList.contains('summary-stale')).toBe(false)
  expect(screen.getByTestId('setups-all-versions')).toBe(toggle)
  expect(document.activeElement).toBe(toggle)
})

test('blok Dnes se po 17:00 CT přenačte na novou seanci i bez WS události (#1319)', async () => {
  // Souhrn „Dnes" je snímek serveru; přes noc na Globexu nový setup (a s ním
  // setups.*) nemusí přijít hodiny — přechod seance musí dotaz vyvolat sám
  vi.useFakeTimers({ shouldAdvanceTime: true })
  try {
    vi.setSystemTime(Date.UTC(2026, 8, 28, 21, 59, 30)) // pondělí 16:59:30 CDT
    const baseFetch = mockApi([SETUP_ROW])
    const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
      if (String(url).includes('/setups/summary')) {
        // Server počítá den v okamžiku dotazu (trading_session_date(now))
        const base = summary()
        return {
          ok: true,
          json: async () => ({ ...base, today: { ...base.today, session: sessionDateIso() } }),
        }
      }
      return baseFetch(url, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    const summaryCalls = () =>
      fetchMock.mock.calls.filter(([url]) => String(url).includes('/setups/summary')).length
    renderApp()
    fireEvent.click(await screen.findByRole('button', { name: 'Setupy' }))
    await waitFor(() => expect(screen.getByTestId('day-session').textContent).toBe('28. 9.'))
    const before = summaryCalls()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(61_000) // 17:00:31 CDT → úterní seance
    })
    await waitFor(() => expect(screen.getByTestId('day-session').textContent).toBe('29. 9.'))
    expect(summaryCalls()).toBeGreaterThan(before)
  } finally {
    vi.useRealTimers()
  }
})

test('souhrn nedostupný: chyba místo čísel z tabulky, tabulka ukazuje „N z M" (#1319)', async () => {
  mockApi([SETUP_ROW], null, 604)
  renderApp()
  fireEvent.click(screen.getByRole('button', { name: 'Setupy' }))
  expect(await screen.findByTestId('setups-summary-error')).toBeDefined()
  expect(screen.queryByTestId('setups-total-pnl')).toBeNull()
  expect(screen.getByTestId('setups-page-note').textContent).toContain('posledních 1 z 604')
})

test('obrazovka Setupy: risk sloupec (#1185) — obchodovatelný vs. stín a filtr', async () => {
  const risk = {
    account_equity_usd: 50000,
    risk_budget_usd: 500,
    stop_points: 29,
    contracts: 0,
    max_loss_usd: 0,
    fee_usd: 0,
    affordable: false,
    tradeable: false,
    trade_block: 'stop_over_budget',
    template_gate: 'pass',
  }
  const tradeable = {
    ...SETUP_ROW,
    id: 8,
    stop: 7493,
    context: {
      ...risk,
      stop_points: 8,
      contracts: 1,
      max_loss_usd: 400,
      fee_usd: 10,
      affordable: true,
      tradeable: true,
      trade_block: null,
    },
  }
  const account = {
    trades: 1,
    gross_usd: 192,
    fees_usd: 10,
    net_usd: 182,
    net_pct: 0.364,
    max_drawdown_usd: 0,
  }
  mockApi(
    [SETUP_ROW, { ...SETUP_ROW, id: 9, context: risk }, tradeable],
    summary({
      shadow: { ...GROUP, count: 1 },
      shadow_reasons: { stop_over_budget: 1 },
      account,
    }),
  )
  renderApp()
  fireEvent.click(screen.getByRole('button', { name: 'Setupy' }))
  await screen.findAllByText('Neúspěšný průraz')
  const cells = Array.from(document.querySelectorAll('[data-part="risk"]')).map(
    (c) => c.textContent,
  )
  // Bez pravidel „—", stín s důvodem (ztlumený řádek), obchodovatelný s P/L účtu 0.48 × 400 − 10
  expect(cells).toEqual(['—', 'stín: stop nad rozpočtem rizika', '1 ks · 400 $ +182 $'])
  expect(document.querySelectorAll('tr.setup-shadow').length).toBe(1)
  expect((await screen.findByTestId('setups-account-pnl')).textContent).toBe('+182 $')
  expect(screen.getByTestId('split-shadow').getAttribute('title')).toContain(
    'stop nad rozpočtem rizika: 1',
  )
  // Filtr „jen obchodovatelné" schová stín, řádek bez pravidel zůstává
  fireEvent.click(screen.getByTestId('setups-tradeable-only'))
  await waitFor(() => expect(document.querySelectorAll('[data-part="risk"]').length).toBe(2))
  expect(document.querySelectorAll('tr.setup-shadow').length).toBe(0)
})
