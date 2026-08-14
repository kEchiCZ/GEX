/** Testy značek deníku na ose (#673): párování formatterem, clustery, near. */
import { describe, expect, it } from 'vitest'
import type { JournalEntry } from '../api/journal'
import { buildJournalMarkers, journalMarkerNear } from './journalMarkers'

function entry(id: number, tsRef: string): JournalEntry {
  return {
    id,
    ts_ref: tsRef,
    symbol: 'ES',
    entry_type: 'pozorovani',
    text: 'x',
    tags: [],
    setup_id: null,
    news_event_id: null,
    profile: 'futures',
    trade: null,
    created_ts: tsRef,
    updated_ts: null,
  }
}

// Formatter jako v ose: HH:MM z ISO (UTC, ať test nezávisí na timezone)
const label = (iso: string) => iso.slice(11, 16)

describe('buildJournalMarkers', () => {
  it('mapuje záznam na minutu přes formatter popisků', () => {
    const labels = ['15:00', '15:01', '15:02']
    const markers = buildJournalMarkers([entry(1, '2026-08-13T15:01:00Z')], labels, label)
    expect(markers).toHaveLength(1)
    expect(markers[0].minuteIdx).toBe(1)
    expect(markers[0].count).toBe(1)
  })

  it('víc záznamů v téže minutě = jedna značka s počtem', () => {
    const labels = ['15:00', '15:01']
    const markers = buildJournalMarkers(
      [entry(1, '2026-08-13T15:01:10Z'), entry(2, '2026-08-13T15:01:40Z')],
      labels,
      label,
    )
    expect(markers).toHaveLength(1)
    expect(markers[0].count).toBe(2)
    expect(markers[0].entries.map((e) => e.id)).toEqual([1, 2])
  })

  it('záznam mimo osu (jiný den/čas) se tiše vynechá', () => {
    const markers = buildJournalMarkers([entry(1, '2026-08-13T09:00:00Z')], ['15:00'], label)
    expect(markers).toEqual([])
  })

  it('prázdná osa nic nevrací', () => {
    expect(buildJournalMarkers([entry(1, '2026-08-13T15:00:00Z')], [], label)).toEqual([])
  })
})

describe('journalMarkerNear', () => {
  const markers = buildJournalMarkers(
    [entry(1, '2026-08-13T15:00:00Z'), entry(2, '2026-08-13T15:10:00Z')],
    Array.from({ length: 11 }, (_, i) => `15:${String(i).padStart(2, '0')}`),
    label,
  )

  it('vrací nejbližší značku v toleranci', () => {
    expect(journalMarkerNear(markers, 1, 1)?.entries[0].id).toBe(1)
    expect(journalMarkerNear(markers, 9, 1)?.entries[0].id).toBe(2)
  })

  it('mimo toleranci vrací null', () => {
    expect(journalMarkerNear(markers, 5, 2)).toBeNull()
  })
})
