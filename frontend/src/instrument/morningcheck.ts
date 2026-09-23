/** Ranní checklist (#1241): šest otázek, které 21. 9. 2026 (trendový den po
kvartálním OPEX, NQ +580 b od 15:30) měly odpověď v datech, ale Briefing je
nesložil. Čistá funkce nad tím, co Briefing už načítá: útes gammy minulé
seance, vyšší TF trend, nejbližší zeď a její dominance, gap vůči PDC a flipu,
tendence, minuty do expirace. Každý bod nese hodnotu, verdikt a co dělat. */
import type { MapStateInfo } from './mapstate'
import { mapStateLabel } from './mapstate'
import type { TrendReport } from './trend'

export type CheckStatus = 'go' | 'watch' | 'calm' | 'na'

export interface CheckItem {
  key: string
  label: string
  /** Krátká hodnota („útes 83 %“, „call 30 300 · 18 %“). */
  value: string
  status: CheckStatus
  /** Co z toho plyne pro obchod — jedna věta. */
  action: string
}

export interface MorningCheckInput {
  /** Podíl gammy, který odpadl expirací minulé seance (0–1). */
  prevCliffShare: number | null
  prevCliffOpex: boolean
  /** Stav mapy TEĎ (#1245) z /status; null = kolektor neběží. */
  mapState: MapStateInfo | null
  trend: TrendReport | null
  price: number | null
  prevClose: number | null
  flip: number | null
  callWall: number | null
  putWall: number | null
  callWallDom: number | null
  putWallDom: number | null
  tendencyBand: string | null
  /** Minuty do expirace sledovaného řetězu; null bez dat. */
  minutesToExpiry: number | null
}

/** Útes, po kterém je tlumení tenké — zrcadlo enginu `dayverdict.CLIFF_DAMPING_OFF`. */
export const CLIFF_RANGE_DAY = 0.5
/** Zeď pod touto dominancí není brzda — zrcadlo `compute/tendency.WALL_WEAK_DOMINANCE`. */
export const WALL_WEAK = 0.25
/** Max Pain a tendence mají váhu až v posledních minutách do expirace. */
export const PIN_WINDOW_MIN = 90

const pct = (value: number) => `${Math.round(value * 100)} %`
const pts = (value: number) => `${Math.round(value)} b`

