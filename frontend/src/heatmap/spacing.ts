/** Děravá strike osa (#548): medián rozestupů, cap výšky řádku, pásma děr.

Osa Y mapuje řádky rovnoměrně po indexech, takže díra v ose (např. NQ:
klastry 29290–29370 a 29680–29790, mezera 310 bodů) se v cenách vejde do
jediného rozestupu řádků. Buňka/pruh krajního striku pak v cenách pokrývá
celou díru a po přiblížení osy Y je z něj obří pruh přes stovky pixelů.

Fix: výška řádku je v CENÁCH capnutá na medián rozestupů × tolerance —
řádek u díry zůstane tenký, díra prázdná a křivky se přes ni přerušují
(vzor `isLevelJump`/`breaksOnJump`, #197/#198).
*/

import { fractionalRow } from './overlays'
import { priceAtRow } from './view'

/** Tolerance capu výšky řádku — rozestup do 1.5× mediánu je ještě „normální". */
export const ROW_CAP_TOLERANCE = 1.5

/** Práh přerušení křivky: díra větší než 2× medián rozestupů (#548). */
export const CURVE_GAP_TOLERANCE = 2

/** Medián rozestupů sousedních strikes; 0 = osa nemá aspoň 2 hodnoty. */
export function medianStep(strikes: number[]): number {
  if (strikes.length < 2) return 0
  const steps: number[] = []
  for (let index = 0; index + 1 < strikes.length; index += 1) {
    steps.push(strikes[index + 1] - strikes[index])
  }
  steps.sort((a, b) => a - b)
  const mid = Math.floor(steps.length / 2)
  return steps.length % 2 === 1 ? steps[mid] : (steps[mid - 1] + steps[mid]) / 2
}

/** Zlomky (0..1] poloviční výšky řádku nad/pod hodnotou: 1 = plná polovina,
méně = řádek sousedí s dírou a v cenách by přes ni přetekl. Polovina řádku
smí pokrýt nejvýš `tolerance × medián / 2` ceny — zlomek je poměr capu
k cenovému rozsahu, který by plná polovina řádku zabrala. */
export function capHalfFractions(
  strikes: number[],
  value: number,
  tolerance = ROW_CAP_TOLERANCE,
): { up: number; down: number } {
  const median = medianStep(strikes)
  const row = median > 0 ? fractionalRow(strikes, value) : null
  if (median <= 0 || row === null) return { up: 1, down: 1 }
  const capHalf = (tolerance * median) / 2
  const upExtent = priceAtRow(strikes, row + 0.5) - value
  const downExtent = value - priceAtRow(strikes, row - 0.5)
  return {
    up: upExtent > capHalf ? capHalf / upExtent : 1,
    down: downExtent > capHalf ? capHalf / downExtent : 1,
  }
}

/** Pásmo díry na obrazovce (px, plná šířka) — heatmapa ho z bitmapy vymaže. */
export interface GapBand {
  top: number
  bottom: number
}

/** Pásma děr osy v obrazovkových px. `scaleY`/`offsetY` = Y mapování gridu
(shodné s `rowToY` heatmapy). Krajní buňky díry si nechají jen capnutý díl
poloviny řádku, zbytek rozestupu je pásmo k vymazání. */
export function gapBands(
  strikes: number[],
  scaleY: number,
  offsetY: number,
  tolerance = ROW_CAP_TOLERANCE,
): GapBand[] {
  const median = medianStep(strikes)
  if (median <= 0 || scaleY <= 0) return []
  const bands: GapBand[] = []
  const last = strikes.length - 1
  const rowY = (row: number): number => (last - row + 0.5) * scaleY + offsetY
  for (let index = 0; index < last; index += 1) {
    const gap = strikes[index + 1] - strikes[index]
    if (gap <= tolerance * median) continue
    // Plná polovina řádku pokrývá gap/2 ceny; capnutá jen tolerance×medián/2
    const half = (scaleY / 2) * ((tolerance * median) / gap)
    bands.push({ top: rowY(index + 1) + half, bottom: rowY(index) - half })
  }
  return bands
}

/** Cenové intervaly děr osy (rozestup > tolerance × medián) — křivky (GEX/Dyn
profil) se přes ně přerušují místo souvislé čáry. */
export function axisGapRanges(
  strikes: number[],
  tolerance = CURVE_GAP_TOLERANCE,
): Array<[number, number]> {
  const median = medianStep(strikes)
  if (median <= 0) return []
  const ranges: Array<[number, number]> = []
  for (let index = 0; index + 1 < strikes.length; index += 1) {
    if (strikes[index + 1] - strikes[index] > tolerance * median) {
      ranges.push([strikes[index], strikes[index + 1]])
    }
  }
  return ranges
}
