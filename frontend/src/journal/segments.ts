/** Segmenty obchodního dne pro Daily Report Card (#712).

Den se hodnotí po částech, ne jako celek: degradace výkonu odpoledne se
v denním průměru schová (Breitstein / SMB Daily Report Card).

Hranice se počítají z TÉŽE tabulky seancí jako markery a range presety
(`usSessionMs`, #511) — žádné hardcodované 9:30 ET, DST řeší zoneinfo.
Futures dělení kopíruje presety rozsahů (#487), aby „US open +30" v report
card znamenalo totéž okno jako v grafu.
*/
import { usSessionMs } from '../instrument/sessions'
import type { JournalProfile } from '../api/journal'

export interface DaySegment {
  key: string
  label: string
  /** Začátek segmentu (epoch ms, UTC). */
  startMs: number
  /** Konec segmentu (exkluzivně). */
  endMs: number
}

const MINUTE = 60_000

/**
 * Segmenty dne podle profilu.
 *
 * `futures`: Globex noc → premarket → IB (open+30) → dopoledne → poledne →
 * power hour → close. `smb`: klasické SMB dělení akciové seance.
 */
export function daySegments(profile: JournalProfile, dateIso: string): DaySegment[] {
  const open = usSessionMs('open', dateIso)
  const close = usSessionMs('close', dateIso)
  if (profile === 'smb') {
    return [
      { key: 'premarket', label: 'Pre-market', startMs: open - 150 * MINUTE, endMs: open },
      { key: 'open_11', label: '9:30–11', startMs: open, endMs: open + 90 * MINUTE },
      { key: '11_12', label: '11–12', startMs: open + 90 * MINUTE, endMs: open + 150 * MINUTE },
      { key: '12_14', label: '12–14', startMs: open + 150 * MINUTE, endMs: open + 270 * MINUTE },
      { key: '14_close', label: '14–16', startMs: open + 270 * MINUTE, endMs: close },
    ]
  }
  return [
    // Globex seance začíná 17:00 CT předchozího dne (#512); pro report card
    // stačí ohraničit ji premarketem, přesná hrana osy sem nepatří.
    { key: 'globex', label: 'Globex noc', startMs: open - 15 * 60 * MINUTE, endMs: open - 90 * MINUTE }, // prettier-ignore
    { key: 'premarket', label: 'US premarket', startMs: open - 90 * MINUTE, endMs: open },
    { key: 'open30', label: 'US open +30', startMs: open, endMs: open + 30 * MINUTE },
    { key: 'dopoledne', label: 'RTH dopoledne', startMs: open + 30 * MINUTE, endMs: open + 150 * MINUTE }, // prettier-ignore
    { key: 'poledne', label: 'Poledne', startMs: open + 150 * MINUTE, endMs: close - 90 * MINUTE },
    { key: 'power', label: 'Power hour', startMs: close - 90 * MINUTE, endMs: close - 30 * MINUTE },
    { key: 'close30', label: 'Posledních 30 min', startMs: close - 30 * MINUTE, endMs: close },
  ]
}

/** Do kterého segmentu spadá okamžik; null mimo pokryté hodiny. */
export function segmentForTs(
  profile: JournalProfile,
  dateIso: string,
  tsIso: string,
): DaySegment | null {
  const ms = Date.parse(tsIso)
  if (!Number.isFinite(ms)) return null
  for (const segment of daySegments(profile, dateIso)) {
    if (ms >= segment.startMs && ms < segment.endMs) return segment
  }
  return null
}
