/** Test chipu tendence (#350): pásmo, rozpad hlasů, badge nekalibrováno. */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { TendencyChip } from './TendencyChip'
import { AppStateProvider } from '../state/AppState'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

const ROW = {
  ts_min: '2026-07-30T14:00:00+00:00',
  symbol: 'ES',
  score: 0.34,
  band: 'long',
  weights_version: 1,
  votes: [
    { name: 'flip', vote: 1, weight: 3, detail: 'cena nad flipem 7440' },
    { name: 'centroid', vote: -1, weight: 1, detail: 'cena nad těžištěm 7445' },
  ],
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) =>
      Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve(
            String(input).includes('/tendency/') ? { tendency: [ROW] } : { expiries: [] },
          ),
      }),
    ),
  )
})

test('ukazuje pásmo, po kliku rozpad hlasů s badge nekalibrováno (#350)', async () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(
    <AppStateProvider socket={socket}>
      <TendencyChip />
    </AppStateProvider>,
  )
  const chip = await screen.findByTestId('tendency-chip')
  expect(chip.textContent).toContain('Long')
  expect(chip.className).toContain('tendency-long')

  fireEvent.click(chip)
  const popover = screen.getByRole('dialog', { name: 'Rozpad hlasů tendence' })
  expect(popover.textContent).toContain('nekalibrováno')
  expect(popover.textContent).toContain('skóre 0.34')
  expect(popover.textContent).toContain('Poloha vůči Gamma Flipu')
  expect(popover.textContent).toContain('cena nad těžištěm 7445')
})
