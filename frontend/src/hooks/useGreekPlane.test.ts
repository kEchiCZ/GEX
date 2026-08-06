/** Test zarovnání řádků plochy na minutovou osu (#204) a race fetch × WS (#504). */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { alignPlaneProfiles, useGreekPlane } from './useGreekPlane'
import type { LiveSocket } from '../api/ws'
import type { GexProfileRow } from '../replay/loader'

function row(tsIso: string): GexProfileRow {
  return { tsIso, gridStart: 7400, gridStep: 5, values: [1, 2, 3] }
}

type FakeSocket = LiveSocket & { emit: (channel: string, data: unknown) => void }

function makeSocket(): FakeSocket {
  const handlers = new Map<string, Set<(data: unknown) => void>>()
  const socket = {
    subscribe: (channel: string, handler: (data: unknown) => void) => {
      let set = handlers.get(channel)
      if (!set) {
        set = new Set()
        handlers.set(channel, set)
      }
      set.add(handler)
    },
    unsubscribe: (channel: string, handler: (data: unknown) => void) =>
      handlers.get(channel)?.delete(handler),
    onReconnect: () => () => {},
    connect: () => {},
    close: () => {},
    emit: (channel: string, data: unknown) => handlers.get(channel)?.forEach((h) => h(data)),
  }
  return socket as unknown as FakeSocket
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('alignPlaneProfiles', () => {
  it('mapuje řádky podle ISO času, mimo osu zahazuje, díry nechává null', () => {
    const minutes = [
      '2026-07-30T14:00:00+00:00',
      '2026-07-30T14:01:00+00:00',
      '2026-07-30T14:02:00+00:00',
    ]
    const aligned = alignPlaneProfiles(
      [row(minutes[2]), row(minutes[0]), row('2026-07-30T15:00:00+00:00')],
      minutes,
    )
    expect(aligned).toHaveLength(3)
    expect(aligned[0]?.tsIso).toBe(minutes[0])
    expect(aligned[1]).toBeNull()
    expect(aligned[2]?.tsIso).toBe(minutes[2])
    expect(alignPlaneProfiles([], [])).toEqual([])
  })
})

describe('useGreekPlane', () => {
  it('WS minuta došlá během fetche se náhradou payloadu neztratí (#504)', async () => {
    // Fetch se vyřídí až na povel — WS profil mezitím stihne dorazit
    let resolveFetch!: (payload: unknown) => void
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise((resolve) => {
            resolveFetch = (payload) => resolve({ ok: true, json: async () => payload })
          }),
      ),
    )
    const socket = makeSocket()
    const { result } = renderHook(() =>
      useGreekPlane('ES', '20260730', '2026-07-30', 'charm', socket),
    )

    // WS minuta 14:05 dorazí dřív než odpověď fetche
    act(() => {
      socket.emit('charmprofile.ES.20260730', {
        ts_min: '2026-07-30T14:05:00Z',
        grid_start: 7400,
        grid_step: 5,
        values: [9, 9, 9],
      })
    })
    expect(result.current.profiles.map((item) => item.tsIso)).toEqual(['2026-07-30T14:05:00.000Z'])

    // Fetch (vyžádaný před WS minutou) ji ještě neobsahuje — nesmí ji přepsat
    await act(async () => {
      resolveFetch({
        profiles: [
          { ts_min: '2026-07-30T14:04:00Z', grid_start: 7400, grid_step: 5, values: [1, 2, 3] },
        ],
      })
    })
    await waitFor(() =>
      expect(result.current.profiles.map((item) => item.tsIso).sort()).toEqual([
        '2026-07-30T14:04:00.000Z',
        '2026-07-30T14:05:00.000Z',
      ]),
    )
  })
})
