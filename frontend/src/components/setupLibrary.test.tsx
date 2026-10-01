/** Setupy → Knihovna a dialog stádia (#1323): buňky ze serveru, povinný důvod,
rozpočet zkoušky, varování a zápis přes POST /setups/stage. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import type { LibraryBrakes, LibraryCell } from '../api/setupLibrary'
import { LiveSocket } from '../api/ws'
import { AppStateProvider } from '../state/AppState'
import { FakeWebSocket } from '../test/fakeWs'
import { RiskSettings } from './RiskSettings'
import { SetupsView } from './SetupsView'

const NQ_T7: LibraryCell = {
  cell: 'NQ:trend_continuation',
  ticker: 'NQ',
  template: 'trend_continuation',
  template_number: 7,
  stage: 'auto',
  effective_stage: 'auto',
  trial: null,
  gate_verdict: 'block',
  gate_n: 163,
  gate_lb: -0.14,
  avg_r: 0.09,
  avg_net_r: 0.03,
  net_usd: -120.5,
  n_needed: 462,
  sessions: 25,
  per_session: 6.52,
  window_capacity: 489,
  sessions_to_decision: 46,
}

const ES_T7: LibraryCell = {
  ...NQ_T7,
  cell: 'ES:trend_continuation',
  ticker: 'ES',
  stage: 'shadow',
  effective_stage: 'shadow',
  gate_n: 192,
  gate_lb: -0.11,
  avg_r: 0.1,
  avg_net_r: -0.13,
  n_needed: 456,
  sessions_to_decision: 40,
}

const NQ_T4: LibraryCell = {
  ...NQ_T7,
  cell: 'NQ:gamma_momentum',
  template: 'gamma_momentum',
  template_number: 4,
  stage: 'trial',
  effective_stage: 'trial',
  trial: {
    started_at: '2026-10-01T14:05:00+00:00',
    budget_setups: 10,
    budget_r: 3,
    mechanics_version: 5,
    setups: 3,
    sum_r: -1,
    spent: false,
  },
  gate_verdict: 'insufficient',
  gate_n: 12,
  gate_lb: 0.05,
  n_needed: 618,
  per_session: 0.48,
  window_capacity: 28,
  sessions_to_decision: null,
}

const BRAKES: LibraryBrakes = {
  session: '2026-10-01',
  day_r: -1,
  week_r: -2.5,
  daily_brake_r: 3,
  weekly_brake_r: 6,
  max_template_stops_per_day: 2,
  block: null,
  template_stops: { trend_continuation: 1 },
}

const PARAMS = {
  current: {
    version: 1,
    created_ts: '2026-09-08T10:00:00+00:00',
    created_by: 'engine',
    note: 'seed',
    params: {
      template_gate_days: 60,
      template_gate_min_samples: 30,
      trial_budget_setups: 10,
      trial_budget_r: 3,
      disabled_templates: ['divergence_spring'],
      shadow_cells: ['ES:trend_continuation'],
      trial_cells: {},
    },
  },
  defaults: { trial_budget_setups: 10, trial_budget_r: 3 },
}

const GROUP = {
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

function summary(cells: LibraryCell[] | undefined, brakes: LibraryBrakes | undefined = BRAKES) {
  return {
    all: GROUP,
    tradeable: GROUP,
    shadow: GROUP,
    unruled: GROUP,
    symbols: ['ES', 'NQ'],
    mechanics_version: 5,
    all_versions: false,
    total_count: 0,
    legacy_count: 0,
    after_settle_count: 0,
    fee_per_contract_usd: 10,
    account_usd: 50000,
    unpriced_symbols: [],
    shadow_reasons: {},
    account: null,
    today: null,
    band_gates: null,
    regimes: [],
    ...(cells === undefined ? {} : { cells, brakes }),
  }
}

interface MockOptions {
  cells?: LibraryCell[]
  brakes?: LibraryBrakes
  stageResponse?: { status: number; body: unknown }
}

function mockApi(options: MockOptions = {}) {
  const cells = 'cells' in options ? options.cells : [NQ_T7, ES_T7, NQ_T4]
  const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
    const target = String(url)
    if (target.includes('/setups/stage') && init?.method === 'POST') {
      const response = options.stageResponse ?? { status: 201, body: { version: 2 } }
      return {
        ok: response.status < 300,
        status: response.status,
        json: async () => response.body,
      }
    }
    if (target.includes('/setups/params')) {
      if (init?.method === 'POST') return { ok: true, json: async () => ({ version: 2 }) }
      return { ok: true, json: async () => PARAMS }
    }
    if (target.includes('/setups/summary')) {
      return { ok: true, json: async () => summary(cells, options.brakes) }
    }
    if (target.includes('/watchlist')) {
      return {
        ok: true,
        json: async () => ({
          watchlist: [
            { id: 1, symbol: 'ES' },
            { id: 2, symbol: 'NQ' },
          ],
        }),
      }
    }
    if (target.includes('/setups/')) {
      return { ok: true, json: async () => ({ setups: [], total_count: 0 }) }
    }
    return { ok: false, status: 404, json: async () => ({}) }
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderLibrary() {
  // Podzáložka se pamatuje (persist) — test začíná rovnou v Knihovně
  window.localStorage.setItem('gexlens.setupsTab', JSON.stringify('library'))
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  return render(
    <AppStateProvider socket={socket}>
      <SetupsView />
    </AppStateProvider>,
  )
}

function stageCalls(fetchMock: ReturnType<typeof mockApi>) {
  const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit | undefined]>
  return calls
    .filter(([url, init]) => String(url).includes('/setups/stage') && init?.method === 'POST')
    .map(([, init]) => JSON.parse(String(init?.body)) as Record<string, unknown>)
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.restoreAllMocks()
})

test('Knihovna: buňky v pořadí serveru, sloupce důkazu a stav brzd i zkoušek', async () => {
  const fetchMock = mockApi()
  renderLibrary()
  const rows = await screen.findAllByTestId(/^library-row-/)
  // Řadí server (průkaznost) — UI pořadí nemění
  expect(rows.map((row) => row.dataset.testid)).toEqual([
    'library-row-NQ:trend_continuation',
    'library-row-ES:trend_continuation',
    'library-row-NQ:gamma_momentum',
  ])
  const nq = within(rows[0])
  expect(nq.getByText('T7 Pokračování trendu')).toBeDefined()
  expect(nq.getByRole('button', { name: /Stádium T7 Pokračování trendu · NQ: Auto/ })).toBeDefined()
  expect(rows[0].textContent).toContain('✕ block')
  expect(rows[0].textContent).toContain('LB -0.14')
  expect(rows[0].textContent).toContain('+0.09 / +0.03')
  expect(rows[0].textContent).toContain('163 / 462')
  expect(rows[0].textContent).toContain('~46 seancí')
  // Stín uživatele a běžící zkouška s čerpáním; vzácná šablona: okno brány
  // vzorek nepojme, takže žádný odhad v seancích
  expect(rows[1].textContent).toContain('Stín')
  expect(rows[1].textContent).toContain('+0.10 / -0.13')
  expect(rows[2].textContent).toContain('Zkouška')
  expect(rows[2].textContent).toContain('3/10 · -1.0 z -3.0 R')
  expect(rows[2].textContent).toContain('· 12/30')
  expect(rows[2].textContent).toContain('v okně nedosáhne · max ~28/618')
  // Mobil: každá buňka nese popisek sloupce pro kartu (CSS ::before)
  for (const cell of rows[0].querySelectorAll('td')) {
    expect(cell.getAttribute('data-label')).not.toBeNull()
  }
  // Hlavička: brzdy účtu a běžící zkoušky
  expect(screen.getByTestId('library-brakes').textContent).toContain(
    'Brzdy: den -1.0 / -3.0 R · týden -2.5 / -6.0 R',
  )
  expect(screen.getByTestId('library-brakes').textContent).toContain('stopy T7 1/2')
  expect(screen.getByTestId('library-trials').textContent).toContain('NQ T4 3/10')
  expect(screen.getByTestId('library-scope').textContent).toBe('okno 60 seancí · mechanika v5')
  expect(screen.getByTestId('library-disabled').textContent).toContain('Divergenční spring')
  // Souhrn žádá všechny tickery watchlistu (Knihovna není per symbol)
  await waitFor(() =>
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('symbols=ES%2CNQ'))).toBe(
      true,
    ),
  )
})

test('dialog: bez důvodu nejde uložit, čip důvod doplní, Stín se uloží jen s buňkou a důvodem', async () => {
  const fetchMock = mockApi()
  renderLibrary()
  fireEvent.click(await screen.findByRole('button', { name: /Stádium T7 Pokračování trendu · NQ/ }))
  const dialog = screen.getByRole('dialog', { name: 'T7 Pokračování trendu · NQ' })
  expect(dialog.textContent).toContain('platí: Auto (verze 1')
  const save = within(dialog).getByRole('button', { name: 'Uložit → verze 2' })
  // Platné stádium beze změny nejde uložit
  expect((save as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(within(dialog).getByRole('radio', { name: /Stín/ }))
  // Bez důvodu pořád ne — náhradní text neexistuje
  expect((save as HTMLButtonElement).disabled).toBe(true)
  expect(within(dialog).getByTestId('stage-note-required')).toBeDefined()
  // Mezery nejsou důvod
  fireEvent.change(within(dialog).getByLabelText('Důvod změny stádia'), {
    target: { value: '   ' },
  })
  expect((save as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(within(dialog).getByRole('button', { name: 'drawdown' }))
  expect((save as HTMLButtonElement).disabled).toBe(false)
  expect(dialog.textContent).toContain('push ne · mimo účet a brzdy')
  fireEvent.click(save)
  expect(await screen.findByTestId('library-message')).toBeDefined()
  expect(screen.getByTestId('library-message').textContent).toContain('verze 2')
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(stageCalls(fetchMock)).toEqual([
    { cell: 'NQ:trend_continuation', stage: 'shadow', note: 'drawdown', created_by: 'ui' },
  ])
})

test('dialog: Zkouška s výchozím rozpočtem, edge neprokázán, společné brzdy a meze rozpočtu', async () => {
  const fetchMock = mockApi()
  renderLibrary()
  fireEvent.click(await screen.findByRole('button', { name: /Stádium T7 Pokračování trendu · NQ/ }))
  const dialog = screen.getByRole('dialog', { name: 'T7 Pokračování trendu · NQ' })
  fireEvent.click(within(dialog).getByRole('radio', { name: /Zkouška/ }))
  const setups = within(dialog).getByLabelText(
    'Rozpočet zkoušky — počet setupů',
  ) as HTMLInputElement
  const lossR = within(dialog).getByLabelText('Rozpočet zkoušky — ztráta v R') as HTMLInputElement
  // Výchozí rozpočet z parametrů (10 setupů / 3 R)
  expect([setups.value, lossR.value]).toEqual(['10', '3'])
  expect(within(dialog).getByTestId('edge-unproven').textContent).toContain(
    'LB -0.14 ≤ 0 při n 163',
  )
  expect(within(dialog).getByTestId('brakes-shared').textContent).toContain(
    'Brzdy jsou společné napříč symboly (den −3.0 R, týden −6.0 R)',
  )
  fireEvent.click(within(dialog).getByRole('button', { name: 'test naživo' }))
  fireEvent.change(within(dialog).getByLabelText('Důvod změny stádia'), {
    target: { value: 'NQ T7 má čistě kladné ØR' },
  })
  const save = within(dialog).getByRole('button', { name: 'Uložit → verze 2' }) as HTMLButtonElement
  // Mimo meze 1–20 / 0.5–6 R se uložit nedá
  fireEvent.change(setups, { target: { value: '25' } })
  expect(save.disabled).toBe(true)
  fireEvent.change(setups, { target: { value: '2.5' } })
  expect(save.disabled).toBe(true)
  fireEvent.change(setups, { target: { value: '8' } })
  fireEvent.change(lossR, { target: { value: '7' } })
  expect(save.disabled).toBe(true)
  fireEvent.change(lossR, { target: { value: '2.5' } })
  expect(save.disabled).toBe(false)
  fireEvent.click(save)
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(stageCalls(fetchMock)).toEqual([
    {
      cell: 'NQ:trend_continuation',
      stage: 'trial',
      note: 'test naživo · NQ T7 má čistě kladné ØR',
      created_by: 'ui',
      budget_setups: 8,
      budget_r: 2.5,
    },
  ])
})

test('dialog: běžící zkouška se obnovuje, kladná LB štítek nemá, chyba serveru zůstane v dialogu', async () => {
  mockApi({
    stageResponse: { status: 422, body: { detail: 'Neznámá šablona' } },
  })
  renderLibrary()
  fireEvent.click(await screen.findByRole('button', { name: /Stádium T4 Gamma momentum · NQ/ }))
  const dialog = screen.getByRole('dialog', { name: 'T4 Gamma momentum · NQ' })
  // Platí Zkouška — opakované uložení = obnovení od nuly, předchozí čerpání je vidět
  expect(within(dialog).getByTestId('stage-previous-trial').textContent).toContain(
    'Běžící zkouška: 3/10 · -1.0 z -3.0 R',
  )
  expect(within(dialog).queryByTestId('edge-unproven')).toBeNull()
  fireEvent.click(within(dialog).getByRole('button', { name: 'jiný režim trhu' }))
  fireEvent.click(within(dialog).getByRole('button', { name: 'Obnovit zkoušku → verze 2' }))
  expect((await within(dialog).findByRole('alert')).textContent).toBe(
    'Uložení selhalo: Neznámá šablona',
  )
  // Escape dialog zavře bez zápisu
  fireEvent.keyDown(window, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).toBeNull()
})

test('zkouška skončená změnou mechaniky: Auto s důvodem v řádku, hlavičce i dialogu', async () => {
  const ended: LibraryCell = { ...NQ_T4, effective_stage: 'auto' }
  mockApi({ cells: [NQ_T7, ended] })
  renderLibrary()
  const button = await screen.findByRole('button', { name: /Stádium T4 Gamma momentum · NQ/ })
  expect(button.textContent).toContain('Auto (zkouška skončila)')
  expect(button.getAttribute('title')).toContain('zkouška začala na mechanice v5')
  // Čerpání skončené zkoušky v řádku není; hlavička říká proč
  expect(screen.getByTestId('library-row-NQ:gamma_momentum').textContent).not.toContain('3/10')
  expect(screen.getByTestId('library-trials').textContent).toContain(
    'NQ T4 3/10 · -1.0 z -3.0 R (jiná mechanika → Auto)',
  )
  fireEvent.click(button)
  expect(screen.getByTestId('stage-previous-trial').textContent).toContain(
    'Předchozí zkouška (jiná mechanika): 3/10',
  )
})

test('Knihovna bez buněk a starší API bez `cells` se hlásí nahlas', async () => {
  mockApi({ cells: [] })
  const first = renderLibrary()
  expect(await screen.findByTestId('library-empty')).toBeDefined()
  first.unmount()
  mockApi({ cells: undefined })
  renderLibrary()
  expect(await screen.findByTestId('library-unsupported')).toBeDefined()
})

test('podzáložky Přehled ↔ Knihovna', async () => {
  mockApi()
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(
    <AppStateProvider socket={socket}>
      <SetupsView />
    </AppStateProvider>,
  )
  expect(screen.getByRole('tab', { name: 'Přehled' }).getAttribute('aria-selected')).toBe('true')
  fireEvent.click(screen.getByRole('tab', { name: 'Knihovna' }))
  expect(await screen.findByRole('heading', { name: 'Knihovna setupů' })).toBeDefined()
  expect(window.localStorage.getItem('gexlens.setupsTab')).toBe('"library"')
})

test('Settings → Risk: bez důvodu nic, rozpočet zkoušky, bez checkboxu brány a bez stádií v těle', async () => {
  const fetchMock = mockApi()
  render(<RiskSettings />)
  expect(await screen.findByText(/Platná verze 1/)).toBeDefined()
  expect(screen.queryByLabelText('Brána šablon zapnuta')).toBeNull()
  expect((screen.getByLabelText('Zkouška: výchozí počet setupů') as HTMLInputElement).value).toBe(
    '10',
  )
  expect((screen.getByLabelText('Zkouška: výchozí ztráta (R)') as HTMLInputElement).value).toBe('3')
  const save = screen.getByRole('button', { name: 'Uložit jako novou verzi' }) as HTMLButtonElement
  expect(save.disabled).toBe(true)
  expect(screen.getByTestId('risk-note-required')).toBeDefined()
  fireEvent.change(screen.getByLabelText('Důvod změny risk parametrů'), {
    target: { value: 'kratší zkouška' },
  })
  fireEvent.change(screen.getByLabelText('Zkouška: výchozí počet setupů'), {
    target: { value: '5' },
  })
  expect(save.disabled).toBe(false)
  fireEvent.click(save)
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).includes('/setups/params') && (init as RequestInit)?.method === 'POST',
      ),
    ).toBe(true),
  )
  const post = (fetchMock.mock.calls as unknown as Array<[string, RequestInit | undefined]>).find(
    ([url, init]) => url.includes('/setups/params') && init?.method === 'POST',
  )
  const body = JSON.parse(String(post?.[1]?.body)) as {
    params: Record<string, unknown>
    note: string
  }
  expect(body.note).toBe('kratší zkouška')
  expect(body.params.trial_budget_setups).toBe(5)
  // Stádia mění jen POST /setups/stage — starý snímek je nesmí přepsat
  expect(body.params).not.toHaveProperty('shadow_cells')
  expect(body.params).not.toHaveProperty('trial_cells')
  expect(body.params.disabled_templates).toEqual(['divergence_spring'])
})
