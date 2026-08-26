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

/** Režim ze znaménka profilu, když nulou neprochází (#864).

Celý Dyn GEX profil na jedné straně nuly znamená, že flip leží mimo měřené
pásmo — režim je přitom v pásmu jednoznačný (typicky 0DTE ráno s dominancí
putů: celé pásmo negativní). Bere se uzel nejblíž spotu s clampem na okraje
gridu (spot mimo pásmo přebírá znaménko kraje — stejná extrapolace konstantou
jako render). Nula/NaN v uzlu → null.
*/
export function profileSignRegime(
  row: { gridStart: number; gridStep: number; values: number[] },
  spot: number,
): Extract<GexRegimeState, 'positive' | 'negative'> | null {
  if (row.values.length === 0 || row.gridStep <= 0) return null
  const index = Math.round((spot - row.gridStart) / row.gridStep)
  const clamped = Math.min(row.values.length - 1, Math.max(0, index))
  const value = row.values[clamped]
  if (!Number.isFinite(value) || value === 0) return null
  return value > 0 ? 'positive' : 'negative'
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
