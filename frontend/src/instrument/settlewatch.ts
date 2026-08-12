/** Settle watch (#603): klíčová úroveň dne + na které straně jsme — čisté funkce.

Denní teze referenčního tradera je jedna věta: „uzavřeme nad X?". Skládá se
z existujících dat: nejvýznamnější zeď (dominance ADR-0010) a vzdálenost ceny
od ní. Výběr úrovně: SILNÉ zdi (weak === false) mají přednost před slabými
a neznámými; uvnitř třídy vyhrává zeď nejblíž ceně — žádná nová logika,
jen složení toho, co už kreslíme.
*/

export interface WallCandidate {
  name: string
  level: number
  /** Slabá zeď dle dominance (ADR-0010); null = dominance neznámá. */
  weak: boolean | null
}

export interface SettleWatchInfo {
  name: string
  level: number
  /** spot − level: kladné = cena NAD úrovní. */
  distance: number
  weak: boolean
}

export function settleWatchLevel(
  candidates: WallCandidate[],
  spot: number | null,
): SettleWatchInfo | null {
  if (spot === null || !Number.isFinite(spot)) return null
  const valid = candidates.filter((candidate) => Number.isFinite(candidate.level))
  if (valid.length === 0) return null
  const strong = valid.filter((candidate) => candidate.weak === false)
  const pool = strong.length > 0 ? strong : valid
  const nearest = pool.reduce((best, candidate) =>
    Math.abs(candidate.level - spot) < Math.abs(best.level - spot) ? candidate : best,
  )
  return {
    name: nearest.name,
    level: nearest.level,
    distance: spot - nearest.level,
    weak: nearest.weak !== false,
  }
}

/** „nad 7800 −7,3 b" / „pod 7750 +2,1 b" — strana úrovně vůči ceně + odstup.

Znaménko = spot − úroveň (kladné: cena nad úrovní), takže „nad 7800 −7,3"
čteš „úroveň je NAD cenou a chybí 7,3 bodu" — přesně formulace z #603.
*/
export function formatSettleWatch(watch: SettleWatchInfo): string {
  const side = watch.distance < 0 ? 'nad' : 'pod'
  const signed = `${watch.distance >= 0 ? '+' : '−'}${Math.abs(watch.distance).toFixed(1)}`
  // GEX levels jsou interpolované floaty (#653) — bez zaokrouhlení by v hlavičce
  // stálo „nad 7628.166920999555"; Number() shodí koncovou nulu u celých strajků
  const level = Number(watch.level.toFixed(1))
  return `${side} ${level} ${signed} b`
}
