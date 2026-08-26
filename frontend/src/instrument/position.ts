/** Kalkulačka velikosti pozice v R (#679) — čisté funkce, nic se neukládá na server.

Riziko = účet × %; počet kontraktů = floor(riziko / (stop v bodech × hodnota
bodu)). Vedle plného kontraktu se počítá i micro varianta (ES→MES, NQ→MNQ) —
s malým účtem je plný kontrakt často 0× a micro je jediná smysluplná exekuce.
*/
import { pointValue } from './tick'

/** Micro protějšek plného kontraktu; null = micro neexistuje/nezobrazovat. */
export const MICRO_SYMBOLS: Record<string, string> = {
  ES: 'MES',
  NQ: 'MNQ',
  RTY: 'M2K',
  YM: 'MYM',
}

export interface PositionSize {
  riskUsd: number
  stopPoints: number
  contracts: number
  symbol: string
  micro: { symbol: string; contracts: number } | null
}

export function positionSize(input: {
  symbol: string
  entry: number
  stop: number
  accountUsd: number
  riskPct: number
}): PositionSize | null {
  const { symbol, entry, stop, accountUsd, riskPct } = input
  const stopPoints = Math.abs(entry - stop)
  if (stopPoints <= 0 || accountUsd <= 0 || riskPct <= 0) return null
  const riskUsd = (accountUsd * riskPct) / 100
  const contracts = Math.floor(riskUsd / (stopPoints * pointValue(symbol)))
  const microSymbol = MICRO_SYMBOLS[symbol]
  return {
    riskUsd,
    stopPoints,
    contracts,
    symbol,
    micro:
      microSymbol === undefined
        ? null
        : {
            symbol: microSymbol,
            contracts: Math.floor(riskUsd / (stopPoints * pointValue(microSymbol))),
          },
  }
}

/** Volatilitní buckety, u kterých je fixní bodový stop těsnější obchod (#874). */
const CAUTION_BUCKETS = new Set(['elevated', 'crisis'])

export interface StopVsRange {
  /** Stop jako podíl typického denního rozsahu (session_range vol režimu). */
  share: number
  /** Bucket elevated/crisis — zvýraznit, fixní bodový stop je v tomto režimu užší. */
  caution: boolean
}

/** Stop vůči typickému rozsahu dne (#874, ADR-0028): stejný stop v bodech je
v jiném volatilitním režimu úplně jiný obchod. NIC nemění — jen ukazuje;
adaptace prahů patří do #434/#575. Null = bez vol režimu (žádný default). */
export function stopVsRange(
  stopPoints: number,
  vol: { bucket: string; session_range: number } | null,
): StopVsRange | null {
  if (vol === null || vol.session_range <= 0 || stopPoints <= 0) return null
  return {
    share: stopPoints / vol.session_range,
    caution: CAUTION_BUCKETS.has(vol.bucket),
  }
}

/** Text do karty setupu: „riziko 50 $ (1 %) · stop 8 b → ES 0× · MES 1ד. */
export function positionLabel(size: PositionSize, riskPct: number): string {
  const parts = [`${size.symbol} ${size.contracts}×`]
  if (size.micro) parts.push(`${size.micro.symbol} ${size.micro.contracts}×`)
  const risk = Math.round(size.riskUsd)
  const stop = Math.round(size.stopPoints * 100) / 100
  return `riziko ${risk} $ (${riskPct} %) · stop ${stop} b → ${parts.join(' · ')}`
}
