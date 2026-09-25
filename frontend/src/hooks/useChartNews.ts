/** Zprávy pro markery grafu po seancích (#1290).

Graf dřív bral posledních 100 zpráv feedu. Při toku ~150 zpráv za hodinu to
pokrylo ~30 minut a starší markery během dne mizely — k upozornění na reakci
trhu pak v grafu nebyla zpráva, na kterou upozorňovalo. Jednotkou je proto
**seance** (obchodní den, ADR-0023), ne počet ani viewport:

- den = jeden request `GET /news/markers?from=open&to=close` bez stropu počtu;
- cache per datum seance — zprávy jsou pro ES i NQ tytéž, přepnutí instrumentu
  nic nestahuje; minulé dny se drží, dokud jsou v ose (historie #788);
- živý den: WS kanál `news` upsertuje podle id (bez stropu, bez řazení),
  každou minutu dotažení od posledního úspěšného dotažení − 30 min
  (klasifikace, výsledek makra, WS rámec zahozený plnou frontou, výpadek REST);
- **po výpadku odběru celý den znovu**: reconnect WS i (znovu)zapnutí vrstvy,
  návrat z Daily nebo z jiného dne — během pauzy nešel WS ani dotažení a díra
  by jinak zůstala do reloadu stránky;
- historické dny z osy (#788) se dotahují líně, po jednom (single-flight).

Chyba načtení je vidět (`error`) per den a zmizí úspěchem téhož dne — nezamění
se za den bez zpráv ani ji neschová úspěch jiného dne.

Výkon (#1274): každá změna `days`/`nowMs` znamená re-render grafu, proto se
stav mění jen se skutečnou změnou dat — dotažení beze změny drží identitu mapy
a „teď" se posune jen, když plánovaný event živého dne přejde do minulosti.
*/
import { useEffect, useRef, useState } from 'react'
import { fetchNewsMarkers } from '../api/news'
import type { ChartNewsRow, NewsRow } from '../api/news'
import type { LiveSocket } from '../api/ws'
import type { TimedNewsRow } from '../heatmap/newsMarkers'
import { sessionBoundsUtc, sessionDateIso } from '../instrument/tz'

/** Perioda dotažení konce živého dne — klasifikace i výsledek makra do minuty. */
export const CHART_NEWS_TAIL_MS = 60_000
/** Rezerva dotažení za posledním úspěšným: pokryje zpožděnou klasifikaci,
zprávy s opožděným `ts_event` i zahozené WS rámce. */
export const CHART_NEWS_TAIL_WINDOW_MS = 30 * 60_000
/** Po chybě načtení dne další pokus nejdřív za tolik ms (ne smyčka requestů). */
export const CHART_NEWS_RETRY_MS = 60_000
/** WS pushe se slévají — dávka klasifikace nese až stovky zpráv naráz a marker
má minutovou granularitu, takže 2 s zpoždění nic nestojí a šetří přestavby. */
export const CHART_NEWS_WS_FLUSH_MS = 2_000
/** Nad tolik dní v cache se zahazují dny, které osa nezobrazuje (~2 MB/den). */
const MAX_CACHED_DAYS = 8

export type DayNews = ReadonlyMap<number, TimedNewsRow>
export type ChartNewsDays = ReadonlyMap<string, DayNews>

export interface ChartNewsState {
  /** Zprávy per datum seance; mapa per den je klíčovaná id zprávy. */
  days: ChartNewsDays
  /** „Teď" pro nadcházející plánované eventy (dutý marker). Posouvá se s daty
      a když plánovaný event živého dne přejde do minulosti — ne každou minutu. */
  nowMs: number
  /** Chyby načtení dnů osy (datum: důvod); null = vše načtené nebo vrstva vypnutá. */
  error: string | null
}

function timed(row: ChartNewsRow): TimedNewsRow {
  return { ...row, tsMs: Date.parse(row.ts_event) }
}

/** Den z REST odpovědi — REST je autoritativní, řádek se nahrazuje celý. */
function dayFromRows(rows: ChartNewsRow[]): Map<number, TimedNewsRow> {
  const day = new Map<number, TimedNewsRow>()
  for (const row of rows) day.set(row.id, timed(row))
  return day
}

