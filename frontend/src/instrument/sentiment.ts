/** Vlastní sentiment ukazatele (#205): Put/Call ratio z dat, která už sbíráme.

PCR(volume) = Σ put volume / Σ call volume přes strikes dané minuty — intradenní
tok (> 1 a roste = defenzivní nákupy putů; extrémy kontrariánsky). PCR(OI) =
totéž z OI — strukturální pozicování. Rozdíl obou = dnešní tok proti drženému
stavu. Žádný nový sběr — čistá redukce nad snapshot maticí (RawDay).
*/
import type { RawDay } from '../heatmap/modes'

export interface PcrPoint {
  volume: number | null
  oi: number | null
}

function ratioAt(
  put: Float32Array,
  call: Float32Array,
  minutes: number,
  strikeCount: number,
  minuteIdx: number,
): number | null {
  let putSum = 0
  let callSum = 0
  for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
    const index = strikeIdx * minutes + minuteIdx
    putSum += put[index]
    callSum += call[index]
  }
  return callSum > 0 ? putSum / callSum : null
}

/** PCR (volume i OI) k dané minutě; null = bez dat (nulová call strana). */
export function pcrAt(raw: RawDay, minuteIdx: number): PcrPoint {
  const strikeCount = raw.strikes.length
  if (raw.minutes === 0 || strikeCount === 0 || minuteIdx < 0 || minuteIdx >= raw.minutes) {
    return { volume: null, oi: null }
  }
  return {
    volume: ratioAt(raw.putVolume, raw.callVolume, raw.minutes, strikeCount, minuteIdx),
    oi: ratioAt(raw.putOi, raw.callOi, raw.minutes, strikeCount, minuteIdx),
  }
}

/** Denní řada PCR(volume) pro mini křivku — vývoj intradenního toku. */
export function pcrVolumeSeries(raw: RawDay): (number | null)[] {
  const strikeCount = raw.strikes.length
  return Array.from({ length: raw.minutes }, (_, minuteIdx) =>
    ratioAt(raw.putVolume, raw.callVolume, raw.minutes, strikeCount, minuteIdx),
  )
}

/** Formát PCR hodnoty (2 desetinná místa, pomlčka bez dat). */
export function formatPcr(value: number | null): string {
  return value === null ? '—' : value.toFixed(2)
}
