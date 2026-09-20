/** Hodiny US akciového trhu (#206): 9:30–16:00 America/New_York, DST-korektně,
víkend = zavřeno. Zrcadlo enginu `compute/marketclock.outside_us_rth`. */

const NY_FORMAT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hour: 'numeric',
  minute: 'numeric',
  hour12: false,
  weekday: 'short',
})

/** Minuty od půlnoci NY + den v týdnu (0 = neděle). */
export function newYorkClock(now: Date): { minutes: number; weekday: number } {
  const parts = NY_FORMAT.formatToParts(now)
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? ''
  const hour = Number(get('hour')) % 24
  const minute = Number(get('minute'))
  const weekday = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(get('weekday'))
  return { minutes: hour * 60 + minute, weekday }
}

/** Ticho tasty streamu, které je v US RTH porucha (#1228, zrcadlo enginu
`tasty/watchdog.SILENT_RTH_S`). */
export const FEED_SILENT_AFTER_MIN = 3

/** Kolik minut tasty stream mlčí, pokud je to při otevřeném trhu porucha;
null = v pořádku, nebo trh stojí (mimo RTH je ticho normální — 19. 9. 2026
sobota byla falešný poplach). */
export function feedSilenceMinutes(lastEventTs: string | undefined, now: Date): number | null {
  if (!lastEventTs || outsideUsRth(now)) return null
  const last = Date.parse(lastEventTs)
  if (!Number.isFinite(last)) return null
  const minutes = Math.floor((now.getTime() - last) / 60_000)
  return minutes >= FEED_SILENT_AFTER_MIN ? minutes : null
}

/** Mimo US RTH (9:30–16:00 ET)? Víkend = mimo. */
export function outsideUsRth(now: Date): boolean {
  const { minutes, weekday } = newYorkClock(now)
  if (weekday === 0 || weekday === 6) return true
  return minutes < 9 * 60 + 30 || minutes >= 16 * 60
}
