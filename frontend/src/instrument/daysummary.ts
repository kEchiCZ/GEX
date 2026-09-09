/** Shrnutí dne v Briefingu (#1090, ADR-0035 §3–4): úrovně obratu, zprávy dne
s očekávanou reakcí a verdikt dne jako průhledné hlasování.

Čisté funkce nad daty, která Briefing už má (levels, referenční úrovně, EM,
trend, tendence, sentiment, ΔOI, kalendář). Nic se nedosazuje: chybějící
vstup = chybějící hlas s důvodem, ne tichá nula. Verdikt je heuristika
s pevnými vahami (varianta a v ADR-0035) — proto se ukládá a vyhodnocuje (#1091). */
import type { LevelsRow, OiDeltaSummary } from '../api/briefing'
import type { NewsRow, SentimentStateInfo } from '../api/news'
import { isHighImpact } from '../api/news'
import type { ReferenceLevels } from './referencelevels'
import type { TrendDirection, TrendReport } from './trend'
import { directionLabel } from './trend'

/** Verze pravidel hlasování — ukládá se s verdiktem, ať jde historie číst správně. */
export const VERDICT_RULES_VERSION = 1
/** Skóre ≥ +3 = spíše long, ≤ −3 = spíše short (ADR-0035 §3). */
export const VERDICT_THRESHOLD = 3
/** Konfluence: dvě úrovně do 0,25 % ceny od sebe. */
export const CONFLUENCE_SHARE = 0.0025
/** ΔOI hlasuje jen při rozdílu ≥ 10 % většího z totálů. */
export const OI_DELTA_MIN_SHARE = 0.1

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
  const result: TurnLevel[] = present.map((row) => ({
    price: row.price,
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

function magnitudeText(row: NewsRow, typical: TypicalReaction[]): string {
  const match = row.category ? typical.find((item) => item.category === row.category) : undefined
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

export interface VerdictVote {
  name: string
  vote: number
  reason: string
}

export interface DayVerdict {
  verdict: VerdictKind
  score: number
  votes: VerdictVote[]
  label: string
  /** Věta pro kartu a plán. */
  summary: string
}

export interface VerdictInput {
  trend: TrendReport | null
  /** Znaménko total GEX: true = pozitivní gamma; null bez levels. */
  positiveGamma: boolean | null
  /** Poslední pásmo tendence (#350): strong_short … strong_long; null bez dat. */
  tendencyBand: string | null
  sentiment: SentimentStateInfo | null
  price: number | null
  prevClose: number | null
  oiDelta: OiDeltaSummary | null
  /** High-impact zpráva mezi teď a US openem. */
  newsBeforeOpen: boolean
}

const TENDENCY_VOTES: Record<string, number> = {
  strong_short: -2,
  short: -1,
  neutral: 0,
  long: 1,
  strong_long: 2,
}

function directionVote(direction: TrendDirection | null, weight: number): number {
  if (direction === 'up') return weight
  if (direction === 'down') return -weight
  return 0
}

export const VERDICT_LABELS: Record<VerdictKind, string> = {
  long: 'Spíše LONG den',
  short: 'Spíše SHORT den',
  none: 'Bez převahy',
  wait_news: 'Počkat na tisk',
}

/** Hlasování s pevnými vahami (ADR-0035 §3); každý hlas nese důvod. */
export function dayVerdict(input: VerdictInput): DayVerdict {
  const votes: VerdictVote[] = []
  const { trend } = input
  const higher = trend?.higher ?? null
  const lower = trend?.lower ?? null
  votes.push({
    name: 'trend_higher',
    vote: directionVote(higher, 2),
    reason:
      higher === null ? 'vyšší TF bez dat' : `vyšší TF (týden, den) ${directionLabel(higher)}`,
  })
  votes.push({
    name: 'trend_lower',
    vote: directionVote(lower, 1),
    reason: lower === null ? 'nižší TF bez dat' : `nižší TF (4h, 1h, 15m) ${directionLabel(lower)}`,
  })
  const band = input.tendencyBand
  votes.push({
    name: 'tendency',
    vote: band !== null && band in TENDENCY_VOTES ? TENDENCY_VOTES[band] : 0,
    reason: band === null ? 'tendence bez dat' : `tendence ${band.replace('_', ' ')}`,
  })
  const sentiment = input.sentiment
  let sentimentVote = 0
  let sentimentReason = 'sentiment bez dat'
  if (sentiment) {
    if (sentiment.unconfirmed)
      sentimentReason = `sentiment ${sentiment.state} nepotvrzený — nehlasuje`
    else if (sentiment.state === 'RiskOn') {
      sentimentVote = 1
      sentimentReason = 'sentiment RiskOn'
    } else if (sentiment.state === 'RiskOff') {
      sentimentVote = -1
      sentimentReason = 'sentiment RiskOff'
    } else sentimentReason = 'sentiment Neutral'
  }
  votes.push({ name: 'sentiment', vote: sentimentVote, reason: sentimentReason })
  let overnightVote = 0
  let overnightReason = 'overnight vs. včerejší close bez dat'
  if (input.price !== null && input.prevClose !== null) {
    if (input.price > input.prevClose) {
      overnightVote = 1
      overnightReason = `cena ${input.price} nad včerejším close ${input.prevClose}`
    } else if (input.price < input.prevClose) {
      overnightVote = -1
      overnightReason = `cena ${input.price} pod včerejším close ${input.prevClose}`
    } else overnightReason = 'cena na včerejším close'
  }
  votes.push({ name: 'overnight', vote: overnightVote, reason: overnightReason })
  votes.push(oiDeltaVote(input.oiDelta))
  const partial = votes.reduce((sum, vote) => sum + vote.vote, 0)
  votes.push(gammaVote(input.positiveGamma, trend?.expected ?? null, partial))
  const score = votes.reduce((sum, vote) => sum + vote.vote, 0)
  let verdict: VerdictKind = 'none'
  if (score >= VERDICT_THRESHOLD) verdict = 'long'
  else if (score <= -VERDICT_THRESHOLD) verdict = 'short'
  if (input.newsBeforeOpen) verdict = 'wait_news'
  return {
    verdict,
    score,
    votes,
    label: VERDICT_LABELS[verdict],
    summary: verdictSummary(verdict, score),
  }
}

function oiDeltaVote(oiDelta: OiDeltaSummary | null): VerdictVote {
  const callDelta = oiDelta?.call_delta
  const putDelta = oiDelta?.put_delta
  if (!oiDelta?.days?.previous || callDelta === undefined || putDelta === undefined) {
    return { name: 'oi_delta', vote: 0, reason: 'ΔOI přes noc bez dat' }
  }
  const scale = Math.max(oiDelta.call_total ?? 0, oiDelta.put_total ?? 0)
  const diff = callDelta - putDelta
  if (scale <= 0 || Math.abs(diff) < scale * OI_DELTA_MIN_SHARE) {
    return { name: 'oi_delta', vote: 0, reason: 'ΔOI bez výrazné převahy call/put' }
  }
  return diff > 0
    ? { name: 'oi_delta', vote: 1, reason: `ΔOI převaha call (+${Math.round(diff)})` }
    : { name: 'oi_delta', vote: -1, reason: `ΔOI převaha put (${Math.round(diff)})` }
}

/** Negativní gamma přidává momentum ve směru trendu; pozitivní táhne skóre k nule. */
function gammaVote(
  positiveGamma: boolean | null,
  expected: TrendDirection | null,
  partial: number,
): VerdictVote {
  if (positiveGamma === null) return { name: 'gamma', vote: 0, reason: 'gamma režim bez dat' }
  if (!positiveGamma) {
    if (expected === 'up') return { name: 'gamma', vote: 1, reason: 'negativní gamma = momentum ve směru trendu (long)' } // prettier-ignore
    if (expected === 'down') return { name: 'gamma', vote: -1, reason: 'negativní gamma = momentum ve směru trendu (short)' } // prettier-ignore
    return { name: 'gamma', vote: 0, reason: 'negativní gamma, ale trend bez směru' }
  }
  if (partial > 0) return { name: 'gamma', vote: -1, reason: 'pozitivní gamma tlumí pohyb — převaha slabší' } // prettier-ignore
  if (partial < 0) return { name: 'gamma', vote: 1, reason: 'pozitivní gamma tlumí pohyb — převaha slabší' } // prettier-ignore
  return { name: 'gamma', vote: 0, reason: 'pozitivní gamma tlumí pohyb' }
}

function verdictSummary(verdict: VerdictKind, score: number): string {
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
