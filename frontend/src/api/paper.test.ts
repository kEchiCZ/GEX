/** Paper účet — klient a sizing (#1187). */
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  fetchPaperAccount,
  maxContracts,
  orderLabel,
  placePaperOrder,
  rewardRisk,
  riskUsd,
} from './paper'
import type { PaperOrderRow } from './paper'

const ORDER: PaperOrderRow = {
  id: 7,
  symbol: 'ES',
  side: 'long',
  qty: 1,
  order_type: 'limit',
  entry_price: 7600,
  stop_price: 7592,
  target_price: 7616,
  status: 'working',
  created_ts: '2026-09-17T14:00:00+00:00',
  filled_ts: null,
  fill_price: null,
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

describe('sizing', () => {
  it('max kontraktů z rozpočtu, riziko a RRR', () => {
    expect(maxContracts(500, 7600, 7592, 50)).toBe(1) // 8 b × 50 = 400 ≤ 500
    expect(maxContracts(500, 7600, 7596, 50)).toBe(2) // 200 $/ks
    expect(maxContracts(500, 7600, 7586, 50)).toBe(0) // 700 > 500
    expect(maxContracts(500, 7600, 7600, 50)).toBe(0)
    expect(riskUsd(2, 29000, 28975, 20)).toBe(1000)
    expect(rewardRisk(7600, 7592, 7616)).toBe(2)
    expect(rewardRisk(7600, 7592, null)).toBeNull()
  })

  it('popisek pozice', () => {
    expect(orderLabel(ORDER)).toBe('LONG 1× ES @ 7600 (limit čeká) · stop 7592 · cíl 7616')
    expect(orderLabel({ ...ORDER, status: 'open', fill_price: 7599.75, target_price: null })).toBe(
      'LONG 1× ES @ 7599.75 · stop 7592',
    )
  })
})

describe('klient', () => {
  afterEach(() => vi.restoreAllMocks())

  it('účet: validace tvaru; 409 z podání se přeloží na důvod', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ equity: 50000, risk_budget_usd: 500 }),
      })),
    )
    const account = await fetchPaperAccount()
    expect(account?.equity).toBe(50000)
    expect(account?.open).toEqual([])
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 409,
        json: async () => ({
          detail: { block: 'daily_brake', reason: 'brzda: dnes -3.0 R', max_contracts: 0 },
        }),
      })),
    )
    const blocked = await placePaperOrder({
      symbol: 'ES',
      side: 'long',
      qty: 1,
      order_type: 'limit',
      entry_price: 7600,
      stop_price: 7592,
      target_price: null,
      setup_key: null,
      note: null,
    })
    expect(blocked.ok).toBe(false)
    if (!blocked.ok) {
      expect(blocked.block?.block).toBe('daily_brake')
      expect(blocked.error).toBe('denní brzda: brzda: dnes -3.0 R')
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, json: async () => ({}) })),
    )
    expect(await fetchPaperAccount()).toBeNull()
  })
})
