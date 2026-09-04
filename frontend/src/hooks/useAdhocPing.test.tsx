/** Ad-hoc ping (#521 C): před načtením watchlistu se nepinguje (4. 9. nález). */
import { renderHook } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { useAdhocPing } from './useAdhocPing'

afterEach(() => vi.restoreAllMocks())

test('watched === null (watchlist neznámý) → žádný POST /adhoc', () => {
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}'))
  renderHook(() => useAdhocPing('NQ', null))
  expect(fetchMock).not.toHaveBeenCalled()
})

test('watched === false → ping hned; watched === true → nic', () => {
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}'))
  renderHook(() => useAdhocPing('CL', false))
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(String(fetchMock.mock.calls[0][0])).toContain('/adhoc/CL')
  fetchMock.mockClear()
  renderHook(() => useAdhocPing('ES', true))
  expect(fetchMock).not.toHaveBeenCalled()
})
