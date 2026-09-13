/** Preset „Čistý pohled" (#238, #1126 bod 3e): jedno kliknutí = dominantní
Ridge + Max Pain + cena, vše ostatní ztišené — rychlé čtení „kde je největší
sázka" bez přepínání šesti checkboxů.

Preset NENÍ další sada voleb, ale dočasný přepis těch stávajících: před
zapnutím se uloží snímek všeho, na co sahá, a vypnutí snímek vrátí přesně
(i nevýchozí hodnoty). Obojí — příznak i snímek — se persistuje (ADR-0007),
takže refresh nechá pohled zapnutý a druhé kliknutí pořád vrací původní volby.
Čisté funkce, bez Reactu — App.tsx jen drží stav a volá settery. */
import type { ContoursMode } from '../heatmap/contours'
import type { PriceStyle } from '../heatmap/overlays'
import { WALLS_MODES } from '../heatmap/wallsModes'
import type { WallsMode } from '../heatmap/wallsModes'
import type { Toggles } from './AppState'
import type { Revive } from './persist'

/** Přepínače, na které preset sahá (snímek ukládá jen tyhle, ne celé Toggles). */
export const CLEAN_VIEW_TOGGLE_KEYS = [
  'dynGex',
  'secondaryWall',
  'gexLevels',
  'ladder',
  'flowAdjusted',
  'projection',
  'sessions',
  'news',
  'setups',
] as const satisfies readonly (keyof Toggles)[]
export type CleanViewToggleKey = (typeof CLEAN_VIEW_TOGGLE_KEYS)[number]

/** Všechny volby, které preset přepisuje — tvar snímku i cílového nastavení. */
export interface CleanViewSettings {
  walls: WallsMode
  contours: ContoursMode
  priceStyle: PriceStyle
  toggles: Record<CleanViewToggleKey, boolean>
}

export interface CleanViewState {
  active: boolean
  /** Snímek voleb před zapnutím; `null`, dokud preset nebyl zapnutý. */
  snapshot: CleanViewSettings | null
}

export const CLEAN_VIEW_OFF: CleanViewState = { active: false, snapshot: null }

/** Cílové volby presetu. GEX Levels zůstávají zapnuté (vrstva je jeden checkbox
= flip + těžiště + Max Pain, rozdělení uživatel odmítl) — na samotný Max Pain
ji zužuje filtr úrovní v App.tsx (`cleanViewLevels`). Dyn plocha se nemění. */
export const CLEAN_VIEW_TARGET: CleanViewSettings = {
  walls: 'ridge_dominant',
  contours: 'off',
  priceStyle: 'candles',
  toggles: {
    dynGex: false,
    secondaryWall: false,
    gexLevels: true,
    ladder: false,
    flowAdjusted: false,
    projection: false,
    sessions: false,
    news: false,
    setups: false,
  },
}

export const CLEAN_VIEW_TOOLTIP =
  'Čistý pohled: zapne Walls = Ridge dominantní (jediný nejsilnější hřeben), ' +
  'z GEX Levels nechá jen Max Pain, Contours vypne, cenu přepne na svíčky ' +
  'a vypne Zdi, 2. zeď, GEX žebřík, FA levels, Projekci, Sessions, News a Setupy. ' +
  'Dyn plocha zůstává. Druhé kliknutí vrátí všechny volby přesně tak, jak byly.'

/** Snímek aktuálních voleb — z celých Toggles bere jen klíče presetu. */
export function snapshotCleanView(current: {
  walls: WallsMode
  contours: ContoursMode
  priceStyle: PriceStyle
  toggles: Toggles
}): CleanViewSettings {
  const toggles = {} as Record<CleanViewToggleKey, boolean>
  for (const key of CLEAN_VIEW_TOGGLE_KEYS) toggles[key] = current.toggles[key]
  return {
    walls: current.walls,
    contours: current.contours,
    priceStyle: current.priceStyle,
    toggles,
  }
}

/** Filtr úrovní v čistém pohledu: jen Max Pain (jediná pojmenovaná úroveň presetu). */
export function cleanViewLevels<T extends { name: string }>(levels: T[]): T[] {
  return levels.filter((line) => line.name === 'max_pain')
}

const WALLS_VALUES: readonly string[] = WALLS_MODES.map((item) => item.value)
const CONTOURS_VALUES: readonly string[] = ['off', 'major', 'all']
const PRICE_STYLES: readonly string[] = ['line', 'candles']

function reviveSettings(value: unknown): CleanViewSettings | null {
  if (typeof value !== 'object' || value === null) return null
  const { walls, contours, priceStyle, toggles } = value as Record<string, unknown>
  if (typeof walls !== 'string' || !WALLS_VALUES.includes(walls)) return null
  if (typeof contours !== 'string' || !CONTOURS_VALUES.includes(contours)) return null
  if (typeof priceStyle !== 'string' || !PRICE_STYLES.includes(priceStyle)) return null
  if (typeof toggles !== 'object' || toggles === null) return null
  const revivedToggles = {} as Record<CleanViewToggleKey, boolean>
  for (const key of CLEAN_VIEW_TOGGLE_KEYS) {
    const stored = (toggles as Record<string, unknown>)[key]
    if (typeof stored !== 'boolean') return null
    revivedToggles[key] = stored
  }
  return {
    walls: walls as WallsMode,
    contours: contours as ContoursMode,
    priceStyle: priceStyle as PriceStyle,
    toggles: revivedToggles,
  }
}

/** Reviver (ADR-0007): aktivní preset bez platného snímku by neměl co vracet,
proto se rozbitý stav vrací celý na „vypnuto" — nikdy ne na aktivní bez snímku. */
export function revivedCleanView(): Revive<CleanViewState> {
  return (value, fallback) => {
    if (typeof value !== 'object' || value === null) return fallback
    const { active, snapshot } = value as Record<string, unknown>
    if (typeof active !== 'boolean') return fallback
    const revived = snapshot === null || snapshot === undefined ? null : reviveSettings(snapshot)
    if (active && revived === null) return fallback
    return { active, snapshot: revived }
  }
}
