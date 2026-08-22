import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from './App'
import { LiveSocket } from './api/ws'
import { FakeWebSocket } from './test/fakeWs'

function makeApp() {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  return render(<App socket={socket} />)
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ expiries: ['20260716', '20260717'] }),
    }),
  )
})

test('vykreslí kompletní layout (SPEC 7.1)', async () => {
  makeApp()

  expect(screen.getAllByText('ES').length).toBeGreaterThan(0) // ticker + watchlist
  // Watchlist ukazuje TWS symbol předního kontraktu v závorce (#189)
  expect(screen.getByText(/\(ES[HMUZ]\d\)/)).toBeDefined()
  expect(screen.getByLabelText('Hlavní navigace')).toBeDefined()
  expect(screen.getByLabelText('Watchlist')).toBeDefined()
  expect(screen.getByLabelText('Timeframe')).toBeDefined()
  expect(screen.getByLabelText('Přepínače vizualizace')).toBeDefined()
  expect(screen.getByLabelText('Stav pipeline')).toBeDefined()
  expect(screen.getByText('Zdi')).toBeDefined() // přepínač zdí (dřív „Dyn GEX")
  expect(screen.getByText('Dyn GEX')).toBeDefined() // přepínač podkladové vrstvy (#242)
  // "Vol + OI Δ" je v přepínačích i v hlavičce strike profilu
  expect(screen.getAllByText('Vol + OI Δ').length).toBeGreaterThan(0)
  expect(screen.getByLabelText('Strike profil')).toBeDefined()

  // Expirace načtené z REST
  expect(await screen.findByRole('option', { name: '20260716' })).toBeDefined()
})

test('Ctrl+kolečko (pinch) nezoomuje stránku NAD grafem; jinde zůstává (#179, #181)', () => {
  makeApp()
  // Pinch na touchpadu chodí jako wheel s ctrlKey; dispatchEvent vrací false,
  // když handler zavolal preventDefault (page zoom zablokován)
  const overChart = new WheelEvent('wheel', { ctrlKey: true, cancelable: true, bubbles: true })
  const chart = document.querySelector('.chart-row')!
  expect(chart.dispatchEvent(overChart)).toBe(false)
  // Mimo graf (sidebar/lišty) musí zoom prohlížeče zůstat — rozjetý page zoom
  // by se jinak nedal vrátit (#181)
  const overSidebar = new WheelEvent('wheel', { ctrlKey: true, cancelable: true, bubbles: true })
  expect(screen.getByLabelText('Hlavní navigace').dispatchEvent(overSidebar)).toBe(true)
  // Obyčejné kolečko nad grafem (zoom grafu) zůstává nedotčené
  const plain = new WheelEvent('wheel', { cancelable: true, bubbles: true })
  expect(chart.dispatchEvent(plain)).toBe(true)
})

test('sidebar se dá sbalit a rozbalit', () => {
  makeApp()
  const toggle = screen.getByLabelText('Sbalit menu')

  fireEvent.click(toggle)
  expect(screen.queryByLabelText('Hlavní navigace')).toBeNull()

  fireEvent.click(screen.getByLabelText('Rozbalit menu'))
  expect(screen.getByLabelText('Hlavní navigace')).toBeDefined()
})

test('stavová lišta žije ze status kanálu /ws/live (AC)', () => {
  makeApp()
  expect(screen.getByTestId('status-live').textContent).toBe('Stale')

  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('status', {
      engine: 'online',
      connection: 'connected',
      port: 7496,
      greeks_complete: 350,
      greeks_total: 360,
      repair_count: 4,
      lines_utilization: 0.8,
      disk_usage_bytes: 500 * 1024 * 1024,
      disk_limit_bytes: 2 * 1024 * 1024 * 1024,
    })
  })

  expect(screen.getByTestId('status-greeks').textContent).toBe('Greeks 350/360')
  expect(screen.getByTestId('status-repair').textContent).toBe(
    'Repair: retrying 4 incomplete strikes',
  )
  expect(screen.getByTestId('status-lines').textContent).toBe('Lines 80 %')
  expect(screen.getByTestId('status-ibkr').textContent).toBe('IBKR: connected :7496')
  expect(screen.getByTestId('status-disk').textContent).toBe('Disk 500.0 MB / 2.0 GB')
  expect(screen.getByTestId('status-live').textContent).toContain('● Live')
})

test('přepínače timeframe a vizualizace mění stav', () => {
  makeApp()

  const daily = screen.getByRole('button', { name: 'Daily' })
  fireEvent.click(daily)
  expect(daily.className).toContain('active')

  const sessions = screen.getByLabelText('Sessions') as HTMLInputElement
  expect(sessions.checked).toBe(false)
  fireEvent.click(sessions)
  expect(sessions.checked).toBe(true)
})

