/** Klasifikace expirací ES/NQ řetězu podle data (ADR-0001: tradingClass vzory).

Frontend zná jen datum expirace (YYYYMMDD) — typ se odvozuje kalendářně:
3. pátek = měsíční (v březnu/červnu/září/prosinci kvartální), poslední obchodní
den měsíce = EOM, jiný pátek = týdenní, jinak denní 0DTE. Odpočet míří na
settle 16:00 ET dne expirace (v létě 20:00 UTC, v zimě 21:00 — #511).
*/
import { zonedTimeUtc } from './tz'

export type ExpiryKind = 'denní' | 'týdenní' | 'měsíční' | 'kvartální' | 'EOM'

/** `20260728` → `2026-07-28`; null = nečitelný formát. */
export function expiryIsoDate(expiry: string): string | null {
  if (!/^\d{8}$/.test(expiry)) return null
  return `${expiry.slice(0, 4)}-${expiry.slice(4, 6)}-${expiry.slice(6, 8)}`
}

/** Den seance pro zvolenou expiraci (#352).

Proběhlá expirace nemá k dnešku žádná data (řetěz se přestal sweepovat dnem
expirace) — fetch dneška by navždy padal a UI by tiše zůstalo na demo datech.
Čte se proto replay jejího posledního dne = dne expirace. Aktuální a budoucí
expirace se čtou nad dnešní seancí (positioning zítřejšího řetězu se obchoduje
už dnes). */
export function sessionDateFor(expiry: string | null, today: string): string {
  const date = expiry ? expiryIsoDate(expiry) : null
  return date !== null && date < today ? date : today
}

function parse(expiry: string): Date | null {
  if (!/^\d{8}$/.test(expiry)) return null
  const year = Number(expiry.slice(0, 4))
  const month = Number(expiry.slice(4, 6))
  const day = Number(expiry.slice(6, 8))
  const date = new Date(Date.UTC(year, month - 1, day))
  return Number.isNaN(date.getTime()) ? null : date
}

function isThirdFriday(date: Date): boolean {
  return date.getUTCDay() === 5 && date.getUTCDate() >= 15 && date.getUTCDate() <= 21
}

function isLastTradingDayOfMonth(date: Date): boolean {
  // Další obchodní den (přeskočí víkend) už je v jiném měsíci
  const next = new Date(date)
  do {
    next.setUTCDate(next.getUTCDate() + 1)
  } while (next.getUTCDay() === 0 || next.getUTCDay() === 6)
  return next.getUTCMonth() !== date.getUTCMonth()
}

export function expiryKind(expiry: string): ExpiryKind | null {
  const date = parse(expiry)
  if (!date) return null
  if (isThirdFriday(date)) {
    return [2, 5, 8, 11].includes(date.getUTCMonth()) ? 'kvartální' : 'měsíční'
  }
  if (isLastTradingDayOfMonth(date)) return 'EOM'
  if (date.getUTCDay() === 5) return 'týdenní'
  return 'denní'
}

/** Settle dne expirace: 16:00 ET — DST-korektně přes IANA zónu (#511),
shodné s engine `compute/settle.py`. Kvartální expirace (3. pátek bře/čvn/
zář/pro) se vypořádá ráno v SOQ 9:30 ET (#1189, `expiry_settle_ts`). */
export function expirySettleUtc(expiry: string): Date | null {
  const date = parse(expiry)
  if (!date) return null
  const quarterly = expiryKind(expiry) === 'kvartální'
  return new Date(
    zonedTimeUtc(
      'America/New_York',
      date.getUTCFullYear(),
      date.getUTCMonth() + 1,
      date.getUTCDate(),
      quarterly ? 9 : 16,
      quarterly ? 30 : 0,
    ),
  )
}

/** Měsíční kódy kvartálního cyklu futures (CME): bře H, čvn M, zář U, pro Z. */
const QUARTER_CODES: Record<number, string> = { 2: 'H', 5: 'M', 8: 'U', 11: 'Z' }
/** Kořeny s kvartálním cyklem — pro jiné produkty (CL měsíční…) kód neodhadujeme. */
const QUARTERLY_ROOTS = new Set(['ES', 'MES', 'NQ', 'MNQ', 'RTY', 'M2K', 'YM', 'MYM'])

function thirdFridayUtc(year: number, month: number): Date {
  const firstDay = new Date(Date.UTC(year, month, 1)).getUTCDay()
  return new Date(Date.UTC(year, month, 1 + ((5 - firstDay + 7) % 7) + 14))
}

/** Roll futures na další kontrakt: 8 dnů před expirací (ADR-0039, #1189) —
stejné pravidlo jako engine (`GEXLENS_FRONT_ROLL_DAYS`), aby sidebar i deník
ukazovaly kontrakt, který engine skutečně sleduje (po rollu už Z6, ne U6). */
const FRONT_ROLL_DAYS = 8

/** TWS lokální symbol předního kvartálního kontraktu („ES" → „ESU6", #189).

Přední kontrakt = nejbližší kvartální měsíc, jehož roll date (3. pátek − 8 d)
je ještě v budoucnu; od roll date je přední ten další (ADR-0039). Jen
orientační pomůcka pro vyhledání grafu v TWS. */
export function frontContractCode(symbol: string, now: Date): string | null {
  if (!QUARTERLY_ROOTS.has(symbol)) return null
  for (let offset = 0; offset < 15; offset += 1) {
    const month = (now.getUTCMonth() + offset) % 12
    const year = now.getUTCFullYear() + Math.floor((now.getUTCMonth() + offset) / 12)
    const code = QUARTER_CODES[month]
    if (!code) continue
    const rollAt = thirdFridayUtc(year, month).getTime() - FRONT_ROLL_DAYS * 86_400_000
    if (now.getTime() < rollAt) {
      return `${symbol}${code}${year % 10}`
    }
  }
  return null
}

/** Lidský odpočet do expirace („≈ za 5 h 42 m"); null = už expirováno/nečitelné. */
export function expiryCountdown(expiry: string, now: Date): string | null {
  const settle = expirySettleUtc(expiry)
  if (!settle) return null
  const remainingMs = settle.getTime() - now.getTime()
  if (remainingMs <= 0) return null
  const totalMinutes = Math.round(remainingMs / 60_000)
  if (totalMinutes >= 48 * 60) {
    return `≈ za ${Math.round(totalMinutes / (24 * 60))} d`
  }
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  return hours > 0 ? `≈ za ${hours} h ${minutes} m` : `≈ za ${minutes} m`
}
