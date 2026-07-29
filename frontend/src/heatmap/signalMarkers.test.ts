/** Testy šipek signálů (#295, SPEC 9.0) — mapování na osu, stopa, tooltip. */
import { describe, expect, it } from 'vitest'
import { buildSignalMarkers, signalAt, signalColor, signalTooltip } from './signalMarkers'
import type { SignalRow } from '../api/news'

const LABELS = ['15:30', '15:31', '15:32', '15:33', '15:34']
/** Stejný princip jako osa grafu: ISO čas → popisek minuty (tady HH:MM UTC). */
const format = (iso: string) => iso.slice(11, 16)

function signal(overrides: Partial<SignalRow> = {}): SignalRow {
  return {
    id: 1,
    ts: '2026-07-29T15:31:00+00:00',
    symbol: 'ES',
    direction: 'long',
    strength: 0.6,
    mode: 'NEWS',
    inputs: {
      category: 'FED',
      importance: 3,
      bucket: { n: 50, hit_rate_lb: 0.6, ret_mean_bp: 6, window_min: 5 },
    },
    expiry_ts: '2026-07-29T15:33:00+00:00',
    ...overrides,
  }
}

describe('buildSignalMarkers', () => {
  it('mapuje signál i stopu platnosti podle popisků osy', () => {
    const markers = buildSignalMarkers([signal()], LABELS, format, {
      now: new Date(0), // vše v „budoucnosti" → active
    })
    expect(markers).toHaveLength(1)
    expect(markers[0].minuteIdx).toBe(1)
    expect(markers[0].endIdx).toBe(3)
    expect(markers[0].active).toBe(true)
  })

  it('expiraci mimo osu ořízne na poslední minutu, signál mimo osu zahodí', () => {
    const beyond = buildSignalMarkers(
      [signal({ expiry_ts: '2026-07-29T18:00:00+00:00' })],
      LABELS,
      format,
    )
    expect(beyond[0].endIdx).toBe(LABELS.length - 1)
    expect(
      buildSignalMarkers([signal({ ts: '2026-07-29T09:00:00+00:00' })], LABELS, format),
    ).toHaveLength(0)
  })

  it('⚠ badge dostanou jen aktivní signály při unconfirmed změně stavu', () => {
    const rows = [
      signal(),
      signal({ id: 2, ts: '2026-07-29T15:32:00+00:00', expiry_ts: '2026-07-29T15:33:00+00:00' }),
    ]
    const past = new Date(8.64e15) // po expiraci všech
    expect(
      buildSignalMarkers(rows, LABELS, format, { warning: true, now: past }).map((m) => m.warning),
    ).toEqual([false, false])
    expect(
      buildSignalMarkers(rows, LABELS, format, { warning: true, now: new Date(0) }).map(
        (m) => m.warning,
      ),
    ).toEqual([true, true])
  })
})

describe('signalTooltip', () => {
  it('nese režim, zdůvodnění, n a Wilson LB (SPEC 9.0)', () => {
    const text = signalTooltip(signal())
    expect(text).toContain('▲ Long · NEWS')
    expect(text).toContain('síla 0.60')
    expect(text).toContain('Fed imp 3')
    expect(text).toContain('n=50')
    expect(text).toContain('LB 60 %')
  })
})

describe('signalColor a signalAt', () => {
  it('long je teal, short červená; sytost roste se strength', () => {
    const strong = buildSignalMarkers([signal({ strength: 1 })], LABELS, format)[0]
    const weak = buildSignalMarkers([signal({ strength: 0 })], LABELS, format)[0]
    expect(signalColor(strong)).toContain('20,184,166')
    expect(signalColor(strong)).toContain('0.95')
    expect(signalColor(weak)).toContain('0.35')
    const short = buildSignalMarkers([signal({ direction: 'short' })], LABELS, format)[0]
    expect(signalColor(short)).toContain('224,82,96')
  })

  it('signalAt najde marker na minutě crosshairu', () => {
    const markers = buildSignalMarkers([signal()], LABELS, format)
    expect(signalAt(markers, 1)).not.toBeNull()
    expect(signalAt(markers, 2)).toBeNull()
    expect(signalAt(markers, null)).toBeNull()
  })
})
