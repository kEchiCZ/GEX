/** Kalendář expirací a rollu futures (#1189, ADR-0039): REST klient + popisky.

Engine počítá fáze kvartálního týdne (roll date čt −8 d, OPEX týden, SOQ pá
9:30 ET, pondělí po) a značky do grafu; UI je jen zobrazuje — chip v hlavičce,
⌛ v ose grafu, karta v Briefingu. */
import { API_BASE } from '../config'

export type ExpiryPhase = 'normal' | 'roll' | 'opex_week' | 'expiry_day' | 'post_opex'
export type CalendarMarkerKind = 'roll' | 'quarterly_expiry' | 'monthly_opex' | 'vix_expiry'

export interface CalendarMarkerRow {
  date: string
  kind: CalendarMarkerKind
  /** UTC okamžik (SOQ 9:30 ET, settle 16:00 ET, roll 9:30 ET) pro intradenní osu. */
  ts: string
  label: string
}

export interface ExpiryCalendar {
  today: string
  phase: ExpiryPhase
  quarterly_expiry: string
  roll_date: string
  soq_ts: string
  days_to_expiry: number
  is_opex_week: boolean
  vix_expiry: string
  previous_expiry: string
  markers: CalendarMarkerRow[]
}

const PHASES: readonly string[] = ['normal', 'roll', 'opex_week', 'expiry_day', 'post_opex']

export async function fetchExpiryCalendar(date?: string): Promise<ExpiryCalendar | null> {
  try {
    const query = date ? `?date=${encodeURIComponent(date)}` : ''
    const response = await fetch(`${API_BASE}/calendar/expiry${query}`)
    if (!response.ok) return null
    const payload = (await response.json()) as Partial<ExpiryCalendar> | null
    if (
      !payload ||
      typeof payload.phase !== 'string' ||
      !PHASES.includes(payload.phase) ||
      typeof payload.quarterly_expiry !== 'string'
    ) {
      return null
    }
    return { ...(payload as ExpiryCalendar), markers: payload.markers ?? [] }
  } catch {
    return null
  }
}

/** `2026-09-18` → `18. 9.` */
export function shortDate(iso: string): string {
  const [, month, day] = iso.split('-')
  return `${Number(day)}. ${Number(month)}.`
}

/** Kód kvartálního kontraktu z data expirace (`2026-09-18` → `U6`). */
export function contractCode(expiryIso: string): string {
  const [year, month] = expiryIso.split('-')
  const code = { '03': 'H', '06': 'M', '09': 'U', '12': 'Z' }[month] ?? '?'
  return `${code}${year.slice(-1)}`
}

function localTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/** Text chipu v hlavičce; null = běžný den (chip se nekreslí). */
export function phaseChipLabel(calendar: ExpiryCalendar): string | null {
  const expiry = shortDate(calendar.quarterly_expiry)
  const code = contractCode(calendar.quarterly_expiry)
  const soq = localTime(calendar.soq_ts)
  switch (calendar.phase) {
    case 'roll':
      return `roll proběhl ${shortDate(calendar.roll_date)} · expirace ${code} pá ${expiry} ${soq}`
    case 'opex_week':
      return `OPEX týden · expirace ${code} pá ${expiry} ${soq} (SOQ)`
    case 'expiry_day':
      return `kvartální expirace ${code} dnes ${soq} (SOQ)`
    case 'post_opex':
      return `po OPEXu (${expiry}) — bez opční podpory`
    default:
      return null
  }
}

/** Tooltip chipu — odřádkovaný, co fáze znamená (#1189, článek 15. 9. 2026). */
export function phaseTooltip(calendar: ExpiryCalendar): string {
  const code = contractCode(calendar.quarterly_expiry)
  const common = [
    '',
    'Kvartální expirační týden (3. pátek bře/čvn/zář/pro):',
    `• roll futures = čt 8 dní před expirací (${shortDate(calendar.roll_date)}): objem přechází do dalšího kontraktu; aplikace od té chvíle jede na něm`,
    `• SOQ = pátek 9:30 ET: futures ${code} a kvartální opce se vypořádají z otevíracích cen indexu — settle ráno, ne odpoledne`,
    `• VIX expirace st ${shortDate(calendar.vix_expiry)} ráno`,
    '• do pátku je cena tažená hedgingem dealerů, ne náladou — sentiment ber jako šum',
    '• po pátku zmizí put/call wall expirovaného řetězu; pondělí bývá bez brzdy (vol re-expansion, nebo odkup hedgů)',
  ]
  const head: Record<ExpiryPhase, string> = {
    normal: `Do kvartální expirace ${code} zbývá ${calendar.days_to_expiry} d.`,
    roll: `Roll proběhl — front kontrakt je další (${nextContractCode(code)}), ${code} dobíhá s tenkým objemem.`,
    opex_week: `OPEX týden: expirace ${code} v pátek ráno (SOQ). Pinning k velkým strikům, hedging tahá cenu.`,
    expiry_day: `Dnes kvartální expirace ${code}: SOQ v 9:30 ET, pak tichý drift k strikům, na close rebalance indexu.`,
    post_opex:
      'Týden po kvartální expiraci: bez opční podpory, sezónně slabý; nálada se do ceny propisuje až teď.',
  }
  return [head[calendar.phase], ...common].join('\n')
}

export function nextContractCode(code: string): string {
  const order = ['H', 'M', 'U', 'Z']
  const letter = code[0]
  const year = Number(code.slice(1))
  const index = order.indexOf(letter)
  if (index < 0 || !Number.isFinite(year)) return '?'
  const next = order[(index + 1) % 4]
  return `${next}${index === 3 ? (year + 1) % 10 : year}`
}
