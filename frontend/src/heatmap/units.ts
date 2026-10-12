/** Jednotky modelovaných Dyn ploch: Dyn GEX $/bod vs. $/1 % (#569), Charm a Vanna dolarová delta (#1401).

Engine počítá a ukládá pole výhradně v $/bod (Γ·OI·M) — historie zůstává
bitově shodná a porovnatelná. Váha P²/100 (P = cena hladiny mřížky, ne spot)
je čistá funkce souřadnice, takže se aplikuje až při čtení/kreslení; $/1 %
říká „kolik dolarů podkladu musí dealeři přeobchodovat při pohybu o 1 %" —
vyšší cenové hladiny mají přirozeně větší váhu.

Váha se NIKDY nesmí aplikovat před interpolací (`sampleAt`): interpolace
a násobení P² nekomutují, násobí se až hodnota na cílové ceně, kde je P
přesně známé. Paritu s enginovým vzorcem (`price_weight_per_percent`
v compute/gexfield.py, pro band_regime #575) fixuje golden fixture
`engine/tests/golden/p2_weight_569.json`.
*/
import type { UnderlayPlane } from '../hooks/useGreekPlane'

export type GexUnits = 'per_point' | 'per_percent'
export const GEX_UNITS: readonly GexUnits[] = ['per_point', 'per_percent']

export const GEX_UNIT_LABELS: Record<GexUnits, string> = {
  per_point: '$/bod',
  per_percent: '$/1 %',
}

/** Jednotka Dyn plochy (#1401): GEX podle přepínače, Charm a Vanna v dolarové deltě.

Engine ukládá charm jako Δ·OI·M za den a vannu jako Δ·OI·M na 1 bod IV — nejde
o pohyb ceny, takže váha P²/100 ($/1 %) by dala jednotku bez smyslu (na ES
~79× víc). Dolarová delta = hodnota × P hladiny; přepínač $/bod × $/1 % platí
jen pro Dyn GEX. Golden `engine/tests/golden/greek_dollar_delta_1401.json`. */
export type PlaneUnits = GexUnits | 'dollar_delta'

/** Zapnutá Dyn plocha pod heatmapou. */
export type ActivePlane = Exclude<UnderlayPlane, 'off'>

/** Jednotka plochy: gex bere přepínač, charm a vanna vždy dolarovou deltu. */
export function planeUnits(plane: ActivePlane, gexUnits: GexUnits): PlaneUnits {
  return plane === 'gex' ? gexUnits : 'dollar_delta'
}

/** Popisek jednotky Charm a Vanna (#1401) — přepínač u nich není. */
export const GREEK_DOLLAR_DELTA_LABELS: Record<'charm' | 'vanna', string> = {
  charm: '$ delty / den',
  vanna: '$ delty / 1 bod IV',
}

/** Váha hodnoty na cenové hladině `price`: $/bod identita (1), $/1 % P²/100,
dolarová delta P (#1401). */
export function priceWeight(price: number, units: PlaneUnits): number {
  if (units === 'per_percent') return (price * price) / 100
  if (units === 'dollar_delta') return price
  return 1
}

/** Zváží profilový řádek per hladina mřížky.

Pro $/bod vrací TENTÝŽ objekt — bitová identita s dnešním chováním
a stabilní reference pro memoizaci. */
export function weightProfileRow<
  T extends { gridStart: number; gridStep: number; values: number[] },
>(row: T, units: GexUnits): T {
  if (units === 'per_point') return row
  const values = row.values.map(
    (value, index) => value * priceWeight(row.gridStart + index * row.gridStep, units),
  )
  return { ...row, values }
}
