/** Testy kalendářového selektoru expirací (#513): výběr, multi-TC krok, klávesy. */
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { ExpiryCalendar } from './ExpiryCalendar'

const NOW = new Date(Date.UTC(2026, 7, 26, 12, 0))

function renderCalendar(overrides: Partial<Parameters<typeof ExpiryCalendar>[0]> = {}) {
  const onSelect = vi.fn()
  const utils = render(
    <ExpiryCalendar
      expiries={['20260826', '20260828', '20260831']}
      expiryClasses={{ 20260828: ['EW4'], 20260831: ['EW'] }}
      selected="20260828"
      onSelect={onSelect}
      extended={new Set()}
      now={NOW}
      {...overrides}
    />,
  )
  return { onSelect, ...utils }
}

test('trigger nese datum + trading class, klik otevře mřížku měsíce výběru', () => {
  renderCalendar()
  const trigger = screen.getByTestId('expiry-trigger')
  expect(trigger.textContent).toContain('20260828 · EW4')
  fireEvent.click(trigger)
  expect(screen.getByTestId('expiry-calendar')).toBeTruthy()
  expect(screen.getByText('srpen 2026')).toBeTruthy()
})

test('den s jednou sérií se vybere rovnou a popover se zavře', () => {
  const { onSelect } = renderCalendar()
  fireEvent.click(screen.getByTestId('expiry-trigger'))
  const day = document.querySelector('button[data-expiry="20260831"]')
  expect(day).not.toBeNull()
  // Druh expirace je vidět už v mřížce (31. 8. = EOM)
  expect(day?.className).toContain('cal-kind-eom')
  fireEvent.click(day as Element)
  expect(onSelect).toHaveBeenCalledWith('20260831')
  expect(screen.queryByTestId('expiry-calendar')).toBeNull()
})

test('multi-TC den nabídne druhý krok s viditelnými sériemi (AC #513)', () => {
  const { onSelect } = renderCalendar({
    expiryClasses: { 20260828: ['E4C', 'EW4'], 20260831: ['EW'] },
  })
  fireEvent.click(screen.getByTestId('expiry-trigger'))
  const day = document.querySelector('button[data-expiry="20260828"]')
  fireEvent.click(day as Element)
  // Ještě nevybráno — čeká se na sérii
  expect(onSelect).not.toHaveBeenCalled()
  const step = screen.getByTestId('cal-tc-step')
  expect(step.textContent).toContain('E4C')
  expect(step.textContent).toContain('EW4')
  fireEvent.click(screen.getByText('E4C'))
  expect(onSelect).toHaveBeenCalledWith('20260828')
  expect(screen.queryByTestId('expiry-calendar')).toBeNull()
})

test('Escape zavře popover, šipky posouvají měsíce přes PgDn', () => {
  renderCalendar()
  fireEvent.click(screen.getByTestId('expiry-trigger'))
  const popover = screen.getByTestId('expiry-calendar')
  fireEvent.keyDown(popover, { key: 'PageDown' })
  expect(screen.getByText('září 2026')).toBeTruthy()
  fireEvent.keyDown(popover, { key: 'Escape' })
  expect(screen.queryByTestId('expiry-calendar')).toBeNull()
})

test('bez expirací je trigger vypnutý', () => {
  renderCalendar({ expiries: [], selected: null })
  const trigger = screen.getByTestId('expiry-trigger') as HTMLButtonElement
  expect(trigger.disabled).toBe(true)
})

test('expirace mimo IBKR nese v triggeru značku tasty', () => {
  renderCalendar({
    selected: '20260826',
    expiryClasses: {},
    extended: new Set(['20260826']),
  })
  expect(screen.getByTestId('expiry-trigger').textContent).toContain('20260826 · tasty')
})
