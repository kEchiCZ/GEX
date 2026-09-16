/** Order ticket a chip paper účtu (#1187 fáze 2). */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { PaperChip } from './PaperChip'
import { PaperTicketDialog } from './PaperTicketDialog'
import type { PaperAccount, PaperOrderRow } from '../api/paper'

const ACCOUNT: PaperAccount = {
  id: 1,
  name: 'paper',
  equity_start: 50000,
  equity: 50000,
  halted: false,
  halted_reason: null,
  risk_pct: 1,
  risk_max_pct: 2,
  risk_budget_usd: 500,
  day_r: 0,
  week_r: 0,
  brake: null,
  daily_brake_r: 3,
  weekly_brake_r: 6,
  open: [],
  working: [],
}

const OPEN: PaperOrderRow = {
  id: 3,
  symbol: 'ES',
  side: 'long',
  qty: 1,
  order_type: 'limit',
  entry_price: 7600,
  stop_price: 7592,
  target_price: 7616,
  status: 'open',
  created_ts: '2026-09-17T14:00:00+00:00',
  filled_ts: '2026-09-17T14:01:00+00:00',
  fill_price: 7600,
  closed_ts: null,
  exit_price: null,
  exit_reason: null,
  pnl_usd: null,
  fees_usd: null,
  r_multiple: null,
  risk_usd: 400,
  point_value: 50,
  setup_key: null,
  note: null,
  close_requested: false,
  mfe: null,
  mae: null,
}

afterEach(() => vi.restoreAllMocks())

test('ticket předvyplní entry ze spotu, spočítá max kontrakty a podá order', async () => {
  const calls: Array<{ url: string; body: unknown }> = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown, init?: RequestInit) => {
      const target = String(url)
      if (target.includes('/playbook')) {
        return {
          ok: true,
          json: async () => ({
            playbook: [{ id: 1, key: 'failed_break', name: 'Neúspěšný průraz' }],
          }),
        }
      }
      calls.push({ url: target, body: init?.body ? JSON.parse(String(init.body)) : null })
      return { ok: true, json: async () => ({ ...OPEN, status: 'working' }) }
    }),
  )
  const onPlaced = vi.fn()
  render(
    <PaperTicketDialog
      symbol="ES"
      account={ACCOUNT}
      spot={7600}
      onPlaced={onPlaced}
      onCancel={() => {}}
    />,
  )
  // Výchozí: entry 7600, stop 7590 (−10 b ES), cíl 7620 → 10 b × 50 = 500 $ = 1 ks, RRR 2
  expect((screen.getByLabelText('Entry') as HTMLInputElement).value).toBe('7600')
  expect((screen.getByLabelText('Stop') as HTMLInputElement).value).toBe('7590')
  expect(screen.getByTestId('paper-sizing').textContent).toBe('riziko 500 $ · max 1 ks · RRR 2.0')
  // Delší stop → nad rozpočtem, tlačítko zakázané
  fireEvent.change(screen.getByLabelText('Stop'), { target: { value: '7580' } })
  expect(screen.getByTestId('paper-sizing').textContent).toContain('max 0 ks')
  expect((screen.getByTestId('paper-submit') as HTMLButtonElement).disabled).toBe(true)
  // Zpět na 8 b, SHORT přepne úrovně
  fireEvent.change(screen.getByLabelText('Stop'), { target: { value: '7592' } })
  expect((screen.getByTestId('paper-submit') as HTMLButtonElement).disabled).toBe(false)
  await waitFor(() =>
    expect(screen.getByLabelText('Setup').querySelectorAll('option').length).toBe(2),
  )
  fireEvent.change(screen.getByLabelText('Setup'), { target: { value: 'failed_break' } })
  fireEvent.click(screen.getByTestId('paper-submit'))
  await waitFor(() => expect(onPlaced).toHaveBeenCalled())
  // Před podáním se stáhne snímek kontextu (#932) — order je poslední volání
  const placed = calls.find((call) => call.url.includes('/paper/orders'))
  expect(placed).toBeDefined()
  expect(placed?.body).toMatchObject({
    symbol: 'ES',
    side: 'long',
    qty: 1,
    order_type: 'limit',
    entry_price: 7600,
    stop_price: 7592,
    target_price: 7620,
    setup_key: 'failed_break',
  })
})

test('ticket ukáže důvod blokace ze serveru', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('/playbook'))
        return { ok: true, json: async () => ({ playbook: [] }) }
      return {
        ok: false,
        status: 409,
        json: async () => ({ detail: { block: 'position_exists', reason: 'ES už má order' } }),
      }
    }),
  )
  render(
    <PaperTicketDialog
      symbol="ES"
      account={ACCOUNT}
      spot={7600}
      onPlaced={() => {}}
      onCancel={() => {}}
    />,
  )
  fireEvent.click(screen.getByTestId('paper-submit'))
  expect(
    await screen.findByText(/na symbolu už je pozice nebo čekající order: ES už má order/),
  ).toBeDefined()
})

test('chip ukazuje pozici symbolu, zavření volá DELETE, kill switch odblokování', async () => {
  const urls: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown, init?: RequestInit) => {
      urls.push(`${init?.method ?? 'GET'} ${String(url)}`)
      return { ok: true, json: async () => ({}) }
    }),
  )
  const onChanged = vi.fn()
  render(<PaperChip account={{ ...ACCOUNT, open: [OPEN] }} symbol="ES" onChanged={onChanged} />)
  expect(screen.getByTestId('paper-position').textContent).toContain(
    'LONG 1× ES @ 7600 · stop 7592 · cíl 7616',
  )
  fireEvent.click(screen.getByText('✕ Zavřít'))
  await waitFor(() => expect(onChanged).toHaveBeenCalled())
  expect(urls[0]).toMatch(/^DELETE .*\/paper\/orders\/3$/)
  // Jiný symbol: pozice se nekreslí, jen počet „jinde"
  render(<PaperChip account={{ ...ACCOUNT, open: [OPEN] }} symbol="NQ" onChanged={onChanged} />)
  expect(screen.getByText('+1 jinde')).toBeDefined()
  // Halted účet nabízí odblokování
  render(
    <PaperChip
      account={{ ...ACCOUNT, halted: true, halted_reason: 'test' }}
      symbol="ES"
      onChanged={onChanged}
    />,
  )
  expect(screen.getByText('Odblokovat')).toBeDefined()
  render(<PaperChip account={null} symbol="ES" onChanged={onChanged} />)
})
