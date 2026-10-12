/** Měřič výkonu grafu (#1409, E-2a.1a): sběr bez UI, export JSON.

Cíle a postup jsou v #1274 a #1386 (měřené > odhadnuté). Měřič je ve výchozím
stavu vypnutý a vypnutý nic nealokuje: neběží smyčka rAF ani PerformanceObserver
a `perfStart` / `perfEnd` jsou jen větvení (instrumentované místo nepředává
closure). Zapnutý drží:
- **snímky** z rAF (délka snímku → fps, p50, p95),
- **dlouhé úlohy** ≥ 50 ms (`longtask`, počet a maximum za minutu),
- **operace** grafu (`perfStart` / `perfEnd`) s počítadly od zapnutí nebo resetu;
  počty jsou deterministické, časy ne (výkonový rozpočet v CI, E-2a.13),
- **spany „do vykreslení“** (`perfBegin` → `perfFinish`): načtení dne, přepnutí
  symbolu, Mode/Scale včetně kontur, minutový update,
- **kruhový buffer 30 s**: záznam nese čas a pohled (symbol, timeframe, Mode),
  starší se zahazuje.

UI měřič nemá: overlay, Diagnostika ani „Seklo to“ nevznikají (vlastník 12. 10.,
#1411). Ovládá se z konzole (`window.gexlensPerf`) a z Playwrightu (E-2a.13). */
import { APP_ENV, BUILD_SHA } from '../config'

/** Měřené operace grafu; jméno = počítadlo v exportu. */
export type PerfOp =
  | 'drawStatic'
  | 'drawDynamic'
  | 'buildModeGrid'
  | 'renderGrid'
  | 'minuteFlush'
  | 'dayFetch'
  | 'dayDecode'
  | 'dayBuild'

/** Spany od akce do vykreslení. */
export type PerfSpan = 'dayLoad' | 'symbolSwitch' | 'modeScale' | 'minuteUpdate'

export type PerfKind = 'frame' | 'longtask' | 'op' | 'span'

export interface PerfView {
  symbol: string
  timeframe: string
  mode: string
}

export interface PerfRecord {
  /** Konec záznamu v ms (časová osa `performance.now`). */
  t: number
  kind: PerfKind
  name: string
  ms: number
  view: PerfView
}

export interface PerfStats {
  count: number
  p50: number | null
  p95: number | null
  max: number | null
}

export interface PerfSummary {
  windowMs: number
  frames: PerfStats & { fps: number | null }
  longtasks: { count: number; maxMs: number | null; perMinute: LongtaskMinute[] }
  ops: Record<string, PerfStats>
  spans: Record<string, PerfStats>
}

export interface LongtaskMinute {
  /** Začátek minuty (ISO, UTC). */
  minute: string
  count: number
  maxMs: number
}

export interface PerfExport {
  schema: 1
  build: string
  env: string
  exportedAt: string
  view: PerfView
  summary: PerfSummary
  /** Počty operací a spanů od zapnutí nebo resetu (deterministické). */
  counters: Record<string, number>
  /** Pohledy, na které odkazují záznamy (`view` = index). */
  views: PerfView[]
  /** Záznamy bufferu; `t` = ms před `exportedAt` (záporné). Bez nich jen souhrn. */
  records: PerfExportRecord[]
}

export interface PerfExportRecord {
  t: number
  kind: PerfKind
  name: string
  ms: number
  view: number
}

/** Délka kruhového bufferu. */
export const PERF_WINDOW_MS = 30_000
/** Kolik minut dlouhých úloh drží souhrn „za minutu“. */
const LONGTASK_MINUTES_KEPT = 10
/** Kompakce pole až po tolika zahozených záznamech (amortizace). */
const COMPACT_AFTER = 512
/** `perfStart` u vypnutého měřiče — `perfEnd` pak nic nezapíše. */
const DISABLED = -1

let enabled = false
let clock: () => number = () => performance.now()
let view: PerfView = { symbol: '', timeframe: '', mode: '' }
const records: PerfRecord[] = []
let head = 0
const counters = new Map<string, number>()
const pendingSpans = new Map<PerfSpan, number>()
const longtaskMinutes = new Map<number, { count: number; maxMs: number }>()
let rafId: number | null = null
let lastFrame: number | null = null
let observer: PerformanceObserver | null = null
// Skrytá záložka zastaví rAF; snímek přes pauzu by byl sekundy dlouhý
const onVisibility = () => {
  if (document.visibilityState === 'hidden') lastFrame = null
}

