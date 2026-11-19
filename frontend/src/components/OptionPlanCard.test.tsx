/** Karta plánu opce QQQ (F5a #1746): long a short, neobchodovat s důvodem, bez opce, živá osa,
výsledek F3 s variantou nad rozpočtem, chybějící PNG a nenastavený účet. Čísla jsou syntetická
(žádné hodnoty účtu ani skutečné plány). */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { OptionPlanCard } from './OptionPlanCard'
import type {
  OptionPlanMain,
  OptionPlanRow,
  OptionPlanVariant,
  OptionPlanVariantResult,
} from '../api/optionPlans'

const AT_915 = '2026-11-18T14:15:00+00:00'
const AT_931 = '2026-11-18T14:31:00+00:00'

function variant(
  role: 'hlavní' | 'záložní',
  right: 'C' | 'P',
  strike: number,
  bid: number,
  overrides: Partial<OptionPlanVariant> = {},
): OptionPlanVariant {
  return {
    role,
    right,
    strike,
    expiry: '2026-11-19',
    bid,
    ask: bid + 0.1,
    iv: 0.3,
    delta: right === 'C' ? 0.25 : -0.25,
    quote_ts: AT_915,
    at_targets: [bid + 0.5, bid + 0.9],
    source: 'tastytrade',
    pricing: null,
    verdict: 'čeká',
    reasons: ['čeká na vstup A (flip 500,10)'],
    ...overrides,
  }
}

function main(
  direction: 'long' | 'short',
  overrides: Partial<OptionPlanMain> = {},
): OptionPlanMain {
  const right = direction === 'long' ? 'C' : 'P'
  const strike = direction === 'long' ? 505 : 495
  return {
    rules: 'hlavní',
    direction,
    right,
    created_at: AT_915,
    targets: direction === 'long' ? [503.5, 505.25] : [496.5, 494.75],
    target_ts: ['2026-11-18T16:30:00+00:00', '2026-11-18T21:00:00+00:00'],
    exit_ts: '2026-11-18T20:45:00+00:00',
    level_a: ['flip', 500.1],
    entry: null,
    variants: [variant('hlavní', right, strike, 1.2), variant('záložní', right, strike + 1, 1.0)],
    missing: [],
    p5: 'SL se určí při spoušti vstupu A, pak podmíněný příkaz v TWS.',
    model_label: 'model z naměřené IV',
    verdict: 'čeká — čeká na vstup A (flip 500,10)',
    ...overrides,
  }
}

function plan(
  overrides: Partial<OptionPlanRow> = {},
  planMain: OptionPlanMain | null = main('long'),
) {
  const row: OptionPlanRow = {
    id: 7,
    scenario_id: 70,
    day: '2026-11-18',
    created_at: AT_915,
    rules_version: 1,
    status: planMain ? 'sestaven' : 'bez opce',
    verdict: planMain ? 'čeká' : null,
    evaluated_at: null,
    result: null,
    has_image: true,
    live_alerts: [],
    effective: {
      slot: '9:15',
      main: planMain,
      prices: null,
      reasons: [],
      repricing_reasons: null,
    },
    ...overrides,
  }
  return row
}

const fetchMock = vi.fn()