/** Zpráva z WS kanálu `news` v kompaktním tvaru; null = provozní hláška bez id. */
export function socketRow(data: Record<string, unknown>): TimedNewsRow | null {
  if (typeof data.id !== 'number' || typeof data.ts_event !== 'string') return null
  const tsMs = Date.parse(data.ts_event)
  if (Number.isNaN(tsMs)) return null
  const row = data as unknown as NewsRow
  return {
    id: row.id,
    ts_event: row.ts_event,
    kind: row.kind,
    category: row.category ?? null,
    importance: row.importance ?? null,
    title: typeof row.title === 'string' ? row.title : '',
    summary: row.summary ?? null,
    sentiment_dir: row.sentiment_dir ?? null,
    sentiment_score: row.sentiment_score ?? null,
    forecast: row.forecast ?? null,
    previous: row.previous ?? null,
    actual: row.actual ?? null,
    surprise_z: row.surprise_z,
    surprise_direction: row.surprise_direction,
    tsMs,
  }
}

/** Řádek `candidate` nic nemění — všechna jeho pole už `existing` má stejná. */
function sameRow(existing: TimedNewsRow, candidate: object): boolean {
  const current = existing as unknown as Record<string, unknown>
  for (const [key, value] of Object.entries(candidate)) {
    if (current[key] !== value) return false
  }
  return true
}

/** WS verze zprávy do existujícího řádku: null/chybějící pole nepřepisují.

Engine pushuje syrový titulek a news-engine klasifikaci vždy s
`forecast/previous/actual = null` — plné přepsání by u makra smazalo čísla
načtená z REST. */
function mergeSocket(existing: TimedNewsRow | undefined, incoming: TimedNewsRow): TimedNewsRow {
  if (!existing) return incoming
  const merged: Record<string, unknown> = { ...existing }
  for (const [key, value] of Object.entries(incoming)) {
    if (value !== null && value !== undefined) merged[key] = value
  }
  return merged as TimedNewsRow
}

/** Upsert dávky WS zpráv do načtených dnů (copy-on-write jen dotčených dnů).

Zpráva dne, který v cache není, se zahodí — přijde s jeho načtením. Dávka bez
změny (opakovaný push téže verze) vrací původní `days` — žádný re-render. */
export function mergeSocketRows(days: ChartNewsDays, rows: TimedNewsRow[]): ChartNewsDays {
  let next: Map<string, DayNews> | null = null
  const copied = new Map<string, Map<number, TimedNewsRow>>()
  for (const row of rows) {
    const date = sessionDateIso(row.tsMs)
    const source = days.get(date)
    if (!source) continue
    const existing = (copied.get(date) ?? source).get(row.id)
    const merged = mergeSocket(existing, row)
    if (existing && sameRow(existing, merged)) continue
    let day = copied.get(date)
    if (!day) {
      day = new Map(source)
      copied.set(date, day)
      next ??= new Map(days)
      next.set(date, day)
    }
    day.set(row.id, merged)
  }
  return next ?? days
}

/** Dotažení konce dne z REST: řádky se nahrazují celé (autoritativní zdroj).

Beze změny vrací původní `days` — minutové dotažení nesmí každou minutu
přestavět markery a překreslit graf (#1274). */
export function mergeRestRows(
  days: ChartNewsDays,
  date: string,
  rows: ChartNewsRow[],
): ChartNewsDays {
  const current = days.get(date)
  if (!current) return days
  let day: Map<number, TimedNewsRow> | null = null
  for (const row of rows) {
    const existing = current.get(row.id)
    if (existing && sameRow(existing, row)) continue
    day ??= new Map(current)
    day.set(row.id, timed(row))
  }
  if (!day) return days
  const next = new Map(days)
  next.set(date, day)
  return next
}

