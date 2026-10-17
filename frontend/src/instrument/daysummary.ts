/** Shrnutí dne v Briefingu (#1090, ADR-0035 §3–4): úrovně obratu, zprávy dne
s očekávanou reakcí a popisky verdiktu dne.

Čisté funkce nad daty, která Briefing už má (levels, referenční úrovně, EM,
kalendář). Nic se nedosazuje: chybějící vstup = chybějící úroveň, ne tichá nula.
Verdikt dne (hlasování s pevnými vahami) počítá a ukládá jen engine
(`compute/dayverdict.py`, #1595); tady jsou jen jeho popisky pro kartu a plán. */
import type { LevelsRow } from '../api/briefing'
import type { NewsRow } from '../api/news'
import { isHighImpact } from '../api/news'
import type { ReferenceLevels } from './referencelevels'

/** Konfluence: dvě úrovně do 0,1 % ceny od sebe (NQ ≈ 30 b, ES ≈ 8 b). */
export const CONFLUENCE_SHARE = 0.001

// ── Úrovně obratu ────────────────────────────────────────────────

export type LevelKind = 'gamma' | 'reference' | 'em' | 'ema'

export interface TurnLevel {
  price: number
  label: string
  kind: LevelKind
  /** Nad cenou = odpor, pod cenou = podpora. */
  role: 'odpor' | 'podpora'
  /** Mechanika: proč by se tu cena měla otáčet. */
  note: string
  /** Vzdálenost od ceny v bodech (kladná). */
  distance: number
  /** Popisky dalších úrovní v konfluenci (do CONFLUENCE_SHARE ceny). */
  confluence: string[]
}

export interface TurnLevelInput {
  price: number | null
  levels: LevelsRow | null
  reference: ReferenceLevels | null
  em: { em: number; anchor: number } | null
  dailyEma20: number | null
}

interface RawLevel {
  price: number | null | undefined
  label: string
  kind: LevelKind
  note: string
}

/** Jeden seznam úrovní seřazený podle vzdálenosti od ceny; bez ceny prázdný. */
export function turnLevels(input: TurnLevelInput): TurnLevel[] {
  const { price, levels, reference, em, dailyEma20 } = input
  if (price === null) return []
  const raw: RawLevel[] = [
    { price: levels?.flip, label: 'Gamma flip', kind: 'gamma', note: 'změna režimu: nad tlumení, pod zesilování' }, // prettier-ignore
    { price: levels?.call_wall, label: 'Call wall', kind: 'gamma', note: 'brzda shora — dealer hedging tlumí růst' }, // prettier-ignore
    { price: levels?.put_wall, label: 'Put wall', kind: 'gamma', note: 'brzda zdola — dealer hedging tlumí pokles' }, // prettier-ignore
    { price: levels?.centroid, label: 'Těžiště GEX', kind: 'gamma', note: 'magnet positioningu' },
    { price: reference?.prevHigh, label: 'PDH', kind: 'reference', note: 'včerejší high — sleduje ho zbytek trhu' }, // prettier-ignore
    { price: reference?.prevLow, label: 'PDL', kind: 'reference', note: 'včerejší low — sleduje ho zbytek trhu' }, // prettier-ignore
    { price: reference?.prevClose, label: 'PDC', kind: 'reference', note: 'včerejší close — nad/pod = kdo drží den' }, // prettier-ignore
    { price: reference?.onHigh, label: reference?.onRunning ? 'ONH (běží)' : 'ONH', kind: 'reference', note: 'overnight high — test po openu' }, // prettier-ignore
    { price: reference?.onLow, label: reference?.onRunning ? 'ONL (běží)' : 'ONL', kind: 'reference', note: 'overnight low — test po openu' }, // prettier-ignore
    { price: em ? em.anchor + em.em : null, label: '+EM', kind: 'em', note: 'horní hrana očekávaného pohybu' }, // prettier-ignore
    { price: em ? em.anchor - em.em : null, label: '−EM', kind: 'em', note: 'dolní hrana očekávaného pohybu' }, // prettier-ignore
    { price: dailyEma20, label: 'EMA20 (den)', kind: 'ema', note: 'denní trendový průměr' },
  ]
  const present = raw.filter(
    (row): row is RawLevel & { price: number } =>
      typeof row.price === 'number' && Number.isFinite(row.price),
  )
  // Levels z enginu nesou plný float (flip 29496.894…) — do seznamu jde cena
  // zaokrouhlená na setiny, jinak řádek nejde přečíst
  const result: TurnLevel[] = present.map((row) => ({
    price: Math.round(row.price * 100) / 100,
    label: row.label,
    kind: row.kind,
    role: row.price >= price ? 'odpor' : 'podpora',
    note: row.note,
    distance: Math.abs(row.price - price),
    confluence: [],
  }))
  const tolerance = price * CONFLUENCE_SHARE
  for (const level of result) {
    level.confluence = result
      .filter((other) => other !== level && Math.abs(other.price - level.price) <= tolerance)
      .map((other) => other.label)
  }
  return result.sort((a, b) => a.distance - b.distance)
}

