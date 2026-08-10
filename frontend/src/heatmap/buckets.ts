/** Plán timeframe košů: mapa minuta osy → koš, zarovnaná na otevření seance (#584).

Osa X je sjednocení minut se snapshotem a minut s barem (#459), takže její první
minuta padne kamkoliv — třeba na `23:59Z`. Kdyby se koš počítal z INDEXU minuty
(`floor(minuteIdx / bucketMinutes)`), hranice by se o ten zbytek posunuly a 5m koše
by běžely `10:59–11:03` místo `11:00–11:04`. Koš je proto určen časem.

Kotva je **otevření Globex seance 17:00 CT** (ADR-0023, `compute/marketclock.py`),
stejně jako v TradingView: koš `k` běží od `open + k × timeframe`. Pro timeframy,
které dělí hodinu (1m…30m, 1h), z toho vyjdou celé hodiny/minuty jako při kotvě na
půlnoc; u 45m/3h/4h se to liší — 4h koše startují na 17:00/21:00/01:00/05:00 CT
(18:00/22:00/02:00/06:00 ET), ne na celých čtyřhodinách UTC. Nová seance kotvu
resetuje, takže rozdělaný koš na hranici seance končí — jako v TV.

Koše bez jediné naměřené minuty se NEvytvářejí: osa zůstává hustá jako v 1m pohledu,
kde se díra v datech taky nekreslí jako prázdný sloupec. */
import { zonedDateParts, zonedTimeUtc } from '../instrument/tz'

export interface BucketPlan {
  bucketMinutes: number
  /** Počet košů nad naměřenými minutami. */
  buckets: number
  /** minuteIdx → bucketIdx. */
  bucketOf: Int32Array
  /** bucketIdx → první minuteIdx koše. */
  starts: Int32Array
  /** bucketIdx → poslední minuteIdx koše (včetně). */
  ends: Int32Array
  /** bucketIdx → wall-clock hranice koše v ms; `null` = indexový fallback (osa bez ISO). */
  startMs: Float64Array | null
}

/** Zóna a hodina otevření Globexu — shodné s engine `compute/marketclock.py`. */
const SESSION_TZ = 'America/Chicago'
const SESSION_OPEN_HOUR = 17

/** Poslední otevření seance v čase ≤ `ms` (epoch ms).

Okno se drží v jednoúrovňové cache: plán košů jde po ose vzestupně, takže se
`Intl` převod zaplatí jednou za seanci, ne za každou minutu. */
let sessionCache: { from: number; to: number; open: number } | null = null

function sessionOpenMs(ms: number): number {
  const hit = sessionCache
  if (hit && ms >= hit.from && ms < hit.to) return hit.open
  const openAt = (at: number): number => {
    const { year, month, day } = zonedDateParts(SESSION_TZ, at)
    return zonedTimeUtc(SESSION_TZ, year, month, day, SESSION_OPEN_HOUR, 0)
  }
  let open = openAt(ms)
  // Před 17:00 CT patří čas ještě do seance otevřené předchozí den (den v CT má
  // 23–25 h, takže −24 h vždy spadne na předchozí kalendářní datum)
  if (open > ms) open = openAt(ms - 24 * 3_600_000)
  const next = openAt(open + 24 * 3_600_000)
  sessionCache = { from: open, to: next > open ? next : open + 24 * 3_600_000, open }
  return open
}

/** Hranice koše, do kterého padne čas `ms` — zarovnaná na otevření seance. */
export function bucketStartMs(ms: number, bucketMinutes: number): number {
  const bucketMs = Math.max(1, bucketMinutes) * 60_000
  const open = sessionOpenMs(ms)
  return open + Math.floor((ms - open) / bucketMs) * bucketMs
}

function planFromBucketOf(
  bucketOf: Int32Array,
  buckets: number,
  bucketMinutes: number,
  startMs: Float64Array | null,
): BucketPlan {
  const starts = new Int32Array(buckets).fill(-1)
  const ends = new Int32Array(buckets).fill(-1)
  for (let minuteIdx = 0; minuteIdx < bucketOf.length; minuteIdx += 1) {
    const bucketIdx = bucketOf[minuteIdx]
    if (starts[bucketIdx] < 0) starts[bucketIdx] = minuteIdx
    ends[bucketIdx] = minuteIdx
  }
  return { bucketMinutes, buckets, bucketOf, starts, ends, startMs }
}

/** Indexový plán — pro osy bez ISO časů (demo den, Daily pohled). */
function indexPlan(minutes: number, bucketMinutes: number): BucketPlan {
  const buckets = Math.max(1, Math.ceil(minutes / bucketMinutes))
  const bucketOf = new Int32Array(minutes)
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    bucketOf[minuteIdx] = Math.min(buckets - 1, Math.floor(minuteIdx / bucketMinutes))
  }
  return planFromBucketOf(bucketOf, buckets, bucketMinutes, null)
}

/** Plán košů nad osou `minutesIso`; nečitelná/neúplná osa spadne na indexové koše. */
export function buildBucketPlan(
  minutesIso: string[],
  minutes: number,
  bucketMinutes: number,
): BucketPlan {
  if (bucketMinutes <= 1) return indexPlan(minutes, 1)
  if (minutesIso.length !== minutes || minutes === 0) return indexPlan(minutes, bucketMinutes)
  const bucketOf = new Int32Array(minutes)
  const boundaries: number[] = []
  let previousMs = Number.NEGATIVE_INFINITY
  let previousStart = Number.NaN
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    const ms = Date.parse(minutesIso[minuteIdx])
    // Neparsovatelná nebo nerostoucí osa: wall-clock koše by tichem zamíchaly
    // minuty přes sebe → radši dosavadní indexové chování
    if (Number.isNaN(ms) || ms <= previousMs) return indexPlan(minutes, bucketMinutes)
    previousMs = ms
    const start = bucketStartMs(ms, bucketMinutes)
    if (start !== previousStart) {
      boundaries.push(start)
      previousStart = start
    }
    bucketOf[minuteIdx] = boundaries.length - 1
  }
  return planFromBucketOf(bucketOf, boundaries.length, bucketMinutes, Float64Array.from(boundaries))
}

/** Plány se cachují na identitu osy — agregace živé vrstvy běží při každém spot ticku. */
const planCache = new WeakMap<string[], Map<string, BucketPlan>>()

export function cachedBucketPlan(
  minutesIso: string[],
  minutes: number,
  bucketMinutes: number,
): BucketPlan {
  let perAxis = planCache.get(minutesIso)
  if (!perAxis) {
    perAxis = new Map()
    planCache.set(minutesIso, perAxis)
  }
  // Klíč nese i délku mřížky: demo/Daily mají `minutesIso` prázdné, takže
  // samotná identita osy je pro ně nerozliší
  const key = `${bucketMinutes}:${minutes}`
  const hit = perAxis.get(key)
  if (hit) return hit
  const plan = buildBucketPlan(minutesIso, minutes, bucketMinutes)
  perAxis.set(key, plan)
  return plan
}

/** Fáze osy v minutách: o kolik minut je první minuta osy ZA hranicí svého koše.

Slouží převodu spojitý index koše ↔ index 1m osy (anotace, crosshair):
`indexNaOse = indexKoše × bucketMinutes − fáze`. */
export function bucketPhaseMinutes(minutesIso: string[], bucketMinutes: number): number {
  if (bucketMinutes <= 1 || minutesIso.length === 0) return 0
  const ms = Date.parse(minutesIso[0])
  if (Number.isNaN(ms)) return 0
  return (ms - bucketStartMs(ms, bucketMinutes)) / 60_000
}
