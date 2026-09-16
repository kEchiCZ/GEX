/** Panel kouče v Deníku (#933): skóre, příznaky s důkazem, týdenní pravidla. */
import { render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { CoachPanel } from './CoachPanel'
import { formatR, scoreTone } from '../api/coach'

const DAILY = {
  session_day: '2026-09-17',
  rules_version: 1,
  n: 2,
  score: 75,
  total_r: 1.0,
  flagged_cost_r: -1.0,
  summary:
    '2 obchodů, +1.00 R, disciplína 75/100. Příznaky u 1 obchodů: vstup krátce po stopu (revenge).',
  trades: [
    {
      id: 1,
      symbol: 'ES',
      direction: 'long',
      opened_ts: '2026-09-17T14:00:00+00:00',
      closed_ts: '2026-09-17T14:30:00+00:00',
      setup_key: 'failed_break',
      paper: true,
      exit_reason: 'target',
      realized_r: 2.0,
      planned_rr: 2.0,
      capture: 0.94,
      net_pnl: 790,
      flags: [],
      penalty: 0,
    },
    {
      id: 2,
      symbol: 'ES',
      direction: 'short',
      opened_ts: '2026-09-17T14:33:00+00:00',
      closed_ts: '2026-09-17T14:40:00+00:00',
      setup_key: null,
      paper: true,
      exit_reason: 'stop',
      realized_r: -1.0,
      planned_rr: 2.0,
      capture: 0,
      net_pnl: -410,
      flags: [
        {
          kind: 'revenge',
          label: 'vstup krátce po stopu (revenge)',
          detail: 'vstup 3 min po stopu #1',
          cost_r: -1.0,
        },
      ],
      penalty: 15,
    },
  ],
}

const WEEKLY = {
  week_start: '2026-09-14',
  week_end: '2026-09-18',
  rules_version: 1,
  n: 2,
  total_r: 1.0,
  win_rate: 0.5,
  avg_r: 0.5,
  score: 75,
  capture: 0.47,
  flags: { revenge: { n: 1, cost_r: -1.0 } },
  by_hour_utc: { '14': { n: 2, sum_r: 1.0 } },
  days: [],
  rules: [
    {
      kind: 'revenge',
      advice: 'Po stopu 15 minut pauza — žádný nový vstup na stejný symbol.',
      n: 1,
      cost_r: -1.0,
    },
  ],
}

afterEach(() => vi.restoreAllMocks())

test('panel ukáže skóre, obchody s příznaky a týdenní pravidla', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => ({
      ok: true,
      json: async () => (String(url).includes('/weekly') ? WEEKLY : DAILY),
    })),
  )
  render(<CoachPanel date="2026-09-17" symbol="" />)
  expect((await screen.findByTestId('coach-score')).textContent).toBe('75/100')
  expect(screen.getByTestId('coach-summary').textContent).toContain('disciplína 75/100')
  const trades = screen.getAllByTestId('coach-trade')
  expect(trades.length).toBe(2)
  expect(trades[0].textContent).toContain('✓ bez příznaků')
  expect(trades[1].textContent).toContain('⚠ vstup krátce po stopu (revenge) (-1.00 R)')
  expect(screen.getByTestId('coach-weekly').textContent).toContain('Po stopu 15 minut pauza')
})

test('bez API se panel nekreslí jako chyba, pomocné funkce', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: false, json: async () => ({}) })),
  )
  render(<CoachPanel date="" symbol="ES" />)
  expect(await screen.findByText('Kouč není dostupný (API).')).toBeDefined()
  expect(formatR(-1.5)).toBe('-1.50 R')
  expect(formatR(null)).toBe('—')
  expect(scoreTone(90)).toBe('good')
  expect(scoreTone(70)).toBe('warn')
  expect(scoreTone(10)).toBe('bad')
})
