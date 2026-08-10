/** Plán timeframe košů: mapa minuta osy → koš, zarovnaná na wall-clock (#584).

Osa X je sjednocení minut se snapshotem a minut s barem (#459), takže její první
minuta padne kamkoliv — třeba na `23:59Z`. Kdyby se koš počítal z INDEXU minuty
(`floor(minuteIdx / bucketMinutes)`), hranice by se o ten zbytek posunuly a 5m koše
by běžely `10:59–11:03` místo `11:00–11:04`. Koš je proto určen wall-clockem:
`floor(epochMs / bucketMs)`, tedy shodně s TradingView.

Kotva je UTC midnight = celé hodiny burzovního (ET) času, takže 1m…1h vyjdou 1:1
s TradingView. 45m/2h/3h/4h TV kotví na open seance — ty zůstávají na UTC kotvě.

Koše bez jediné naměřené minuty se NEvytvářejí: osa zůstává hustá jako v 1m pohledu,
kde se díra v datech taky nekreslí jako prázdný sloupec. */
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

/** Wall-clock hranice koše, do kterého padne čas `ms`. */
export function bucketStartMs(ms: number, bucketMinutes: number): number {
  const bucketMs = Math.max(1, bucketMinutes) * 60_000
  return Math.floor(ms / bucketMs) * bucketMs
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
