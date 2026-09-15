/** Scénář dne (#1173): cíle z geometrie, převod anotace, dialog, karta, úklid v Settings. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { pathFromAnnotation, targetsFromPath } from '../api/scenarios'
import type { Scenario } from '../api/scenarios'
import { ScenarioCard } from './ScenarioCard'
import { ScenarioDialog } from './ScenarioDialog'
import { ScenarioSettings } from './ScenarioSettings'

afterEach(() => {
  vi.unstubAllGlobals()
})

test('cíle z geometrie: obraty + konec, chvění se slučuje (zrcadlo enginu)', () => {
  const base = Date.parse('2026-09-15T12:00:00Z')
  const at = (minutes: number, price: number) => ({
    ts: new Date(base + minutes * 60_000).toISOString(),
    price,
  })
  expect(
    targetsFromPath(
      [at(0, 7600), at(30, 7640), at(60, 7680), at(61, 7679.5), at(90, 7640), at(120, 7600)],
      7600,
    ),
  ).toEqual([7680, 7600])
  expect(targetsFromPath([at(0, 7600), at(60, 7650)], 7600)).toEqual([7650])
  expect(targetsFromPath([], 7600)).toEqual([])
})

test('anotace (minuta osy × strike) → absolutní čas × cena', () => {
  const minutesIso = ['2026-09-15T12:00:00Z', '2026-09-15T12:01:00Z']
  const path = pathFromAnnotation(
    {
      tool: 'arrow',
      color: '#fff',
      points: [
        { minute: 0, strike: 7600.123 },
        { minute: 90.5, strike: 7680 },
      ],
    },
    minutesIso,
  )
  expect(path).toEqual([
    { ts: '2026-09-15T12:00:00.000Z', price: 7600.12 },
    { ts: '2026-09-15T13:30:30.000Z', price: 7680 },
  ])
  expect(pathFromAnnotation({ tool: 'arrow', color: '#fff', points: [] }, [])).toEqual([])
})

test('dialog: cíle jdou upravit, termín nesmí být před dneškem, potvrzení nese draft', () => {
  const onConfirm = vi.fn()
  render(
    <ScenarioDialog
      symbol="ES"
      entry={7600}
      path={[{ ts: '2026-09-15T12:00:00Z', price: 7600 }]}
      suggestedTargets={[7680, 7600]}
      today="2026-09-15"
      busy={false}
      error={null}
      onConfirm={onConfirm}
      onCancel={() => undefined}
    />,
  )
  const targets = screen.getByLabelText('Cíle scénáře') as HTMLInputElement
  expect(targets.value).toBe('7680, 7600')
  fireEvent.change(targets, { target: { value: '7690; 7610' } })
  fireEvent.change(screen.getByLabelText('Termín scénáře'), { target: { value: '2026-09-14' } })
  const save = screen.getByRole('button', { name: 'Uložit scénář' }) as HTMLButtonElement
  expect(save.disabled).toBe(true) // termín v minulosti
  fireEvent.change(screen.getByLabelText('Termín scénáře'), { target: { value: '2026-09-18' } })
  fireEvent.change(screen.getByLabelText('Poznámka scénáře'), {
    target: { value: ' odraz od zdi ' },
  })
  fireEvent.click(save)
  expect(onConfirm).toHaveBeenCalledWith({
    targets: [7690, 7610],
    deadline: '2026-09-18',
    note: 'odraz od zdi',
  })
})

const SCENARIO: Scenario = {
  id: 7,
  symbol: 'NQ',
  day: '2026-09-15',
  created_at: '2026-09-15T12:00:00Z',
  deadline: '2026-09-15',
  deadline_ts: '2026-09-15T20:00:00Z',
  entry: 29000,
  targets: [29100, 28950],
  path: [],
  annotation_id: 3,
  note: null,
  has_image: true,
  image_bytes: 1000,
  evaluated_at: '2026-09-15T20:16:00Z',
  result: {
    hit1: true,
    hit2: false,
    order_ok: false,
    hit1_ts: '2026-09-15T14:00:00Z',
    hit2_ts: null,
    max_dev_pts: 42.5,
    max_dev_em: 0.31,
    bars: 480,
    verdict: 'partial',
  },
}

test('karta ukazuje verdikt, výsledek a snímek', () => {
  render(<ScenarioCard scenario={SCENARIO} />)
  expect(screen.getByText('částečně')).toBeDefined()
  expect(screen.getByTestId('scenario-result-7').textContent).toContain(
    'cíl 1 ano · cíl 2 ne · pořadí ne · max. odchylka 42.5 b (0.31 EM)',
  )
  expect((screen.getByAltText('Snímek scénáře 7') as HTMLImageElement).src).toContain(
    '/scenarios/7/image',
  )
  render(<ScenarioCard scenario={{ ...SCENARIO, id: 8, result: null, evaluated_at: null }} />)
  expect(screen.getByText('čeká na termín')).toBeDefined()
})

test('Settings: obsazení disku a úklid s potvrzením', async () => {
  const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
    if (init?.method === 'DELETE') {
      expect(String(url)).toContain('older_than_days=14')
      return { ok: true, json: async () => ({ removed: 3, freed_bytes: 3_000_000 }) }
    }
    return {
      ok: true,
      json: async () => ({
        bytes: 1_200_000_000,
        scenarios: 40,
        images: 38,
        limit_bytes: 1_073_741_824,
        over_limit: true,
      }),
    }
  })
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal(
    'confirm',
    vi.fn(() => true),
  )
  render(<ScenarioSettings />)
  await waitFor(() =>
    expect(screen.getByTestId('scenario-disk').textContent).toContain('NAD LIMITEM'),
  )
  fireEvent.change(screen.getByLabelText('Stáří snímků k úklidu (dní)'), {
    target: { value: '14' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Pročistit snímky' }))
  await waitFor(() => expect(screen.getByText(/Smazáno 3 snímků/)).toBeDefined())
})
