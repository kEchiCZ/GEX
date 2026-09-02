/** Vyhledání symbolu (#521 C): našeptávač, výběr → POST /adhoc + přepnutí. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { SymbolSearch } from './SymbolSearch'

const setSymbol = vi.fn()
vi.mock('../state/AppState', () => ({
  useAppState: () => ({ setSymbol }),
}))

beforeEach(() => {
  setSymbol.mockClear()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string) =>
      Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve(
            String(url).includes('/search')
              ? { matches: [{ symbol: 'CL', name: 'Crude Oil' }] }
              : {},
          ),
      }),
    ),
  )
})

test('napíšu „cl", Enter → POST /adhoc/CL a přepnutí symbolu', async () => {
  render(<SymbolSearch />)
  const input = screen.getByLabelText('Vyhledat symbol')
  fireEvent.change(input, { target: { value: 'cl' } })
  await waitFor(() => {
    const urls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/search?q=cl'))).toBe(true)
  })
  fireEvent.keyDown(input, { key: 'Enter' })
  await waitFor(() => {
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
    expect(calls.some((c) => String(c[0]).includes('/adhoc/CL') && c[1]?.method === 'POST')).toBe(
      true,
    )
  })
  expect(setSymbol).toHaveBeenCalledWith('CL')
  // Pole se po výběru vyčistí
  expect((input as HTMLInputElement).value).toBe('')
})

test('prázdný dotaz nehledá a Enter nic nedělá', () => {
  render(<SymbolSearch />)
  fireEvent.keyDown(screen.getByLabelText('Vyhledat symbol'), { key: 'Enter' })
  expect(setSymbol).not.toHaveBeenCalled()
})

test('výběr položky z našeptávače otevře symbol bez Enteru (#983)', async () => {
  render(<SymbolSearch />)
  const input = screen.getByLabelText('Vyhledat symbol')
  fireEvent.change(input, { target: { value: 'c' } })
  await waitFor(() => {
    const urls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/search?q=c'))).toBe(true)
  })
  // Prohlížeč po kliknutí na položku datalistu dosadí hodnotu jako
  // `insertReplacementText` — bez Enteru
  await waitFor(() => expect(screen.getByRole('option', { hidden: true })).toBeDefined())
  fireEvent.input(input, { target: { value: 'CL' }, inputType: 'insertReplacementText' })
  await waitFor(() => expect(setSymbol).toHaveBeenCalledWith('CL'))
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
  expect(calls.some((c) => String(c[0]).includes('/adhoc/CL') && c[1]?.method === 'POST')).toBe(
    true,
  )
  expect((input as HTMLInputElement).value).toBe('')
})

test('obyčejné psaní (ne výběr z našeptávače) symbol neotevírá', async () => {
  render(<SymbolSearch />)
  const input = screen.getByLabelText('Vyhledat symbol')
  fireEvent.input(input, { target: { value: 'CL' }, inputType: 'insertText' })
  await waitFor(() => {
    const urls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/search?q=CL'))).toBe(true)
  })
  expect(setSymbol).not.toHaveBeenCalled()
})