test('tlačítka zpět/vpřed kreslení jsou nejdřív disabled a Ctrl+Z je nerozbije (#590)', () => {
  makeApp()

  const undo = screen.getByRole('button', { name: 'Zpět' }) as HTMLButtonElement
  const redo = screen.getByRole('button', { name: 'Vpřed' }) as HTMLButtonElement
  expect(undo.disabled).toBe(true)
  expect(redo.disabled).toBe(true)
  // Klávesa nad prázdnou historií je no-op (nesmí spadnout ani nic smazat)
  fireEvent.keyDown(window, { key: 'z', ctrlKey: true })
  fireEvent.keyDown(window, { key: 'z', ctrlKey: true, shiftKey: true })
  expect(undo.disabled).toBe(true)
  expect(redo.disabled).toBe(true)
})

test('hlavička ukazuje pokrytí Greeks s progress barem (#470)', () => {
  makeApp()
  // Bez statusu prvek zůstává na místě a jen nic netvrdí (#758) — mizející
  // ukazatel vypadá jako rozbité rozhraní, ne jako chybějící data
  const unknown = screen.getByTestId('coverage-greeks')
  expect(unknown.textContent).toBe('Greeks —')
  expect(unknown.className).toContain('coverage-unknown')
  expect(unknown.querySelector('.coverage-fill')?.getAttribute('style')).toContain('width: 0%')

  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('status', { engine: 'online', greeks_complete: 91, greeks_total: 182 })
  })

  const badge = screen.getByTestId('coverage-greeks')
  expect(badge.textContent).toBe('Greeks 91/182 (50 %)')
  // Pravý blok drží pokrytí, Live i zvoneček u sebe a zvoneček je z nich poslední (#597)
  const right = badge.closest('.header-right')!
  const bell = right.querySelector('.bell-wrap')!
  expect(right.querySelector('.live-indicator')).not.toBeNull()
  expect(right.lastElementChild).toBe(bell)
  expect(badge.className).toContain('coverage-partial') // neúplné pokrytí hlásí barvu
  expect(badge.querySelector('.coverage-fill')?.getAttribute('style')).toContain('width: 50%')

  act(() => {
    ws.push('status', { engine: 'online', greeks_complete: 182, greeks_total: 182 })
  })
  // Plné pokrytí bez procent a bez varovné barvy (#597)
  expect(screen.getByTestId('coverage-greeks').textContent).toBe('Greeks 182/182')
  expect(screen.getByTestId('coverage-greeks').className).not.toContain('coverage-partial')
})

test('fallback na tastytrade je vidět v hlavičce, jinak chip nesvítí (#614)', () => {
  makeApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('status', { engine: 'online', chain_source: 'ibkr', spot_source: 'ibkr' })
  })
  // Za normálního provozu chip nemá co říct a jen by zabíral místo
  expect(screen.queryByTestId('fallback-chip')).toBeNull()

  act(() => {
    ws.push('status', { engine: 'online', chain_source: 'tasty', spot_source: 'ibkr' })
  })
  // Tiché přepnutí zdroje zakazuje ADR-0025 pravidlo 5
  expect(screen.getByTestId('fallback-chip').textContent).toContain('řetěz: tastytrade')

  act(() => {
    ws.push('status', { engine: 'online', chain_source: 'tasty', spot_source: 'tasty' })
  })
  expect(screen.getByTestId('fallback-chip').textContent).toContain('řetěz + cena: tastytrade')
})

test('demo den nemá měřitelnou osu, takže OHLC badge ani časová značka nesvítí (#470)', () => {
  makeApp()
  // Demo den (bez /replay dat) nemá ISO osu ani lastMinuteIso — pokrytí se nedá
  // změřit, tak ho prvek přizná pomlčkou místo aby lhal číslem (#758)
  expect(screen.getByTestId('coverage-ohlc').textContent).toBe('OHLC —')
  expect(screen.queryByTestId('data-stamp')).toBeNull()
  expect(screen.getByTestId('data-source').textContent).toBe('demo data')
})

test('extended expirace nese badge zdroje tastytrade (#616 4b)', async () => {
  makeApp()
  // Expirace z REST; první (20260716) se vybere automaticky
  expect(await screen.findByRole('option', { name: '20260716' })).toBeDefined()

  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('status', {
      engine: 'online',
      // 20260717 je vybraná expirace (nejbližší k dnešku vyhrává v AppState)
      tasty_extended_expiries: { ES: ['20260717'] },
    })
  })

  expect(screen.getByRole('option', { name: '20260717 · tasty' })).toBeDefined()
  expect(screen.getByTestId('expiry-meta').textContent).toContain('zdroj tastytrade')
  // Druhá expirace (IBKR) badge nemá
  expect(screen.getByRole('option', { name: '20260716' })).toBeDefined()
})
