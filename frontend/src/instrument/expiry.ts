/** Klasifikace expirací ES/NQ řetězu podle data (ADR-0001: tradingClass vzory).

Frontend zná jen datum expirace (YYYYMMDD) — typ se odvozuje kalendářně:
3. pátek = měsíční (v březnu/červnu/září/prosinci kvartální), poslední obchodní
den měsíce = EOM, jiný pátek = týdenní, jinak denní 0DTE. Odpočet míří na
přibližný settle 20:00 UTC (16:00 ET) dne expirace.
*/

export type ExpiryKind = 'denní' | 'týdenní' | 'měsíční' | 'kvartální' | 'EOM'

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

/** Přibližný settle: 20:00 UTC dne expirace (16:00 ET). */
export function expirySettleUtc(expiry: string): Date | null {
  const date = parse(expiry)
  if (!date) return null
  date.setUTCHours(20, 0, 0, 0)
  return date
}

/** Měsíční kódy kvartálního cyklu futures (CME): bře H, čvn M, zář U, pro Z. */
const QUARTER_CODES: Record<number, string> = { 2: 'H', 5: 'M', 8: 'U', 11: 'Z' }
/** Kořeny s kvartálním cyklem — pro jiné produkty (CL měsíční…) kód neodhadujeme. */
const QUARTERLY_ROOTS = new Set(['ES', 'MES', 'NQ', 'MNQ', 'RTY', 'M2K', 'YM', 'MYM'])

function thirdFridayUtc(year: number, month: number): Date {
  const firstDay = new Date(Date.UTC(year, month, 1)).getUTCDay()
  return new Date(Date.UTC(year, month, 1 + ((5 - firstDay + 7) % 7) + 14))
}

/** TWS lokální symbol předního kvartálního kontraktu („ES" → „ESU6", #189).

Přední kontrakt = nejbližší kvartální měsíc, jehož expirace (3. pátek) je
v budoucnu; v den expirace se kód přepne na další kontrakt (futures se stejně
rolují dřív). Jen orientační pomůcka pro vyhledání grafu v TWS. */
export function frontContractCode(symbol: string, now: Date): string | null {
  if (!QUARTERLY_ROOTS.has(symbol)) return null
  for (let offset = 0; offset < 15; offset += 1) {
    const month = (now.getUTCMonth() + offset) % 12
    const year = now.getUTCFullYear() + Math.floor((now.getUTCMonth() + offset) / 12)
    const code = QUARTER_CODES[month]
    if (!code) continue
    if (now.getTime() < thirdFridayUtc(year, month).getTime()) {
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