/** Plánovaný event z `rows`, který mezi `previousMs` a `nowMs` přešel do minulosti. */
function scheduledCrossed(
  rows: Iterable<TimedNewsRow>,
  previousMs: number,
  nowMs: number,
): boolean {
  for (const row of rows) {
    if (row.kind === 'scheduled' && row.tsMs > previousMs && row.tsMs <= nowMs) return true
  }
  return false
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

type DayErrors = ReadonlyMap<string, string>

/** Updater chyb per den: `message` null = den je v pořádku. Beze změny vrací
původní mapu (žádný re-render). */
function dayError(date: string, message: string | null): (previous: DayErrors) => DayErrors {
  return (previous) => {
    if (message === null ? !previous.has(date) : previous.get(date) === message) return previous
    const next = new Map(previous)
    if (message === null) next.delete(date)
    else next.set(date, message)
    return next
  }
}

export function useChartNews({
  enabled,
  viewDate,
  live,
  historyDates,
  socket,
}: {
  /** Markery zapnuté a intraday osa — jinak se nic nestahuje ani neodebírá. */
  enabled: boolean
  /** Zobrazený den (datum seance). */
  viewDate: string
  /** Zobrazený den je živý (dnešek) — dotažení, WS a reconnect. */
  live: boolean
  /** Historické seance v ose (#788), od nejnovější. */
  historyDates: readonly string[]
  socket?: LiveSocket
}): ChartNewsState {
  const [days, setDays] = useState<ChartNewsDays>(() => new Map())
  const [nowMs, setNowMs] = useState(() => Date.now())
  // Chyby per datum seance: maže je jen úspěch téhož dne (celý den i dotažení)
  const [errors, setErrors] = useState<DayErrors>(() => new Map())
  // Tik loaderu: po každém dokončeném načtení se podívá, jestli chybí další den
  const [loadTick, setLoadTick] = useState(0)
  // Dny načtené nebo v letu; mimo stav, aby loader nezávisel na identitě mapy
  const loadedRef = useRef<Set<string>>(new Set())
  // Den → start posledního úspěšného REST dotazu, který ho pokryl do konce seance.
  // Chybí = den se načítá celý (dotažení čeká) nebo není načtený vůbec.
  const syncedRef = useRef<Map<string, number>>(new Map())
  const failedRef = useRef<Map<string, number>>(new Map())
  const inFlightRef = useRef(false)
  const retryTimerRef = useRef<number | null>(null)
  // Zrcadla stavu pro časovače (updater setState musí zůstat čistý)
  const daysRef = useRef<ChartNewsDays>(days)
  const nowRef = useRef(nowMs)
  const historyKey = historyDates.join(',')

  useEffect(() => {
    daysRef.current = days
  }, [days])

  useEffect(
    () => () => {
      if (retryTimerRef.current !== null) window.clearTimeout(retryTimerRef.current)
    },
    [],
  )

  // Živý den: (znovu)zahájený odběr a minutové dotažení. Deklarováno PŘED
  // loaderem — zneplatnění dne při startu stihne loader v témže commitu.
  useEffect(() => {
    if (!enabled || !live) return
    // Během vypnutí vrstvy, Daily nebo prohlížení jiného dne nešel WS ani
    // dotažení: den se načte celý znovu, stejně jako po reconnectu. Poprvé
    // (den se ještě načítá nebo načtený není) se nic nezneplatňuje.
    if (syncedRef.current.has(viewDate)) {
      loadedRef.current.delete(viewDate)
      syncedRef.current.delete(viewDate)
    }
    const timer = window.setInterval(() => {
      const now = Date.now()
      // „Teď" se posune, jen když plánovaný event živého dne přešel do minulosti
      if (scheduledCrossed(daysRef.current.get(viewDate)?.values() ?? [], nowRef.current, now)) {
        nowRef.current = now
        setNowMs(now)
      }
      const synced = syncedRef.current.get(viewDate)
      if (synced === undefined) return // den se načítá celý
      const { openMs, closeMs } = sessionBoundsUtc(viewDate)
      // Od posledního úspěšného dotažení s rezervou — výpadek REST delší
      // než okno se tak zacelí, ne jen posledních 30 min
      const fromMs = Math.max(openMs, synced - CHART_NEWS_TAIL_WINDOW_MS)
      if (fromMs >= closeMs) return // seance skončila, přepnutí dne řeší App
      fetchNewsMarkers(fromMs, closeMs)
        .then((rows) => {
          const current = syncedRef.current.get(viewDate)
          // Mezitím zneplatněno (reconnect) → výsledek přinese celé načtení
          if (current === undefined) return
          syncedRef.current.set(viewDate, Math.max(current, now))
          setDays((previous) => mergeRestRows(previous, viewDate, rows))
          setErrors(dayError(viewDate, null))
        })
        .catch((failure: unknown) =>
          setErrors(dayError(viewDate, `dotažení: ${describe(failure)}`)),
        )
    }, CHART_NEWS_TAIL_MS)
    return () => window.clearInterval(timer)
  }, [enabled, live, viewDate])

  // Loader: nejdřív zobrazený den, pak historie od nejnovější; vždy jeden request
  useEffect(() => {
    if (!enabled || inFlightRef.current) return
    const wanted = [viewDate, ...(historyKey ? historyKey.split(',') : [])]
    const now = Date.now()
    const next = wanted.find((date) => {
      if (loadedRef.current.has(date)) return false
      const failedAt = failedRef.current.get(date)
      return failedAt === undefined || now - failedAt >= CHART_NEWS_RETRY_MS
    })
    if (next === undefined) return
    inFlightRef.current = true
    loadedRef.current.add(next)
    const { openMs, closeMs } = sessionBoundsUtc(next)
    fetchNewsMarkers(openMs, closeMs)
      .then((rows) => {
        failedRef.current.delete(next)
        // Zneplatněno během letu (reconnect, znovuzapnutí) → loader stáhne znovu
        if (loadedRef.current.has(next)) syncedRef.current.set(next, now)
        // Cache drží zobrazené dny; nad strop se zahodí ty, které osa nemá
        const keep = new Set(wanted)
        if (loadedRef.current.size > MAX_CACHED_DAYS) {
          for (const date of loadedRef.current) {
            if (keep.has(date)) continue
            loadedRef.current.delete(date)
            syncedRef.current.delete(date)
          }
        }
        const kept = new Set(loadedRef.current)
        kept.add(next)
        setDays((previous) => {
          const updated = new Map<string, DayNews>()
          for (const [date, day] of previous) if (kept.has(date)) updated.set(date, day)
          updated.set(next, dayFromRows(rows))
          return updated
        })
        setErrors(dayError(next, null))
        // Čerstvý den přepočítá i „teď" — v témže renderu jako data
        nowRef.current = Date.now()
        setNowMs(nowRef.current)
      })
      .catch((failure: unknown) => {
        loadedRef.current.delete(next)
        failedRef.current.set(next, Date.now())
        setErrors(dayError(next, describe(failure)))
        // Další pokus po pauze — vlastní časovač, ne závislost na jiném tiku
        if (retryTimerRef.current !== null) window.clearTimeout(retryTimerRef.current)
        retryTimerRef.current = window.setTimeout(() => {
          retryTimerRef.current = null
          setLoadTick((tick) => tick + 1)
        }, CHART_NEWS_RETRY_MS)
      })
      .finally(() => {
        inFlightRef.current = false
        setLoadTick((tick) => tick + 1)
      })
  }, [enabled, live, viewDate, historyKey, loadTick])

  // Živý push (#335) a reconnect: WS dávky se slévají, po výpadku celý den znovu
  useEffect(() => {
    if (!enabled || !live || !socket) return
    const pending: TimedNewsRow[] = []
    let flushTimer: number | null = null
    const flush = () => {
      flushTimer = null
      const batch = pending.splice(0)
      setDays((previous) => mergeSocketRows(previous, batch))
      // Vydaný plánovaný event z dávky nesmí do dalšího tiku zůstat „nadcházející"
      const now = Date.now()
      if (scheduledCrossed(batch, nowRef.current, now)) {
        nowRef.current = now
        setNowMs(now)
      }
    }
    const handler = (data: Record<string, unknown>) => {
      // Kanál `news` nese i provozní hlášky (retro pass) — ty nemají `id`
      const row = socketRow(data)
      if (!row) return
      pending.push(row)
      if (flushTimer === null) flushTimer = window.setTimeout(flush, CHART_NEWS_WS_FLUSH_MS)
    }
    socket.subscribe('news', handler)
    const offReconnect = socket.onReconnect(() => {
      // Během výpadku mohly zprávy utéct — celý den se načte znovu
      loadedRef.current.delete(viewDate)
      syncedRef.current.delete(viewDate)
      setLoadTick((tick) => tick + 1)
    })
    return () => {
      if (flushTimer !== null) window.clearTimeout(flushTimer)
      socket.unsubscribe('news', handler)
      offReconnect()
    }
  }, [enabled, live, socket, viewDate])

  // Chyby jen dnů, které osa chce — a jen se zapnutou vrstvou (Daily ji nemá)
  let error: string | null = null
  if (enabled && errors.size > 0) {
    const parts: string[] = []
    for (const date of [viewDate, ...historyDates]) {
      const message = errors.get(date)
      if (message !== undefined) parts.push(`${date}: ${message}`)
    }
    error = parts.length > 0 ? parts.join('; ') : null
  }

  return { days, nowMs, error }
}
