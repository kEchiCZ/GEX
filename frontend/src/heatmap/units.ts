/** Jednotky modelovaných Dyn ploch (#569): $/bod vs. $/1 %.

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

export type GexUnits = 'per_point' | 'per_percent'
export const GEX_UNITS: readonly GexUnits[] = ['per_point', 'per_percent']

export const GEX_UNIT_LABELS: Record<GexUnits, string> = {
  per_point: '$/bod',
  per_percent: '$/1 %',
}

/** Váha hodnoty na cenové hladině `price`; pro $/bod identita (1). */
export function priceWeight(price: number, units: GexUnits): number {
  return units === 'per_percent' ? (price * price) / 100 : 1
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