export function morningChecklist(input: MorningCheckInput): CheckItem[] {
  const items: CheckItem[] = []

  // 1. Útes gammy minulé seance
  if (input.prevCliffShare === null) {
    items.push({ key: 'cliff', label: 'Útes gammy', value: '—', status: 'na', action: 'Bez záznamu minulé seance (engine ho zapíše po settle).' }) // prettier-ignore
  } else if (input.prevCliffShare >= CLIFF_RANGE_DAY) {
    items.push({
      key: 'cliff',
      label: 'Útes gammy',
      value: `odpadlo ${pct(input.prevCliffShare)}${input.prevCliffOpex ? ' (OPEX)' : ''}`,
      status: 'go',
      action:
        'Struktura, která držela cenu, je pryč: čekej den rozsahu, ne pin. Fade zdí až po jejich potvrzení.',
    })
  } else {
    items.push({
      key: 'cliff',
      label: 'Útes gammy',
      value: `odpadlo ${pct(input.prevCliffShare)}`,
      status: 'calm',
      action: 'Mapa z minulé seance z větší části platí — zdi mají svou váhu.',
    })
  }

  // 1b. Stav mapy teď (#1245): co zbylo, ne co odpadlo
  if (input.mapState === null) {
    items.push({ key: 'map', label: 'Stav mapy', value: '—', status: 'na', action: 'Engine stav mapy nevyhodnocuje (map_state_enabled).' }) // prettier-ignore
  } else if (input.mapState.thin) {
    items.push({
      key: 'map',
      label: 'Stav mapy',
      value: mapStateLabel(input.mapState),
      status: 'go',
      action:
        'Tenká mapa: nic netlumí a nic nepinuje — čekej delší pohyby v obou směrech; setupy od zdi a pin k Max Pain vynech, dokud se mapa neobnoví.',
    })
  } else {
    items.push({
      key: 'map',
      label: 'Stav mapy',
      value: mapStateLabel(input.mapState),
      status: 'calm',
      action: 'Mapa má strukturu — zdi a Max Pain mají svou váhu.',
    })
  }

  // 2. Vyšší TF trend
  const higher = input.trend?.higher ?? null
  if (higher === null) {
    items.push({ key: 'trend', label: 'Vyšší TF trend', value: '—', status: 'na', action: 'Svíčky týden/den se načítají.' }) // prettier-ignore
  } else if (higher === 'up' || higher === 'down') {
    items.push({
      key: 'trend',
      label: 'Vyšší TF trend',
      value: higher === 'up' ? 'rostoucí' : 'klesající',
      status: 'go',
      action: `Bias dne ${higher === 'up' ? 'LONG' : 'SHORT'}; proti němu jen s potvrzením a polovičním sizingem.`,
    })
  } else {
    items.push({ key: 'trend', label: 'Vyšší TF trend', value: 'bez směru', status: 'calm', action: 'Bez biasu — obchoduj úrovně, ne směr.' }) // prettier-ignore
  }

  // 3. Nejbližší zeď a její dominance
  const price = input.price
  const nearest = nearestWall(input)
  if (price === null || nearest === null) {
    items.push({ key: 'wall', label: 'Nejbližší zeď', value: '—', status: 'na', action: 'Bez ceny nebo zdí.' }) // prettier-ignore
  } else {
    const distance = nearest.price - price
    const domText = nearest.dom === null ? 'dominance ?' : `dominance ${pct(nearest.dom)}`
    const weak = nearest.dom !== null && nearest.dom < WALL_WEAK
    items.push({
      key: 'wall',
      label: 'Nejbližší zeď',
      value: `${nearest.side} ${Math.round(nearest.price)} (${distance >= 0 ? '+' : '−'}${pts(Math.abs(distance))}, ${domText})`,
      status: weak ? 'go' : nearest.dom === null ? 'watch' : 'calm',
      action: weak
        ? 'Slabá zeď: průraz je pravděpodobnější než odraz — vstup na 5min akceptaci za zdí, stop pod konsolidaci, cíl další zeď.'
        : 'Silná zeď: odraz má smysl; po průrazu sleduj, jestli se zeď posouvá za cenou (pak nefaduj).',
    })
  }

  // 4. Gap vůči PDC a poloha vůči flipu
  if (price === null || input.prevClose === null) {
    items.push({ key: 'gap', label: 'Gap vůči PDC', value: '—', status: 'na', action: 'Bez ceny nebo settle minulé seance.' }) // prettier-ignore
  } else {
    const gap = price - input.prevClose
    const gapPct = gap / input.prevClose
    const aboveFlip = input.flip === null ? null : price > input.flip
    const flipText = aboveFlip === null ? '' : aboveFlip ? ', nad flipem' : ', pod flipem'
    const strong = Math.abs(gapPct) >= 0.004
    items.push({
      key: 'gap',
      label: 'Gap vůči PDC',
      value: `${gap >= 0 ? '+' : '−'}${pts(Math.abs(gap))} (${(gapPct * 100).toFixed(2)} %)${flipText}`,
      status: strong && (aboveFlip === null || aboveFlip === gap > 0) ? 'go' : 'calm',
      action: strong
        ? 'Gap-and-hold: když cena gap drží a konsoliduje pod/nad zdí, je to komprese před průrazem ve směru gapu.'
        : 'Malý gap — otevírá se uvnitř včerejší struktury, platí úrovně obratu.',
    })
  }

  // 5. Tendence a Max Pain: mimo posledních 90 min jen informace
  const minutes = input.minutesToExpiry
  const inPinWindow = minutes !== null && minutes <= PIN_WINDOW_MIN
  items.push({
    key: 'pin',
    label: 'Tendence a Max Pain',
    value: input.tendencyBand ? input.tendencyBand.replace('_', ' ') : '—',
    status: inPinWindow ? 'watch' : 'calm',
    action: inPinWindow
      ? 'Poslední hodina a půl do expirace: pin k Max Pain a tendence mají váhu.'
      : 'Přes den ber tendenci a Max Pain jen jako informaci — dokud se mapa hýbe, nehlasují proti trendu.',
  })

  // 6. Vlastní pravidla (z review #1188)
  items.push({
    key: 'rules',
    label: 'Riziko',
    value: 'max 50 $ / obchod, −100 $ = konec dne',
    status: 'watch',
    action: 'Stop v $ před vstupem, order s bracketem; 15 min pauza po stopu; max 4 obchody.',
  })
  return items
}

function nearestWall(input: MorningCheckInput): {
  side: 'call' | 'put'
  price: number
  dom: number | null
} | null {
  if (input.price === null) return null
  const candidates: Array<{ side: 'call' | 'put'; price: number; dom: number | null }> = []
  if (input.callWall !== null) candidates.push({ side: 'call', price: input.callWall, dom: input.callWallDom }) // prettier-ignore
  if (input.putWall !== null) candidates.push({ side: 'put', price: input.putWall, dom: input.putWallDom }) // prettier-ignore
  if (candidates.length === 0) return null
  const price = input.price
  candidates.sort((a, b) => Math.abs(a.price - price) - Math.abs(b.price - price))
  return candidates[0]
}
