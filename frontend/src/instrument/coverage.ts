/** Pokrytí dat a jejich čerstvost (#470) — čisté funkce nad osou dne.

Motivace není kosmetická: 4. 8. 2026 trvalo hodinu zjistit, že v datech je 46minutová
díra (#459). Pokrytí barů to řekne jedním pohledem, protože se počítá proti ČASOVÉMU
rozpětí osy, ne proti počtu jejích sloupců — díra osu zkrátí, ale rozpětí zůstane.
*/

export interface Coverage {
  /** Minut, které mají bar. */
  covered: number
  /** Minut, které měla seance mít (časové rozpětí osy). */
  expected: number
  /** `covered / expected` v rozsahu 0–1. */
  ratio: number
}

/** Pokrytí OHLC: kolik minut osy má bar proti tomu, kolik jich mělo být.

`minutesIso` je 1m osa dne, `barMinutes` indexy minut se svíčkou (duplikáty nevadí).
`null` = osu nelze změřit (demo den, Daily pohled, nečitelné časy). */
export function ohlcCoverage(minutesIso: string[], barMinutes: number[]): Coverage | null {
  if (minutesIso.length < 2) return null
  const first = Date.parse(minutesIso[0])
  const last = Date.parse(minutesIso[minutesIso.length - 1])
  if (Number.isNaN(first) || Number.isNaN(last) || last < first) return null
  const expected = Math.round((last - first) / 60_000) + 1
  const covered = new Set(barMinutes).size
  return { covered, expected, ratio: expected > 0 ? Math.min(1, covered / expected) : 0 }
}

/** Pokrytí Greeks ze status kanálu; `null` = engine hodnoty (ještě) neposlal. */
export function greeksCoverage(
  complete: number | undefined,
  total: number | undefined,
): Coverage | null {
  if (complete === undefined || total === undefined || total <= 0) return null
  return { covered: complete, expected: total, ratio: Math.min(1, complete / total) }
}

/** Popisek pokrytí: `84/158 (53 %)`. */
export function coverageLabel(coverage: Coverage): string {
  return `${coverage.covered}/${coverage.expected} (${Math.round(coverage.ratio * 100)} %)`
}

/** Stáří posledních dat v minutách; `null` = není čas, ke kterému se vztáhnout. */
export function dataAgeMinutes(lastIso: string | null | undefined, now: Date): number | null {
  if (!lastIso) return null
  const last = Date.parse(lastIso)
  if (Number.isNaN(last)) return null
  return Math.max(0, (now.getTime() - last) / 60_000)
}

/** Kolik minut bez nových dat už znamená „stojí to". Sweep jede po minutě, takže
dvě minuty jsou ještě normální provoz a tři už ne. */
export const STALE_AFTER_MINUTES = 3
