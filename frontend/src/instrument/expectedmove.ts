/** Expected move dne z ATM straddle (#676) — čisté funkce.

EM = mid(call) + mid(put) na ATM striku v referenční minutě: první minuta
US seance s validním straddlem (spot + obě kotace). Před openem se použije
poslední validní minuta overnight — odhad se průběžně obnovuje a s openem
zamkne. Hranice dne = spot referenční minuty ± EM; kreslí se jako dvě
vodorovné linie (jen Traders mode #677 — trading info, ne positioning).

Mid 0 = kotace chybí (#469) — ATM se hledá mezi nejbližšími strikes se
zaplacenou oběma stranami, do MAX_ATM_CANDIDATES kroků od spotu.
*/

export interface StraddleRow {
  strike: number
  callMid?: number
  putMid?: number
}

export interface EmInputs {
  minutesIso: string[]
  spotSeries: (number | null)[]
  rowsAt: (minuteIdx: number) => StraddleRow[]
  /** US open (9:30 New York) v epoch ms — DST-korektně (usOpenMs, #674). */
  usOpenMs: number
}

export interface ExpectedMove {
  refMinuteIdx: number
  /** Referenční minuta je před US openem — průběžný odhad, openem se zamkne. */
  preOpen: boolean
  anchor: number
  atmStrike: number
  em: number
  upper: number
  lower: number
}

/** Kolik nejbližších strikes se zkouší, když ATM nemá obě kotace. */
const MAX_ATM_CANDIDATES = 3

/** ATM straddle z řádků minuty: nejbližší strike se zaplacenou C i P stranou. */
export function straddleAt(
  rows: StraddleRow[],
  spot: number,
): { strike: number; em: number } | null {
  const candidates = rows
    .filter((row) => (row.callMid ?? 0) > 0 && (row.putMid ?? 0) > 0)
    .sort((a, b) => Math.abs(a.strike - spot) - Math.abs(b.strike - spot))
    .slice(0, MAX_ATM_CANDIDATES)
  if (candidates.length === 0) return null
  const atm = candidates[0]
  return { strike: atm.strike, em: (atm.callMid ?? 0) + (atm.putMid ?? 0) }
}

export function computeExpectedMove(inputs: EmInputs): ExpectedMove | null {
  const { minutesIso, spotSeries, rowsAt, usOpenMs } = inputs
  const build = (minuteIdx: number, preOpen: boolean): ExpectedMove | null => {
    const spot = spotSeries[minuteIdx]
    if (spot === null || spot === undefined) return null
    const straddle = straddleAt(rowsAt(minuteIdx), spot)
    if (straddle === null) return null
    return {
      refMinuteIdx: minuteIdx,
      preOpen,
      anchor: spot,
      atmStrike: straddle.strike,
      em: straddle.em,
      upper: spot + straddle.em,
      lower: spot - straddle.em,
    }
  }
  // První validní minuta od US openu — EM dne se openem zamkne
  for (let idx = 0; idx < minutesIso.length; idx += 1) {
    if (Date.parse(minutesIso[idx]) < usOpenMs) continue
    const result = build(idx, false)
    if (result !== null) return result
  }
  // Před openem: poslední validní overnight minuta (průběžný odhad)
  for (let idx = minutesIso.length - 1; idx >= 0; idx -= 1) {
    if (Date.parse(minutesIso[idx]) >= usOpenMs) continue
    const result = build(idx, true)
    if (result !== null) return result
  }
  return null
}

/** Vyčerpání rozsahu: poloha spotu v pásmu EM (0 = dolní, 1 = horní hranice;
mimo pásmo přeteče přes 0/1). Do popisku linie. */
export function emUsage(move: ExpectedMove, spot: number): number {
  return (spot - move.lower) / (move.upper - move.lower)
}