// ── Zprávy dne ───────────────────────────────────────────────────

export interface TypicalReaction {
  category: string
  windows: Record<string, { median_abs_bp: number; n: number }>
}

export interface NewsExpectation {
  row: NewsRow
  /** Čas v Europe/Prague, HH:MM. */
  timeLabel: string
  highImpact: boolean
  /** Event leží mezi teď a US openem. */
  beforeOpen: boolean
  /** Čtení směru z konvence řady (#462) — před tiskem jen „kterým směrem se čte překvapení". */
  direction: string
  /** Typická velikost z naměřených reakcí kategorie, nebo poctivé „bez měřené reakce". */
  magnitude: string
}

const PRAGUE_TIME = new Intl.DateTimeFormat('cs-CZ', {
  timeZone: 'Europe/Prague',
  hour: '2-digit',
  minute: '2-digit',
})

export function pragueTime(iso: string): string {
  return PRAGUE_TIME.format(new Date(iso))
}

function directionText(row: NewsRow): string {
  const sign = row.series_sign
  if (sign === 1) return 'nad konsensem = risk-on, pod = risk-off'
  if (sign === -1) return 'nad konsensem = risk-off, pod = risk-on'
  return 'směr překvapení bez konvence řady'
}

/** Kategorie bez vlastní řady — medián přes všechno „ostatní" nic neříká. */
const UNTYPED_CATEGORIES = new Set(['OTHER'])

function magnitudeText(row: NewsRow, typical: TypicalReaction[]): string {
  const category = row.category
  if (!category || UNTYPED_CATEGORIES.has(category)) return 'bez měřené reakce'
  const match = typical.find((item) => item.category === category)
  if (!match) return 'bez měřené reakce'
  const parts = Object.entries(match.windows)
    .sort((a, b) => Number(a[0]) - Number(b[0]))
    .map(([window, stat]) => `±${Math.round(stat.median_abs_bp)} bp/${window} min (n=${stat.n})`)
  return parts.length > 0 ? `typicky ${parts.join(', ')}` : 'bez měřené reakce'
}

/** Dnešní eventy s časem, směrem a typickou velikostí; High-impact napřed, pak podle času. */
export function newsExpectations(
  events: NewsRow[],
  typical: TypicalReaction[],
  usOpenMs: number,
  nowMs: number,
): NewsExpectation[] {
  return events
    .map((row) => {
      const at = Date.parse(row.ts_event)
      return {
        row,
        timeLabel: pragueTime(row.ts_event),
        highImpact: isHighImpact(row),
        beforeOpen: at > nowMs && at <= usOpenMs,
        direction: directionText(row),
        magnitude: magnitudeText(row, typical),
      }
    })
    .sort(
      (a, b) =>
        Number(b.highImpact) - Number(a.highImpact) || a.row.ts_event.localeCompare(b.row.ts_event),
    )
}

// ── Verdikt dne ──────────────────────────────────────────────────

export type VerdictKind = 'long' | 'short' | 'none' | 'wait_news'

/** Hlas jedné složky uloženého verdiktu (`briefing_verdicts.votes`, engine `Vote.as_dict`). */
export interface VerdictVote {
  name: string
  vote: number
  reason: string
}

export const VERDICT_LABELS: Record<VerdictKind, string> = {
  long: 'Spíše LONG den',
  short: 'Spíše SHORT den',
  none: 'Bez převahy',
  wait_news: 'Počkat na tisk',
}

/** Věta pro kartu a plán z uloženého verdiktu a skóre. */
export function verdictSummary(verdict: VerdictKind, score: number): string {
  const signed = `${score >= 0 ? '+' : ''}${score}`
  switch (verdict) {
    case 'long':
      return `Spíše long den (skóre ${signed}) — nákupy u podpor ve směru trendu, short jen jako fade u zdí.`
    case 'short':
      return `Spíše short den (skóre ${signed}) — prodeje u odporů ve směru trendu, long jen jako fade u zdí.`
    case 'wait_news':
      return `Před US openem přijde High-impact zpráva — převaha (skóre ${signed}) platí až po tisku, do té doby úrovně.`
    default:
      return `Bez převahy (skóre ${signed}) — den řídí úrovně a gamma režim, obchodovat od hrany k hraně.`
  }
}
