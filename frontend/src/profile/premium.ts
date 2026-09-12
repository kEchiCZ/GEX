/** Prémie v $ per strana striku (#1126 bod 3c) — sdílené pravidlo pro P/C
panel (`pcr.ts`) i strike profil.

Prémie = počet × mid × multiplikátor. Mid = (bid+ask)/2 k ZOBRAZENÉ minutě —
u volume aproximace (neváží cenu v okamžiku obchodu, viz #486), u OI ocenění
držených pozic dnešní cenou. Zmrzlá kotace (ADR-0015, stale > práh) nebo
chybějící mid (0/undefined) stranu z prémie VYLUČUJE — tichá nula by vypadala
jako měření. Jedno místo pro to pravidlo, ať panel a profil nikdy neukážou
jiné složení.
*/
import { STALE_THRESHOLD_S } from '../heatmap/color'
import type { ProfileRow } from './bars'

export type ProfileUnit = 'contracts' | 'premium'
export const PROFILE_UNITS: readonly ProfileUnit[] = ['contracts', 'premium']
export const PROFILE_UNIT_LABELS: Record<ProfileUnit, string> = {
  contracts: 'Kontrakty',
  premium: 'Prémie $',
}

/** Použitelný mid strany, nebo null (zmrzlá kotace / mid chybí). */
export function usableMid(
  row: ProfileRow,
  side: 'call' | 'put',
  staleThresholdS: number = STALE_THRESHOLD_S,
): number | null {
  if ((row.staleAge ?? 0) > staleThresholdS) return null
  const mid = side === 'call' ? row.callMid : row.putMid
  return mid !== undefined && mid > 0 ? mid : null
}

export interface PremiumRows {
  /** Řádky s komponentami pruhů přepočtenými na $ (Vol i OI složka). */
  rows: ProfileRow[]
  /** Podíl kontraktů (Vol + OI) vyloučených kvůli chybějícímu/zmrzlému midu. */
  missingShare: number
  /** False = ani jedna strana nemá použitelný mid (Σ souhrn, replay bez
  kotací, načítání) → volající spadne na kontrakty a řekne to nahlas. */
  available: boolean
}

/** Přepočet komponent profilu na prémii v $ (#1126 bod 3c).

Kontrakty v profilu jsou Δ-vážené (`volume × |Δ|`, `oi × |Δ|`), aby ATM a
křídla byly srovnatelné. V prémii váhu |Δ| NAHRAZUJE mid — cena už polohu
vůči spotu nese, násobit ještě deltou by peníze zkreslilo. Složky:
- Vol   = `volume × mid × multiplikátor` (kolik peněz dnes na striku proteklo),
- OI Δ  = `oi × mid × multiplikátor` (co držené pozice stojí dnešní cenou).
Obě strany téhož striku sdílí jeden mid per strana, takže dvoutónový rozklad
outright/struktura (#1007, podíl) zůstává beze změny.
Strana bez použitelného midu má obě složky 0 a počítá se do `missingShare`;
řádek jen z archivu (#849) mid nemá z principu a do podílu nevstupuje.
*/
export function premiumRows(
  rows: ProfileRow[],
  multiplier: number,
  staleThresholdS: number = STALE_THRESHOLD_S,
): PremiumRows {
  let included = 0
  let excluded = 0
  const converted = rows.map((row) => {
    const callMid = usableMid(row, 'call', staleThresholdS)
    const putMid = usableMid(row, 'put', staleThresholdS)
    const callCount = row.callVolume + row.callOi
    const putCount = row.putVolume + row.putOi
    if (!row.archiveOnly) {
      if (callMid !== null) included += callCount
      else excluded += callCount
      if (putMid !== null) included += putCount
      else excluded += putCount
    }
    const callFactor = callMid === null ? 0 : callMid * multiplier
    const putFactor = putMid === null ? 0 : putMid * multiplier
    return {
      ...row,
      callVolComponent: row.callVolume * callFactor,
      callOiComponent: row.callOi * callFactor,
      putVolComponent: row.putVolume * putFactor,
      putOiComponent: row.putOi * putFactor,
    }
  })
  const total = included + excluded
  return {
    rows: converted,
    missingShare: total > 0 ? excluded / total : 0,
    available: included > 0,
  }
}
