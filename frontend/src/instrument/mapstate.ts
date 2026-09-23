/** Stav „tenká mapa" (#1245): co z opční mapy TEĎ zbylo — na rozdíl od útesu
gammy (#576), který říká, co po settle odpadlo. Engine stav vyhodnocuje každou
minutu (`/status.map_state[symbol]`): tenká gamma (celkové GEX i gamma u ceny
pod 25. percentilem 20 seancí téhož symbolu), slabé zdi (dominance obou < 25 %)
a slitá mapa (flip, zdi a max pain do 0,25 % ceny od sebe); aspoň dvě ze tří
= tenká mapa. Fáze 1 jen ukazuje, nic se podle stavu neblokuje. */

export interface MapStateInfo {
  thin: boolean
  /** Podmínky: true/false = změřeno, null = bez dat (historie prahů, úrovně). */
  thin_gamma: boolean | null
  weak_walls: boolean | null
  fused: boolean | null
  reasons: string[]
  gex_abs: number | null
  gamma_abs: number | null
  spread_pct: number | null
  version: number
}

const CONDITION_LABELS: Array<[keyof MapStateInfo, string]> = [
  ['thin_gamma', 'tenká gamma (celkové GEX i gamma u ceny pod 25. percentilem historie)'],
  ['weak_walls', 'slabé zdi (dominance obou pod 25 %)'],
  ['fused', 'slitá mapa (flip, zdi a max pain do 0,25 % ceny)'],
]

function conditionMark(value: boolean | null): string {
  if (value === null) return '?'
  return value ? '✓' : '✗'
}

/** Tooltip chipu i bodu checklistu: co stav znamená a které podmínky platí. */
export function mapStateTooltip(state: MapStateInfo): string {
  // Nativní title respektuje odřádkování — podmínky pod sebou (vzor ivRankTooltip)
  const parts: string[] = [
    state.thin
      ? 'Tenká mapa (#1245): pozicování, které by cenu tlumilo nebo pinovalo, teď chybí.'
      : 'Mapa má strukturu (#1245): pozicování cenu tlumí/pinuje jako obvykle.',
    'Aspoň 2 ze 3 podmínek = tenká mapa (✓ platí, ✗ neplatí, ? bez dat):',
    ...CONDITION_LABELS.map(
      ([key, label]) => `${conditionMark(state[key] as boolean | null)} ${label}`,
    ),
  ]
  if (state.spread_pct !== null) {
    parts.push(`Rozpětí flip/zdi/max pain: ${(state.spread_pct * 100).toFixed(2)} % ceny.`)
  }
  parts.push(
    '',
    state.thin
      ? 'Co z toho plyne: bez tlumení a bez pinu — pohyby delší v obou směrech; zdi jen orientační, dokud se mapa neobnoví; setupy od zdi a pin k Max Pain nedávají smysl.'
      : 'Prahy jsou relativní k historii symbolu (20 seancí), ne absolutní. Jen měření, nic se nespíná.',
  )
  return parts.join('\n')
}

/** Krátký text pro chip/checklist: „tenká mapa (2/3)" nebo „mapa OK". */
export function mapStateLabel(state: MapStateInfo): string {
  const measured = [state.thin_gamma, state.weak_walls, state.fused].filter(
    (value) => value !== null,
  )
  const met = measured.filter(Boolean).length
  return state.thin ? `tenká mapa (${met}/3)` : `mapa OK (${met}/3)`
}