export function perfEnabled(): boolean {
  return enabled
}

/** Zapnutí a vypnutí za běhu; vypnutí zastaví rAF i observer, data drží do resetu. */
export function setPerfEnabled(on: boolean): void {
  if (on === enabled) return
  enabled = on
  if (on) {
    startFrameLoop()
    startLongtaskObserver()
    if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibility)
  } else {
    stopFrameLoop()
    observer?.disconnect()
    observer = null
    pendingSpans.clear()
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }
}

/** Zahodí buffer, počítadla a rozpracované spany. */
export function resetPerf(): void {
  records.length = 0
  head = 0
  counters.clear()
  pendingSpans.clear()
  longtaskMinutes.clear()
  lastFrame = null
}

/** Aktuální pohled grafu; záznamy drží odkaz, takže objekt se nemění na místě. */
export function setPerfView(next: PerfView): void {
  if (next.symbol === view.symbol && next.timeframe === view.timeframe && next.mode === view.mode) {
    return
  }
  view = { ...next }
}

/** Začátek operace; vypnutý měřič vrátí značku, se kterou `perfEnd` nic nezapíše. */
export function perfStart(): number {
  return enabled ? clock() : DISABLED
}

export function perfEnd(op: PerfOp, t0: number): void {
  if (!enabled || t0 === DISABLED) return
  const now = clock()
  bump(op)
  push('op', op, now - t0, now)
}

/** Začátek spanu „do vykreslení“; opakovaný začátek přepíše dřívější.

`startedAt` = dřívější `perfStart()` (span začal před tím, než bylo jasné, že
skončí vykreslením — např. fetch dne, který může selhat). */
export function perfBegin(span: PerfSpan, startedAt?: number): void {
  if (!enabled || startedAt === DISABLED) return
  pendingSpans.set(span, startedAt ?? clock())
}

/** Konec spanu; bez rozpracovaného začátku nic. */
export function perfFinish(span: PerfSpan): void {
  if (!enabled) return
  const t0 = pendingSpans.get(span)
  if (t0 === undefined) return
  pendingSpans.delete(span)
  const now = clock()
  bump(span)
  push('span', span, now - t0, now)
}

/** Snímek z rAF (`now` = časové razítko callbacku). Exportované kvůli testům. */
export function perfRecordFrame(now: number): void {
  if (!enabled) return
  if (lastFrame !== null) push('frame', 'frame', now - lastFrame, now)
  lastFrame = now
}

/** Dlouhá úloha (`end` = startTime + duration). Exportované kvůli testům. */
export function perfRecordLongtask(end: number, ms: number): void {
  if (!enabled) return
  push('longtask', 'longtask', ms, end)
  const minute = Math.floor(wallClockMs(end) / 60_000)
  const bucket = longtaskMinutes.get(minute)
  if (bucket) {
    bucket.count += 1
    bucket.maxMs = Math.max(bucket.maxMs, ms)
  } else {
    longtaskMinutes.set(minute, { count: 1, maxMs: ms })
    for (const key of longtaskMinutes.keys()) {
      if (key <= minute - LONGTASK_MINUTES_KEPT) longtaskMinutes.delete(key)
    }
  }
}

export function perfCounters(): Record<string, number> {
  return Object.fromEntries(counters)
}

/** Záznamy z posledních 30 s, nejstarší první. */
export function perfRecords(now: number = clock()): PerfRecord[] {
  const cutoff = now - PERF_WINDOW_MS
  return records.slice(head).filter((record) => record.t >= cutoff)
}

export function perfSummary(now: number = clock()): PerfSummary {
  const window = perfRecords(now)
  const frames = window.filter((record) => record.kind === 'frame')
  const longtasks = window.filter((record) => record.kind === 'longtask')
  const frameMs = frames.reduce((sum, record) => sum + record.ms, 0)
  return {
    windowMs: PERF_WINDOW_MS,
    frames: {
      ...stats(frames.map((record) => record.ms)),
      // Z délek snímků, ne z hodin: pauza skryté záložky se nezapočítá
      fps: frameMs > 0 ? round((frames.length * 1000) / frameMs) : null,
    },
    longtasks: {
      count: longtasks.length,
      maxMs: longtasks.length > 0 ? Math.max(...longtasks.map((record) => record.ms)) : null,
      perMinute: [...longtaskMinutes.entries()]
        .sort(([a], [b]) => a - b)
        .map(([minute, bucket]) => ({
          minute: new Date(minute * 60_000).toISOString(),
          count: bucket.count,
          maxMs: round(bucket.maxMs),
        })),
    },
    ops: groupStats(window, 'op'),
    spans: groupStats(window, 'span'),
  }
}

