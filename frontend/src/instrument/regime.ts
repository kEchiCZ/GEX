/** GEX režim (#209): poloha spotu vůči flip ZÓNĚ (měřený × dynamický flip).

Jediná datově podložená hodnota GEX vrstvy je režimový přepínač — realizovaná
volatilita se v pozitivní/negativní gammě měřitelně liší (manuál kap. 18).
Režim neříká směr, říká TYP obchodu: pozitivní = fade/návraty, negativní =
průrazy/momentum, flip zóna = rozmazaná hranice → neobchodovat.
*/

export type GexRegimeState = 'positive' | 'negative' | 'flipzone'

/** Průchod nulou Dyn GEX profilu nejblíž spotu (dynamický flip, ADR-0009).

Zrcadlí konvenci engine `_flip` (více průchodů → nejbližší spotu) a interpolaci
z `gexCurvePaths` (profile/bars.ts). Bez průchodu nulou → null.
*/
export function profileZeroNearest(
  row: { gridStart: number; gridStep: number; values: number[] },
  spot: number,
): number | null {
  let best: number | null = null
  for (let index = 1; index < row.values.length; index += 1) {
    const previous = row.values[index - 1]
    const current = row.values[index]
    const previousSign = previous >= 0 ? 1 : -1
    const currentSign = current >= 0 ? 1 : -1
    if (previousSign === currentSign) continue
    const previousPrice = row.gridStart + (index - 1) * row.gridStep
    const zero = previousPrice + ((0 - previous) / (current - previous)) * row.gridStep
    if (best === null || Math.abs(zero - spot) < Math.abs(best - spot)) best = zero
  }
  return best
}

/** Režim z polohy spotu vůči flip zóně; null = nelze určit (chybí spot i flipy).

Zóna = interval mezi měřeným a dynamickým flipem (kap. 18 manuálu: rozjeté
čáry = rozmazaná hranice). K dispozici jen jeden flip → zóna je bod.
*/
export function gexRegime(
  spot: number | null,
  measuredFlip: number | null,
  dynamicFlip: number | null,
): GexRegimeState | null {
  if (spot === null) return null
  const flips = [measuredFlip, dynamicFlip].filter((value): value is number => value !== null)
  if (flips.length === 0) return null
  const low = Math.min(...flips)
  const high = Math.max(...flips)
  if (spot > high) return 'positive'
  if (spot < low) return 'negative'
  return 'flipzone'
}

export const REGIME_LABELS: Record<GexRegimeState, string> = {
  positive: 'Pozitivní gamma',
  negative: 'Negativní gamma',
  flipzone: 'Flip zóna',
}

/** Tooltip badge: co režim znamená pro typ obchodu — a co NEznamená (směr). */
export const REGIME_HINTS: Record<GexRegimeState, string> = {
  positive:
    'Dealeři tlumí pohyb — fungují návraty a odrazy od hran (fade), breakouty většinou selžou. ' +
    'Pozor: režim neříká směr — i klidný celodenní trend může běžet v pozitivní gammě.',
  negative:
    'Dealeři pohyb zesilují — fungují průrazy a momentum, fade proti trendu bývá přejetý. ' +
    'Širší stopy, menší pozice. Režim neříká směr, jen typ obchodu.',
  flipzone:
    'Spot uvnitř pásma mezi měřeným a dynamickým flipem — hranice režimů je rozmazaná, ' +
    'signály nečitelné. Vyčkat, až cena opustí celé pásmo (manuál kap. 18).',
}