function imageOk() {
  fetchMock.mockImplementation(() =>
    Promise.resolve({ ok: true, status: 200, blob: () => Promise.resolve(new Blob(['png'])) }),
  )
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  fetchMock.mockReset()
  imageOk()
  // jsdom objectURL nemá
  URL.createObjectURL = vi.fn(() => 'blob:plan-7')
  URL.revokeObjectURL = vi.fn()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

test('long před spouští: call, kotace, model v TP, vstup A; SL a R:R až při spoušti', async () => {
  const { container } = render(<OptionPlanCard plan={plan()} accountMissing={false} />)
  const text = container.textContent ?? ''
  expect(text).toContain('long → call 505 · exp. 19. 11.')
  expect(text).toContain('čeká — čeká na vstup A (flip 500,10)')
  expect(text).toContain('1.20 / 1.30 · IV 30.0 % · Δ 0.25')
  expect(text).toContain('1.70 / 2.10') // model v TP1 / TP2 z engine
  expect(text).toContain('Vstup A: odmítnutí od flip 500.10')
  expect(text).toContain('vstup B jen stín v track recordu')
  expect(text).toContain('až při spoušti vstupu A')
  expect(text).toContain('čeká na 9:31 ET')
  expect(text).not.toMatch(/R:R \d/) // K12 A: žádný předběžný odhad
  expect(screen.queryByTestId('option-plan-account-missing')).toBeNull()
  expect((await screen.findByTestId('option-plan-image')).getAttribute('src')).toBe('blob:plan-7')
  expect(fetchMock).toHaveBeenCalledWith('/api/option-plans/7/image', { cache: 'no-cache' })
})

test('short s přeceněním v 9:31: put a obě sady kotací', () => {
  const repriced = variant('hlavní', 'P', 495, 1.4, { quote_ts: AT_931 })
  const row = plan(
    {
      effective: {
        slot: '9:15',
        main: main('short'),
        prices: { main: [repriced] },
        reasons: [],
        repricing_reasons: [],
      },
    },
    main('short'),
  )
  const { container } = render(<OptionPlanCard plan={row} accountMissing={false} />)
  const hlavni = screen.getByTestId('option-plan-variant-hlavní').textContent ?? ''
  expect(container.textContent).toContain('short → put 495')
  expect(hlavni).toContain('1.20 / 1.30')
  expect(hlavni).toContain('1.40 / 1.50')
  expect(hlavni).toContain('1.90 / 2.30') // model z 9:31
  expect(screen.getByTestId('option-plan-variant-záložní').textContent).toContain('nezapsáno')
})

test('neobchodovat s důvodem je viditelně označené', () => {
  const reasons = ['rozpočet nenastaven (chybí rozpočet na obchod a velikost účtu)']
  const blocked = main('short', {
    verdict: `neobchodovat — ${reasons[0]}`,
    variants: [variant('hlavní', 'P', 495, 1.2, { verdict: 'neobchodovat', reasons })],
  })
  const { container } = render(
    <OptionPlanCard plan={plan({ verdict: 'neobchodovat' }, blocked)} accountMissing />,
  )
  const verdict = container.querySelector('.option-plan-verdict')
  expect(verdict?.classList.contains('option-plan-stop')).toBe(true)
  expect(verdict?.textContent).toContain('neobchodovat — rozpočet nenastaven')
  expect(screen.getByTestId('option-plan-account-missing').textContent).toContain('Settings')
})

test('plán bez opce ukáže důvody z 9:15 i z 9:31', () => {
  const row = plan(
    {
      effective: {
        slot: '9:15',
        main: null,
        prices: null,
        reasons: ['ranní snímek neúplný'],
        repricing_reasons: ['chybí kotace 1DTE'],
      },
    },
    null,
  )
  render(<OptionPlanCard plan={row} accountMissing={false} />)
  expect(screen.getByTestId('option-plan-no-option').textContent).toBe(
    'Bez opce: ranní snímek neúplný; chybí kotace 1DTE',
  )
  expect(screen.queryByTestId('option-plan-live')).toBeNull()
})

test('živá osa z odeslaných přechodů; starší API bez live_alerts řekne, kde průběh je', () => {
  const alerts = [
    { event: 'vstup', ts: '2026-11-18T15:35:00+00:00', sent_at: AT_931, final: false },
    { event: 'TP1', ts: '2026-11-18T17:31:00+00:00', sent_at: AT_931, final: true },
  ]
  const { unmount } = render(
    <OptionPlanCard plan={plan({ live_alerts: alerts })} accountMissing={false} />,
  )
  expect(screen.getByTestId('option-plan-live').textContent).toMatch(
    /vstup A \d\d:\d\d · TP1 \d\d:\d\d \(konec\)/,
  )
  unmount()
  render(<OptionPlanCard plan={plan({ live_alerts: undefined })} accountMissing={false} />)
  expect(screen.getByTestId('option-plan-live').textContent).toContain('API průběh zatím nevrací')
})

test('výsledek F3: vyřazeno P3 hypoteticky, varianta nad rozpočtem označená, naměřené P&L', () => {
  const entry = {
    kind: 'A' as const,
    ts: '2026-11-18T14:50:00+00:00',
    price: 498.5,
    stop: 500.75,
    level: 'prev_low',
    level_price: 499.9,
  }
  const rejected: OptionPlanVariantResult = {
    role: 'hlavní',
    right: 'P',
    strike: 495,
    expiry: '2026-11-19',
    kind: 'vyřazeno P3',
    entry_quote: { ts_min: entry.ts, bid: 1.5, ask: 1.6 },
    pricing: {
      paid: 1.6,
      at_stop: 1.1,
      at_tp1: 1.9,
      at_tp2: 2.0,
      rr: 0.6,
      rr_strict: 0.4,
      loss_at_stop_usd: -50,
      drawdown_usd: -90,
      pnl_tp1_usd: 30,
      pnl_tp2_usd: 40,
    },
    budget: 'nad rozpočtem',
    exit_tp1: {
      event: 'TP1',
      ts: '2026-11-18T15:00:00+00:00',
      bid: 2.0,
      quote_age_s: 1,
      pnl_usd: 40,
    },
    exit_hold: null,
    gaps: [],
  }
  const row = plan(
    {
      evaluated_at: '2026-11-18T21:15:00+00:00',
      result: {
        rules_version: 1,
        main: {
          rules: 'hlavní',
          status: 'spuštěno',
          reasons: [],
          entry,
          outcome: 'TP1',
          events: [{ kind: 'vstup', ts: entry.ts, price: 498.5 }],
          variants: [rejected],
        },
        shadows: {},
      },
    },
    main('short'),
  )
  const { container } = render(<OptionPlanCard plan={row} accountMissing={false} />)
  const result = screen.getByTestId('option-plan-result').textContent ?? ''
  expect(result).toContain('Výsledek: spuštěno → TP1')
  expect(result).toContain('za 498.50 od PDL 499.90 · SL 500.75')
  const variantRow = screen.getByTestId('option-plan-result-hlavní')
  expect(variantRow.classList.contains('option-plan-rejected')).toBe(true)
  expect(variantRow.textContent).toContain('vyřazeno P3 — hypoteticky')
  expect(variantRow.textContent).toContain('R:R 0.60')
  expect(variantRow.textContent).toContain('ztráta při SL −50 $')
  expect(variantRow.textContent).toContain('TP1 ')
  expect(variantRow.textContent).toContain('+40 $')
  expect(variantRow.querySelector('.option-plan-over')?.textContent).toBe('⚠ nad rozpočtem')
  expect(screen.queryByTestId('option-plan-live')).toBeNull() // po vyhodnocení platí výsledek
  expect(container.querySelector('.scenario-verdict')?.textContent).toBe('spuštěno → TP1')
})

test('chybějící PNG: důvod ze serveru, žádný prázdný obrázek', async () => {
  fetchMock.mockImplementation(() =>
    Promise.resolve({
      ok: false,
      status: 500,
      json: () => Promise.resolve({ detail: 'Kreslení PNG plánu 7 selhalo: bary chybí' }),
    }),
  )
  render(<OptionPlanCard plan={plan({ has_image: false })} accountMissing={false} />)
  expect((await screen.findByTestId('option-plan-image-error')).textContent).toBe(
    'Graf plánu se nenačetl: HTTP 500: Kreslení PNG plánu 7 selhalo: bary chybí',
  )
  expect(screen.queryByTestId('option-plan-image')).toBeNull()
})

test('nenačtené Settings = výslovná hláška; kompaktní řádek nese stav a nic nenačítá', async () => {
  const { unmount } = render(<OptionPlanCard plan={plan()} accountMissing={null} />)
  expect(screen.getByTestId('option-plan-account-unknown').textContent).toContain('nenačetlo')
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  unmount()
  fetchMock.mockClear()
  render(<OptionPlanCard plan={plan({ id: 3 })} compact accountMissing />)
  expect(screen.getByTestId('option-plan-line-3').textContent).toBe(
    'Plán opce QQQ · 18. 11. · long → call 505 · exp. 19. 11. · čeká',
  )
  expect(screen.queryByTestId('option-plan-account-missing')).toBeNull()
  expect(fetchMock).not.toHaveBeenCalled()
})

test('sbalený řádek jde rozbalit na celou kartu s grafem a zase sbalit (Q2 C)', async () => {
  render(<OptionPlanCard plan={plan({ id: 3 })} compact accountMissing />)
  const toggle = screen.getByTestId('option-plan-toggle-3')
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  fireEvent.click(toggle)
  expect(screen.queryByTestId('option-plan-line-3')).toBeNull()
  expect(screen.getByTestId('option-plan-account-missing')).toBeTruthy()
  expect(screen.getByTestId('option-plan-3').textContent).toContain('vstup A')
  await screen.findByTestId('option-plan-image')
  expect(fetchMock).toHaveBeenCalledTimes(1)
  const collapse = screen.getByTestId('option-plan-toggle-3')
  expect(collapse.getAttribute('aria-expanded')).toBe('true')
  fireEvent.click(collapse)
  expect(screen.getByTestId('option-plan-line-3')).toBeTruthy()
  expect(screen.queryByTestId('option-plan-image')).toBeNull()
})

test('celá karta bez compact tlačítko sbalit nemá', () => {
  render(<OptionPlanCard plan={plan()} accountMissing={false} />)
  expect(screen.queryByTestId('option-plan-toggle-7')).toBeNull()
})

test('stáří živé osy: čas načtení seznamu u řádku Průběh živě', () => {
  const loadedAt = Date.parse('2026-11-18T16:02:00+00:00')
  render(<OptionPlanCard plan={plan()} accountMissing={false} loadedAt={loadedAt} />)
  expect(screen.getByTestId('option-plan-live').textContent).toMatch(
    /^Průběh živě \(stav k \d\d:\d\d\): zatím bez přechodu/,
  )
})

test('PNG se načte znovu po změně stavu plánu (vyhodnocení), jinak ne', async () => {
  const { rerender } = render(<OptionPlanCard plan={plan()} accountMissing={false} />)
  await screen.findByTestId('option-plan-image')
  rerender(<OptionPlanCard plan={plan({ live_alerts: [] })} accountMissing={false} />)
  expect(fetchMock).toHaveBeenCalledTimes(1) // tentýž stav = žádný nový dotaz
  rerender(
    <OptionPlanCard
      plan={plan({ evaluated_at: '2026-11-18T21:15:00+00:00' })}
      accountMissing={false}
    />,
  )
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
})
