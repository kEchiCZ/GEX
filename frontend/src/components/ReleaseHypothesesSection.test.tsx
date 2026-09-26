/** Stats → hypotézy releasů (#1296): stavy a chipy, tooltip po řádcích, prázdný stav, chyba. */
import { render, screen, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { ReleaseHypothesesSection } from './ReleaseHypothesesSection'
import { headingTooltip } from '../api/releases'
import type { HypothesisSymbolState, ReleaseHypotheses } from '../api/releases'

function state(overrides: Partial<HypothesisSymbolState> = {}): HypothesisSymbolState {
  return {
    historical: { hits: 13, n: 14 },
    hits: 0,
    n: 0,
    wilson_lb: null,
    wilson_ub: null,
    status: 'testing',
    decided_at_n: null,
    next_checkpoint: 10,
    outcomes: [],
    computed_at: null,
    ...overrides,
  }
}

function payload(live = false): ReleaseHypotheses {
  return {
    registered_at: '2026-10-01T00:00:00+00:00',
    criteria: {
      checkpoints: [10, 20, 30],
      confidence: 0.95,
      verified: 'dolní mez Wilsonova 95% intervalu nad 50 %',
      rejected: 'horní mez pod 50 %, nebo n = 30 bez ověření',
    },
    hypotheses: [
      {
        id: 'H1',
        label: 'Teplejší jádro inflace → za 15 min níž',
        rule: 'Headline Core CPI/PPI/PCE m/m s překvapením nad odhadem',
        in_preview: 'testing',
        symbols: {
          ES: state(),
          NQ: live
            ? state({
                status: 'rejected',
                hits: 1,
                n: 10,
                wilson_lb: 0.018,
                wilson_ub: 0.404,
                decided_at_n: 10,
                next_checkpoint: null,
                outcomes: [
                  {
                    cluster_ts: '2026-10-14T12:30:00+00:00',
                    family: 'CPI',
                    hit: false,
                    value_bp: 12.3,
                  },
                ],
              })
            : state(),
        },
      },
      {
        id: 'M1',
        label: 'Výchylka za 15 min nad běžným dnem',
        rule: 'Rodiny CPI, NFP, FOMC, PPI, PCE',
        in_preview: 'verified',
        symbols: {
          ES: live
            ? state({
                status: 'verified',
                hits: 10,
                n: 10,
                wilson_lb: 0.722,
                wilson_ub: 1,
                decided_at_n: 10,
                next_checkpoint: null,
                historical: { hits: 107, n: 111 },
              })
            : state({ historical: { hits: 107, n: 111 } }),
        },
      },
    ],
  }
}

function stubFetch(response: { ok: boolean; status: number; body?: unknown }) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: response.ok,
      status: response.status,
      json: async () => response.body ?? {},
    })),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

test('bez živého releasu: prázdný stav, všechno ověřuje se, historie popisně', async () => {
  stubFetch({ ok: true, status: 200, body: payload() })
  render(<ReleaseHypothesesSection />)
  expect((await screen.findByTestId('release-hypotheses-empty')).textContent).toContain(
    'Živě zatím žádný hodnocený release (registrace 1. 10. 2026)',
  )
  const row = screen.getByTestId('hyp-H1-ES')
  expect(within(row).getByText('ověřuje se').className).toContain('hypothesis-testing')
  expect(row.textContent).toContain('0/0')
  expect(row.textContent).toContain('další při 10')
  expect(row.textContent).toContain('13/14')
})

test('ověřeno zeleně, zamítnuto červeně, interval a poslední výsledky', async () => {
  stubFetch({ ok: true, status: 200, body: payload(true) })
  render(<ReleaseHypothesesSection />)
  const verified = await screen.findByTestId('hyp-M1-ES')
  expect(within(verified).getByText('ověřeno').className).toContain('hypothesis-verified')
  expect(verified.textContent).toContain('72 %–100 %')
  expect(verified.textContent).toContain('rozhodnuto při 10')
  const rejected = screen.getByTestId('hyp-H1-NQ')
  expect(within(rejected).getByText('zamítnuto').className).toContain('hypothesis-rejected')
  expect(rejected.textContent).toContain('14. 10. 2026 CPI ✘ 12.3 bp')
  expect(screen.queryByTestId('release-hypotheses-empty')).toBeNull()
})

test('tooltip nadpisu jsou odrážky po řádcích', async () => {
  stubFetch({ ok: true, status: 200, body: payload() })
  render(<ReleaseHypothesesSection />)
  await screen.findByTestId('release-hypotheses')
  const heading = screen.getByRole('heading', { name: /předem registrované hypotézy/ })
  const lines = (heading.getAttribute('title') ?? '').split('\n')
  expect(lines[0]).toBe('Předem registrované hypotézy (ADR-0044)')
  expect(lines).toContain('• Rozhoduje se při n = 10, 20, 30')
  expect(lines).toContain('• Pravděpodobnost v upozornění jen po ověření')
  expect(headingTooltip(payload())).toContain('• Počítají se jen releasy od 1. 10. 2026')
})

test('chyba API je vidět, ne prázdná tabulka', async () => {
  stubFetch({ ok: false, status: 500 })
  render(<ReleaseHypothesesSection />)
  expect((await screen.findByTestId('release-hypotheses-error')).textContent).toContain('HTTP 500')
  expect(screen.queryByTestId('release-hypotheses')).toBeNull()
})
