/** Referenční úrovně (#678, Traders mode): ONH/ONL, PDH/PDL(+PDC), denní VWAP.

Čisté funkce nad 1min OHLCV bary seance (GET /bars, osa = Globex seance #512).
Smysl vrstvy: konfluence zdí s úrovněmi, které sleduje zbytek trhu — zeď na
PDH ≠ zeď v prázdnu.

- **ONH/ONL** — overnight extrémy: od začátku seance do US openu. Po openu
  jsou zamčené; před openem průběžně rostou s daty.
- **PDH/PDL/PDC** — extrémy celé Globex seance a close k settle (RTH close)
  předchozí obchodní seance; souhrn počítá `previousSessionRange` (#1596).
- **VWAP** — kumulativní Σ(typická cena × objem) / Σ objem přes seanci,
  typická cena = (H+L+C)/3. Minuty bez objemu drží poslední hodnotu.
*/
import type { BarRow, RangeSummary } from '../api/briefing'

export interface ReferenceLevels {
  onHigh: number | null
  onLow: number | null
  /** Overnight okno ještě běží (před US openem) — hodnoty nejsou finální. */
  onRunning: boolean
  prevHigh: number | null
  prevLow: number | null
  prevClose: number | null
  /** VWAP per 1min bar dnešní seance (index = pořadí baru v `todayBars`). */
  vwap: Array<{ tsIso: string; value: number }>
}

export function computeReferenceLevels(input: {
  todayBars: BarRow[]
  /** Předchozí obchodní seance (`previousSessionRange`); null = bez dat. */
  prevDay: RangeSummary | null
  usOpenMs: number
  nowMs: number
}): ReferenceLevels {
  const { todayBars, prevDay, usOpenMs, nowMs } = input

  let onHigh: number | null = null
  let onLow: number | null = null
  for (const bar of todayBars) {
    if (Date.parse(bar.ts_min) >= usOpenMs) break
    onHigh = onHigh === null ? bar.high : Math.max(onHigh, bar.high)
    onLow = onLow === null ? bar.low : Math.min(onLow, bar.low)
  }

  const vwap: Array<{ tsIso: string; value: number }> = []
  let cumPv = 0
  let cumVolume = 0
  for (const bar of todayBars) {
    const typical = (bar.high + bar.low + bar.close) / 3
    cumPv += typical * bar.volume
    cumVolume += bar.volume
    if (cumVolume > 0) vwap.push({ tsIso: bar.ts_min, value: cumPv / cumVolume })
  }

  return {
    onHigh,
    onLow,
    onRunning: nowMs < usOpenMs,
    prevHigh: prevDay?.high ?? null,
    prevLow: prevDay?.low ?? null,
    prevClose: prevDay?.last ?? null,
    vwap,
  }
}

/** VWAP řada namapovaná na osu grafu (koše): poslední hodnota ≤ konec koše.

`minutesIso` je 1m osa dne, `bucketMinutes` agregace timeframu — mapuje se
přes ISO čas (osa může nést díry, #502), ne aritmetikou indexů. */
export function vwapSeriesForAxis(
  vwap: Array<{ tsIso: string; value: number }>,
  minutesIso: string[],
  bucketMinutes: number,
): (number | null)[] {
  const buckets = Math.ceil(minutesIso.length / bucketMinutes)
  const byIso = new Map(vwap.map((point) => [point.tsIso, point.value]))
  const series: (number | null)[] = Array.from({ length: buckets }, () => null)
  let last: number | null = null
  for (let idx = 0; idx < minutesIso.length; idx += 1) {
    const value = byIso.get(minutesIso[idx])
    if (value !== undefined) last = value
    series[Math.floor(idx / bucketMinutes)] = last
  }
  return series
}
