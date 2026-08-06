/** Převod „burzovní čas → UTC" nad IANA zónami (#511).

Protějšek engine `compute/settle.py`: časy seancí a settle jsou definované
v lokálním čase burzy (`America/New_York`, `Europe/London`, …) a na UTC se
převádí přes `Intl.DateTimeFormat` s `timeZone` — žádné externí závislosti,
žádná vlastní aproximace DST po celých dnech (ta se na přechodovém víkendu
míjela o hodinu, #159/#511). */

const dtfCache = new Map<string, Intl.DateTimeFormat>()

function formatter(timeZone: string): Intl.DateTimeFormat {
  let dtf = dtfCache.get(timeZone)
  if (!dtf) {
    dtf = new Intl.DateTimeFormat('en-US', {
      timeZone,
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
    dtfCache.set(timeZone, dtf)
  }
  return dtf
}

/** Offset zóny vůči UTC (ms, východně kladný) v okamžiku `ts` (epoch ms). */
function offsetMs(timeZone: string, ts: number): number {
  const parts = formatter(timeZone).formatToParts(ts)
  const get = (type: Intl.DateTimeFormatPartTypes): number =>
    Number(parts.find((p) => p.type === type)?.value ?? NaN)
  const asUtc = Date.UTC(
    get('year'),
    get('month') - 1,
    get('day'),
    get('hour'),
    get('minute'),
    get('second'),
  )
  // Zaokrouhlení na celé sekundy — formatToParts sekundy ořízne
  return asUtc - Math.floor(ts / 1000) * 1000
}

/** Epoch ms okamžiku „`hour`:`minute` dne `year`-`month`-`day` v zóně `timeZone`".

`month` je 1-based. Dvě iterace stačí: první odhad s offsetem UTC okamžiku,
druhá jej zpřesní — offset se mezi nimi liší nejvýš o DST hodinu. Neexistující
časy v den přechodu DST se přimknou k platnému okamžiku. */
export function zonedTimeUtc(
  timeZone: string,
  year: number,
  month: number,
  day: number,
  hour: number,
  minute: number,
): number {
  const naive = Date.UTC(year, month - 1, day, hour, minute)
  const guess = naive - offsetMs(timeZone, naive)
  return naive - offsetMs(timeZone, guess)
}