/** Export pro protokoly v `docs/research/` a komentáře v #1274.

Pohled se v záznamech neopakuje (index do `views`), čas je relativní — 30 s
při 60 fps a pěti operacích na snímek tak má stovky kB místo MB. Do komentáře
GitHubu (strop 65 536 znaků) se vejde jen souhrn: `{ records: false }`. */
export function perfExport(now: number = clock(), options: { records?: boolean } = {}): PerfExport {
  const views: PerfView[] = []
  const viewIndex = new Map<PerfView, number>()
  const records =
    options.records === false
      ? []
      : perfRecords(now).map((record) => {
          let index = viewIndex.get(record.view)
          if (index === undefined) {
            index = views.length
            views.push(record.view)
            viewIndex.set(record.view, index)
          }
          return {
            t: Math.round((record.t - now) * 10) / 10,
            kind: record.kind,
            name: record.name,
            ms: round(record.ms),
            view: index,
          }
        })
  return {
    schema: 1,
    build: BUILD_SHA,
    env: APP_ENV,
    exportedAt: new Date(wallClockMs(now)).toISOString(),
    view: { ...view },
    summary: perfSummary(now),
    counters: perfCounters(),
    views,
    records,
  }
}

/** Testy: deterministické hodiny. */
export function setPerfClock(next: () => number): void {
  clock = next
}

function bump(name: string): void {
  counters.set(name, (counters.get(name) ?? 0) + 1)
}

function push(kind: PerfKind, name: string, ms: number, t: number): void {
  records.push({ t, kind, name, ms, view })
  const cutoff = t - PERF_WINDOW_MS
  while (head < records.length && records[head].t < cutoff) head += 1
  if (head > COMPACT_AFTER && head * 2 > records.length) {
    records.splice(0, head)
    head = 0
  }
}

function startFrameLoop(): void {
  if (typeof requestAnimationFrame !== 'function') return // jsdom bez rAF, SSR
  lastFrame = null
  const onFrame = (now: number) => {
    if (!enabled) return
    perfRecordFrame(now)
    rafId = requestAnimationFrame(onFrame)
  }
  rafId = requestAnimationFrame(onFrame)
}

function stopFrameLoop(): void {
  if (rafId !== null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(rafId)
  rafId = null
  lastFrame = null
}

function startLongtaskObserver(): void {
  if (typeof PerformanceObserver === 'undefined') return
  if (!PerformanceObserver.supportedEntryTypes?.includes('longtask')) return
  observer = new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      perfRecordLongtask(entry.startTime + entry.duration, entry.duration)
    }
  })
  observer.observe({ type: 'longtask', buffered: false })
}

/** Časová osa `performance.now` → epoch ms. */
function wallClockMs(t: number): number {
  return (typeof performance !== 'undefined' ? performance.timeOrigin : 0) + t
}

function groupStats(window: PerfRecord[], kind: PerfKind): Record<string, PerfStats> {
  const byName = new Map<string, number[]>()
  for (const record of window) {
    if (record.kind !== kind) continue
    const values = byName.get(record.name) ?? []
    values.push(record.ms)
    byName.set(record.name, values)
  }
  return Object.fromEntries([...byName].map(([name, values]) => [name, stats(values)]))
}

function stats(values: number[]): PerfStats {
  if (values.length === 0) return { count: 0, p50: null, p95: null, max: null }
  const sorted = [...values].sort((a, b) => a - b)
  return {
    count: sorted.length,
    p50: round(percentile(sorted, 0.5)),
    p95: round(percentile(sorted, 0.95)),
    max: round(sorted[sorted.length - 1]),
  }
}

/** Nejbližší pořadí (nearest-rank) nad seřazeným polem. */
function percentile(sorted: number[], p: number): number {
  const rank = Math.max(1, Math.ceil(p * sorted.length))
  return sorted[rank - 1]
}

function round(value: number): number {
  return Math.round(value * 100) / 100
}
