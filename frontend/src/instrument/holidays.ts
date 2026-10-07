/** Kalendář svátků US akciového trhu (#1308, ADR-0046) — protějšek engine
`compute/settle` (`us_market_holidays`, `is_early_close`), mění se spolu.

Pravidla NYSE, ne udržovaný seznam dat: seznam tiše zastará, pravidla platí
každý rok. CME indexové futures v den svátku nemají settle ani US RTH;
zkrácená seance (den po Thanksgiving, Štědrý den, 3. 7.) končí v 13:00 ET. */

/** Datum jako `YYYY-MM-DD` z UTC složek. */
function iso(year: number, month: number, day: number): string {
  return new Date(Date.UTC(year, month - 1, day)).toISOString().slice(0, 10)
}

/** N-tý výskyt dne v týdnu (0 = neděle) v měsíci; n = −1 = poslední. */
function nthWeekday(year: number, month: number, weekday: number, n: number): string {
  if (n > 0) {
    const first = new Date(Date.UTC(year, month - 1, 1)).getUTCDay()
    return iso(year, month, 1 + ((weekday - first + 7) % 7) + 7 * (n - 1))
  }
  const lastDay = new Date(Date.UTC(year, month, 0))
  const back = (lastDay.getUTCDay() - weekday + 7) % 7
  return iso(year, month, lastDay.getUTCDate() - back)
}

/** Velikonoční neděle (gregoriánský výpočet, anonymní algoritmus). */
export function easterSundayIso(year: number): string {
  const a = year % 19
  const b = Math.floor(year / 100)
  const c = year % 100
  const d = Math.floor(b / 4)
  const e = b % 4
  const f = Math.floor((b + 8) / 25)
  const g = Math.floor((b - f + 1) / 3)
  const h = (19 * a + b - d - g + 15) % 30
  const i = Math.floor(c / 4)
  const k = c % 4
  const l = (32 + 2 * e + 2 * i - h - k) % 7
  const m = Math.floor((a + 11 * h + 22 * l) / 451)
  const month = Math.floor((h + l - 7 * m + 114) / 31)
  const day = ((h + l - 7 * m + 114) % 31) + 1
  return iso(year, month, day)
}

/** Svátek o víkendu se drží v pátek (sobota) nebo v pondělí (neděle). */
function observed(year: number, month: number, day: number): string {
  const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay()
  if (weekday === 6) return iso(year, month, day - 1)
  if (weekday === 0) return iso(year, month, day + 1)
  return iso(year, month, day)
}

function shiftIso(dateIso: string, days: number): string {
  const date = new Date(`${dateIso}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

const cache = new Map<number, Set<string>>()

/** Celodenní svátky NYSE roku `year` — dny bez US seance. */
export function usMarketHolidays(year: number): Set<string> {
  const cached = cache.get(year)
  if (cached) return cached
  const days = new Set([
    nthWeekday(year, 1, 1, 3), // Martin Luther King Jr.
    nthWeekday(year, 2, 1, 3), // Washington's Birthday
    shiftIso(easterSundayIso(year), -2), // Velký pátek
    nthWeekday(year, 5, 1, -1), // Memorial Day
    observed(year, 7, 4), // Den nezávislosti
    nthWeekday(year, 9, 1, 1), // Labor Day
    nthWeekday(year, 11, 4, 4), // Thanksgiving
    observed(year, 12, 25), // Vánoce
  ])
  // Nový rok v sobotu se nepřesouvá na pátek 31. 12.
  if (new Date(Date.UTC(year, 0, 1)).getUTCDay() !== 6) days.add(observed(year, 1, 1))
  if (year >= 2022) days.add(observed(year, 6, 19)) // Juneteenth
  cache.set(year, days)
  return days
}

/** Je `dateIso` celodenní svátek US akciového trhu? */
export function isUsMarketHoliday(dateIso: string): boolean {
  return usMarketHolidays(Number(dateIso.slice(0, 4))).has(dateIso)
}

/** Zkrácená seance (close 13:00 ET): den po Thanksgiving; 24. 12. a 3. 7. po–čt. */
export function isEarlyClose(dateIso: string): boolean {
  const weekday = new Date(`${dateIso}T00:00:00Z`).getUTCDay()
  if (weekday === 0 || weekday === 6 || isUsMarketHoliday(dateIso)) return false
  const year = Number(dateIso.slice(0, 4))
  if (dateIso === shiftIso(nthWeekday(year, 11, 4, 4), 1)) return true
  const monthDay = dateIso.slice(5)
  return monthDay === '12-24' || monthDay === '07-03'
}
