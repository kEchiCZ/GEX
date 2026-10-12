/* Baseline výkonu grafu (#1412, E-2a.2): konzolový sběrač nad měřičem #1409.

Celý soubor se vloží do konzole produkce (:8080) ve VIDITELNÉM okně Chrome.
Zakryté okno škrtí rAF až na ~1 fps a snímky pak nejsou reprezentativní.

  pb.start()               // zapne měřič a LoAF a začne skládat záznamy
  await pb.gate()          // brána: viditelná záložka, klidová frekvence snímků
  await pb.run('1m', false) // aktivní scénáře na ES: timeframe × Čistý pohled (true/false)
  pb.report()              // souhrn po segmentech a pasivní okno (JSON)
  await pb.restore()       // vrátí původní volby (symbol, timeframe, pohled, Mode…)
  pb.stop()

Po reloadu stránky (studené načtení dne) se soubor vloží znovu a zavolá
`pb.coldLoad()`. Ta čte bufferované LoAF, resource timing `/api/replay` a navigaci.

Výchozí volby si sběrač uloží do sessionStorage záložky při prvním `start()`, takže
přežijí i opětovné vložení souboru. `restore()` je vrátí a záznam smaže.

Aplikaci sběrač nemění, jen přepíná ovládací prvky jako uživatel. Měřič drží
30 s (PERF_WINDOW_MS), proto sběrač každých 5 s vybere nové záznamy a skládá je.
p50/p95 tak jde spočítat přes libovolně dlouhé okno: minutový update, hodinový
refetch. Snímky se drží jen v aktivních segmentech. Mimo ně zůstane histogram
a snímky ≥ 20 ms, jinak by hodina při 144 Hz měla půl milionu záznamů.

Pojistky gesta: anotaci tažení v režimu Kurzor přesune a uloží (#589) a range
(#484) posune. Pan proto začne jen v režimu Kurzor a jen v bodě, kde canvas po
najetí neukazuje jiný kurzor než výchozí (anotace, range ani osa). */
;(() => {
  const P = window.gexlensPerf
  if (!P || typeof P.perfRecords !== 'function') {
    throw new Error('window.gexlensPerf chybí: build bez měřiče #1409')
  }
  const PASSIVE_FRAME_MIN_MS = 20
  const FRAME_BUCKETS = [8.4, 16.7, 25, 33.4, 50, 100, 250, Infinity]
  const HARVEST_MS = 5_000
  // Přepnutí na ES trvalo 12. 10. před RTH 20,5 s (fetch /api/replay 18,3 s)
  const SYMBOL_TIMEOUT_MS = 60_000
  const seen = new WeakSet()
  const log = [] // {t, kind, name, ms, view}
  const loaf = [] // long-animation-frame (≥ 50 ms)
  const segments = [] // {name, tf, clean, t0, t1, samples, …}
  const histogram = FRAME_BUCKETS.map(() => 0)
  const visibility = [] // {t, state}: zakryté okno Windows hlásí jako `hidden`
  const resources = [] // /api/replay a /news/stats: buffer resource timing (250) je po načtení plný
  let frameMsTotal = 0
  let harvestTimer = null
  let loafObserver = null
  let resourceObserver = null
  let startedAt = null
  let initial = null

  const now = () => performance.now()
  const frame = () => new Promise((resolve) => requestAnimationFrame(resolve))
  const frames = async (n) => {
    for (let i = 0; i < n; i += 1) await frame()
  }
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
  // Konec vykreslení: rAF dalšího snímku a pak úloha po něm (paint je hotový)
  const afterPaint = () =>
    new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)))
  const round = (value) => (value == null ? null : Math.round(value * 10) / 10)

  function stats(values) {
    if (values.length === 0) return { n: 0, p50: null, p95: null, max: null }
    const sorted = [...values].sort((a, b) => a - b)
    // Nejbližší pořadí, stejně jako perfSummary měřiče
    const at = (p) => sorted[Math.max(1, Math.ceil(p * sorted.length)) - 1]
    return { n: sorted.length, p50: round(at(0.5)), p95: round(at(0.95)), max: round(at(1)) }
  }

  function inSegment(t) {
    for (let i = segments.length - 1; i >= 0; i -= 1) {
      const s = segments[i]
      if (t >= s.t0 && (s.t1 === null || t <= s.t1)) return s
    }
    return null
  }

  function harvest() {
    for (const r of P.perfRecords()) {
      if (seen.has(r)) continue
      seen.add(r)
      if (startedAt !== null && r.t < startedAt) continue
      if (r.kind === 'frame') {
        histogram[FRAME_BUCKETS.findIndex((limit) => r.ms <= limit)] += 1
        frameMsTotal += r.ms
        if (r.ms < PASSIVE_FRAME_MIN_MS && !inSegment(r.t)) continue
      }
      const v = r.view
      log.push({ t: r.t, kind: r.kind, name: r.name, ms: r.ms, view: `${v.symbol}/${v.timeframe}/${v.mode}` })
    }
  }

  const INITIAL_KEY = 'gexlens-perf-baseline-initial'

  function start() {
    if (document.visibilityState !== 'visible') throw new Error('záložka není viditelná')
    P.setPerfEnabled(true)
    P.resetPerf()
    startedAt = now()
    initial = loadInitial() ?? readControls()
    saveInitial(initial)
    harvestTimer = setInterval(harvest, HARVEST_MS)
    document.addEventListener('visibilitychange', onVisibility)
    resourceObserver = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) if (/\/api\/(replay|news\/stats)/.test(e.name)) resources.push(resourceRecord(e))
    })
    resourceObserver.observe({ type: 'resource', buffered: false })
    if (PerformanceObserver.supportedEntryTypes?.includes('long-animation-frame')) {
      loafObserver = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) loaf.push(loafRecord(e))
      })
      loafObserver.observe({ type: 'long-animation-frame', buffered: false })
    } else {
      console.warn('[pb] LoAF není k dispozici: rozpad dlouhých snímků chybí')
    }
    console.info('[pb] start', initial)
    return initial
  }

  function onVisibility() {
    visibility.push({ t: now(), state: document.visibilityState })
  }

  function loadInitial() {
    try {
      return JSON.parse(sessionStorage.getItem(INITIAL_KEY) ?? 'null')
    } catch (error) {
      console.warn('[pb] výchozí volby ze sessionStorage nejdou přečíst', error)
      return null
    }
  }

  function saveInitial(value) {
    try {
      if (value === null) sessionStorage.removeItem(INITIAL_KEY)
      else sessionStorage.setItem(INITIAL_KEY, JSON.stringify(value))
    } catch (error) {
      console.warn('[pb] výchozí volby do sessionStorage nejdou uložit', error)
    }
  }

  function stop() {
    harvest()
    if (harvestTimer !== null) clearInterval(harvestTimer)
    harvestTimer = null
    loafObserver?.disconnect()
    loafObserver = null
    document.removeEventListener('visibilitychange', onVisibility)
    resourceObserver?.disconnect()
    resourceObserver = null
    P.setPerfEnabled(false)
  }

  function resourceRecord(e) {
    return {
      url: e.name.replace(location.origin, ''),
      at: new Date(performance.timeOrigin + e.startTime).toISOString(),
      queue: round(e.requestStart - e.startTime),
      ttfb: round(e.responseStart - e.requestStart),
      download: round(e.responseEnd - e.responseStart),
      kB: Math.round((e.encodedBodySize || 0) / 1024),
      decodedKB: Math.round((e.decodedBodySize || 0) / 1024),
    }
  }

  function loafRecord(e) {
    const end = e.startTime + e.duration
    return {
      t: e.startTime,
      end,
      ms: e.duration,
      blocking: e.blockingDuration,
      // Úlohy a mikroúlohy před vykreslením | rAF + ResizeObserver | styl, layout, paint
      work: e.renderStart > 0 ? e.renderStart - e.startTime : e.duration,
      raf: e.renderStart > 0 ? e.styleAndLayoutStart - e.renderStart : 0,
      layout: e.styleAndLayoutStart > 0 ? end - e.styleAndLayoutStart : 0,
      scripts: e.scripts.map((s) => ({
        invoker: s.invoker,
        type: s.invokerType,
        ms: s.duration,
        forcedLayout: s.forcedStyleAndLayoutDuration,
        // Místo v minifikovaném bundlu: produkce nemá sourcemapy, funkce se dohledá v dist/
        source: `${(s.sourceURL || '').split('/').pop()}:${s.sourceCharPosition} ${s.sourceFunctionName || '?'}`,
      })),
    }
  }

  // --- Ovládací prvky (stejné jako uživatel) --------------------------------

  const q = (selector) => document.querySelector(selector)
  const overlay = () => q('canvas.heatmap-overlay')
  const modeSelect = () => q('select[aria-label="Heatmap mód"]')
  const scaleSelect = () => q('select[aria-label="Škála heatmapy"]')
  const styleSelect = () =>
    [...document.querySelectorAll('select')].find((el) => el.querySelector('option[value="blobs"]'))
  const contoursSelect = () =>
    [...document.querySelectorAll('select')].find((el) => el.querySelector('option[value="major"]'))
  const cleanButton = () => q('[data-testid="clean-view-toggle"]')
  const timeframeButtons = () => [...document.querySelectorAll('[aria-label="Timeframe"] button')]
  const activeSymbol = () => q('.watchlist-symbol.active')?.firstChild?.textContent?.trim() ?? null

  function readControls() {
    return {
      symbol: activeSymbol(),
      timeframe: timeframeButtons().find((b) => b.classList.contains('active') && /^(Intraday|Daily)$/.test(b.textContent))?.textContent ?? null,
      interval: timeframeButtons().find((b) => b.classList.contains('active') && /^\d+[mh]$/.test(b.textContent))?.textContent ?? null,
      clean: cleanButton()?.getAttribute('aria-pressed') === 'true',
      mode: modeSelect()?.value ?? null,
      scale: scaleSelect()?.value ?? null,
      style: styleSelect()?.value ?? null,
      contours: contoursSelect()?.value ?? null,
      cursorTool: [...document.querySelectorAll('button.chip')].some((b) => b.textContent === 'Kurzor' && b.classList.contains('active')),
    }
  }

  function setSelect(el, value) {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set
    setter.call(el, value)
    el.dispatchEvent(new Event('change', { bubbles: true }))
  }

  async function selectInterval(interval) {
    const button = timeframeButtons().find((b) => b.textContent === interval)
    if (!button) throw new Error(`interval ${interval} nenalezen`)
    if (!button.classList.contains('active')) button.click()
    await waitIdle()
  }

  async function setClean(on) {
    const button = cleanButton()
    if (!button) throw new Error('přepínač Čistý pohled nenalezen')
    if ((button.getAttribute('aria-pressed') === 'true') !== on) button.click()
    await waitIdle()
  }

  async function setSymbol(symbol) {
    const button = q(`button[aria-label="Přepnout na ${symbol}"]`)
    if (!button) throw new Error(`symbol ${symbol} není ve watchlistu`)
    if (!button.classList.contains('active')) button.click()
  }

  // Klid: dva snímky po sobě kratší než 25 ms (žádná rozběhnutá přestavba)
  async function waitIdle(timeoutMs = 5_000) {
    const deadline = now() + timeoutMs
    let calm = 0
    let last = now()
    while (now() < deadline) {
      await frame()
      const t = now()
      calm = t - last < 25 ? calm + 1 : 0
      last = t
      if (calm >= 2) return true
    }
    return false
  }

  async function waitCounter(name, before, timeoutMs = 10_000) {
    const deadline = now() + timeoutMs
    while (now() < deadline) {
      if ((P.perfCounters()[name] ?? 0) > before) return true
      await sleep(20)
    }
    return false
  }

  // Segmenty mimo minutový update: v RTH 12. 10. přicházel ve 33.–36. sekundě minuty
  // (2–4 pushe po sobě), ne na hranici minuty. Okno 40.–58. s nechá segmentu ≥ 35 s.
  async function quietWindow() {
    for (;;) {
      const second = new Date().getSeconds()
      if (second >= pb.quiet[0] && second <= pb.quiet[1]) return
      await sleep(250)
    }
  }

  async function segment(name, meta, body) {
    await waitVisible()
    await quietWindow()
    await waitIdle()
    const s = { name, ...meta, t0: now(), t1: null, samples: [], counters0: P.perfCounters() }
    segments.push(s)
    try {
      await body(s)
    } finally {
      await frames(3)
      s.t1 = now()
      s.counters1 = P.perfCounters()
      harvest()
    }
    return s
  }

  // Brána před každým segmentem: zakryté okno nezastaví běh, segment počká
  async function waitVisible() {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await gate()
      } catch (error) {
        if (attempt === 0) console.warn(`[pb] čekám na viditelné okno: ${error.message}`)
        await sleep(2_000)
      }
    }
  }

  // --- Gesta -----------------------------------------------------------------

  function pointer(type, x, y, buttons) {
    overlay().dispatchEvent(
      new PointerEvent(type, {
        bubbles: true,
        cancelable: true,
        composed: true,
        pointerId: 1,
        pointerType: 'mouse',
        isPrimary: true,
        clientX: x,
        clientY: y,
        button: type === 'pointermove' ? -1 : 0,
        buttons,
      }),
    )
  }

  // Bod plochy bez anotace, range a osy: po najetí zůstane výchozí kurzor
  async function freePoint() {
    const canvas = overlay()
    if (!canvas) throw new Error('canvas.heatmap-overlay nenalezen')
    if (!readControls().cursorTool) throw new Error('aktivní není nástroj Kurzor: pan by kreslil anotaci')
    const box = canvas.getBoundingClientRect()
    for (const fy of [0.45, 0.35, 0.55, 0.65]) {
      for (const fx of [0.45, 0.35, 0.55, 0.3, 0.6]) {
        const x = box.left + box.width * fx
        const y = box.top + box.height * fy
        pointer('pointermove', x, y, 0)
        await frames(2)
        if (!canvas.style.cursor) return { x, y }
      }
    }
    throw new Error('žádný volný bod plochy: anotace nebo range všude, pan se neměří')
  }

  async function pan(s, { reps = 3, dx = 300, steps = 60 } = {}) {
    for (let rep = 0; rep < reps; rep += 1) {
      const p = await freePoint()
      pointer('pointerdown', p.x, p.y, 1)
      for (const dir of [-1, 1]) {
        for (let i = 1; i <= steps; i += 1) {
          await frame()
          const offset = dir < 0 ? (dx * i) / steps : dx - (dx * i) / steps
          pointer('pointermove', p.x - offset, p.y, 1)
          s.samples.push(now())
        }
      }
      pointer('pointerup', p.x, p.y, 0)
      await waitIdle()
    }
    await resetView()
  }

  async function wheel(s, { reps = 3, steps = 12 } = {}) {
    for (let rep = 0; rep < reps; rep += 1) {
      const p = await freePoint()
      for (const deltaY of [-100, 100]) {
        for (let i = 0; i < steps; i += 1) {
          await frame()
          overlay().dispatchEvent(
            new WheelEvent('wheel', { bubbles: true, cancelable: true, clientX: p.x, clientY: p.y, deltaY, deltaMode: 0 }),
          )
          s.samples.push(now())
        }
      }
      await waitIdle()
    }
    await resetView()
  }

  // Dvojklik = reset pohledu (`resetView`, obnoví i auto-follow #501)
  async function resetView() {
    const box = overlay().getBoundingClientRect()
    overlay().dispatchEvent(
      new MouseEvent('dblclick', { bubbles: true, cancelable: true, clientX: box.left + box.width / 2, clientY: box.top + box.height / 2 }),
    )
    await waitIdle()
  }

  // Přepnutí selectu: span měřiče (je-li) a „změna → po vykreslení“ sběrače
  async function cycleSelect(s, el, span, rounds) {
    const original = el.value
    const values = [...el.options].filter((o) => !o.disabled).map((o) => o.value)
    for (let pass = 0; pass < rounds; pass += 1) {
      for (const value of [...values.filter((v) => v !== original), original]) {
        const before = span ? (P.perfCounters()[span] ?? 0) : 0
        const t0 = now()
        setSelect(el, value)
        await afterPaint()
        const paint = now() - t0
        const settled = span ? await waitCounter(span, before) : true
        s.samples.push({ value, paintMs: round(paint), settled })
        await waitIdle()
      }
    }
  }

  async function symbolSwitch(s, other) {
    const home = activeSymbol()
    for (const target of [other, home, other, home]) {
      const before = P.perfCounters().symbolSwitch ?? 0
      const t0 = now()
      await setSymbol(target)
      const settled = await waitCounter('symbolSwitch', before, SYMBOL_TIMEOUT_MS)
      s.samples.push({ target, ms: round(now() - t0), settled })
      await waitIdle()
      await sleep(1_000)
    }
  }

  // --- Scénáře ---------------------------------------------------------------

  async function gate() {
    if (document.visibilityState !== 'visible') throw new Error('záložka není viditelná')
    const deltas = []
    let last = await frame()
    const until = now() + 1_000
    while (now() < until) {
      const t = await frame()
      deltas.push(t - last)
      last = t
    }
    const median = stats(deltas).p50
    const result = { medianFrameMs: median, hz: round(1000 / median), ok: median <= 17.5 }
    if (!result.ok) throw new Error(`klidový snímek ${median} ms: okno je zakryté nebo škrcené`)
    return result
  }

  // Měřený symbol: přepnout a počkat, až jsou jeho data vykreslená
  async function ensureSymbol(symbol) {
    if (activeSymbol() === symbol) return
    const before = P.perfCounters().symbolSwitch ?? 0
    await setSymbol(symbol)
    if (!(await waitCounter('symbolSwitch', before, SYMBOL_TIMEOUT_MS))) {
      throw new Error(`data ${symbol} se do ${SYMBOL_TIMEOUT_MS / 1000} s nevykreslila`)
    }
    await waitIdle()
    await sleep(2_000)
  }

  async function setContours(value) {
    const el = contoursSelect()
    if (el.value !== value) setSelect(el, value)
    await waitIdle()
  }

  // Mode a Scale se měří s každým nastavením kontur v `contours` (cíl je „vč. kontur“)
  async function run(interval, clean, { symbol = 'ES', contours = ['off', 'major'], only = null } = {}) {
    const g = await gate()
    await ensureSymbol(symbol)
    await selectInterval(interval)
    await setClean(clean)
    const meta = { tf: interval, clean, symbol, gateHz: g.hz, controls: readControls() }
    const want = (name) => only === null || only.includes(name)
    const done = []
    if (want('pan')) done.push(await segment('pan', meta, (s) => pan(s)))
    if (want('wheel')) done.push(await segment('wheel', meta, (s) => wheel(s)))
    for (const value of contours) {
      if (!want('mode') && !want('scale')) break
      await setContours(value)
      const withContours = { ...meta, contours: value }
      if (want('mode')) done.push(await segment('mode', withContours, (s) => cycleSelect(s, modeSelect(), 'modeScale', 1)))
      if (want('scale')) done.push(await segment('scale', withContours, (s) => cycleSelect(s, scaleSelect(), 'modeScale', 2)))
    }
    if (initial?.contours) await setContours(initial.contours)
    if (want('style')) done.push(await segment('style', meta, (s) => cycleSelect(s, styleSelect(), null, 3)))
    if (want('symbol')) {
      const other = symbol === 'ES' ? 'NQ' : 'ES'
      done.push(await segment('symbol', meta, (s) => symbolSwitch(s, other)))
    }
    harvest()
    return done.map((s) => summarizeSegment(s))
  }

  async function restore() {
    if (!initial) return null
    if (initial.symbol && activeSymbol() !== initial.symbol) {
      await setSymbol(initial.symbol)
      await sleep(3_000)
    }
    if (initial.interval) await selectInterval(initial.interval)
    await setClean(initial.clean)
    if (initial.mode && modeSelect().value !== initial.mode) setSelect(modeSelect(), initial.mode)
    if (initial.scale && scaleSelect().value !== initial.scale) setSelect(scaleSelect(), initial.scale)
    if (initial.style && styleSelect().value !== initial.style) setSelect(styleSelect(), initial.style)
    if (initial.contours) await setContours(initial.contours)
    await waitIdle()
    await resetView()
    const after = readControls()
    const same = JSON.stringify(after) === JSON.stringify(initial)
    if (same) saveInitial(null)
    else console.warn('[pb] volby se neshodují s výchozími', { initial, after })
    return { same, initial, after }
  }

  // --- Souhrny ---------------------------------------------------------------

  function within(records, t0, t1) {
    return records.filter((r) => r.t >= t0 && r.t <= t1)
  }

  function opsStats(records, kind) {
    const byName = {}
    for (const r of records) {
      if (r.kind !== kind) continue
      ;(byName[r.name] ??= []).push(r.ms)
    }
    return Object.fromEntries(Object.entries(byName).map(([name, values]) => [name, stats(values)]))
  }

  function loafStats(entries) {
    const byInvoker = {}
    for (const e of entries) {
      for (const script of e.scripts) {
        const key = `${script.type}:${script.invoker}`
        byInvoker[key] = (byInvoker[key] ?? 0) + script.ms
      }
    }
    const top = Object.entries(byInvoker)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([invoker, ms]) => ({ invoker, ms: round(ms) }))
    const bySource = {}
    for (const e of entries) {
      for (const script of e.scripts) bySource[script.source] = (bySource[script.source] ?? 0) + script.ms
    }
    const topSources = Object.entries(bySource)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([source, ms]) => ({ source, ms: round(ms) }))
    const sum = (key) => round(entries.reduce((acc, e) => acc + e[key], 0))
    return {
      n: entries.length,
      ms: stats(entries.map((e) => e.ms)),
      sumWork: sum('work'),
      sumRaf: sum('raf'),
      sumLayout: sum('layout'),
      sumScripts: round(entries.reduce((acc, e) => acc + e.scripts.reduce((a, s) => a + s.ms, 0), 0)),
      top,
      topSources,
    }
  }

  function delta(c0, c1) {
    const out = {}
    for (const key of new Set([...Object.keys(c0), ...Object.keys(c1)])) {
      const d = (c1[key] ?? 0) - (c0[key] ?? 0)
      if (d) out[key] = d
    }
    return out
  }

  function summarizeSegment(s) {
    const records = within(log, s.t0, s.t1)
    const frameMs = records.filter((r) => r.kind === 'frame').map((r) => r.ms)
    const longtasks = records.filter((r) => r.kind === 'longtask').map((r) => r.ms)
    const counters = delta(s.counters0, s.counters1 ?? P.perfCounters())
    const samples = s.samples.filter((x) => typeof x === 'object')
    // Pokrytí snímky: skrytá záložka snímky nemá (měřič je nezapočítá), čas segmentu ano
    const coverage = round((100 * frameMs.reduce((a, b) => a + b, 0)) / (s.t1 - s.t0))
    const hidden = within(visibility, s.t0, s.t1).filter((v) => v.state === 'hidden').length
    return {
      name: s.name,
      symbol: s.symbol,
      tf: s.tf,
      clean: s.clean,
      contours: s.contours ?? null,
      at: new Date(performance.timeOrigin + s.t0).toISOString(),
      seconds: round((s.t1 - s.t0) / 1000),
      gateHz: s.gateHz,
      frames: { ...stats(frameMs), fps: frameMs.length ? round((frameMs.length * 1000) / frameMs.reduce((a, b) => a + b, 0)) : null },
      events: s.samples.filter((x) => typeof x === 'number').length,
      longtasks: stats(longtasks),
      ops: opsStats(records, 'op'),
      spans: opsStats(records, 'span'),
      paint: samples.length ? stats(samples.map((x) => x.paintMs ?? x.ms)) : null,
      unsettled: samples.filter((x) => x.settled === false).length,
      counters,
      // Minutový update nebo načtení dne uvnitř segmentu = snímky zkreslené
      contaminated: Boolean(counters.minuteUpdate || counters.dayLoad),
      coverage,
      hidden,
      // Segment ze zakrytého okna neplatí: gesto i spany čekaly na rAF
      occluded: hidden > 0 || coverage < 90,
      loaf: loafStats(within(loaf, s.t0, s.t1)),
    }
  }

  // Pasivní okno: vše mimo aktivní segmenty (minutový update, tick, refetch)
  function passive() {
    const outside = (r) => !segments.some((s) => r.t >= s.t0 && r.t <= (s.t1 ?? Infinity))
    const records = log.filter(outside)
    const minutes = (now() - startedAt) / 60_000
    const longtasksByMinute = {}
    for (const r of records) {
      if (r.kind !== 'longtask') continue
      const minute = new Date(performance.timeOrigin + r.t).toISOString().slice(0, 16)
      const bucket = (longtasksByMinute[minute] ??= { n: 0, max: 0, sum: 0 })
      bucket.n += 1
      bucket.max = Math.max(bucket.max, r.ms)
      bucket.sum += r.ms
    }
    const perMinute = Object.values(longtasksByMinute)
    const wsLoaf = loaf.filter(outside).filter((e) => e.scripts.some((s) => /WebSocket/.test(s.invoker)))
    const opCount = (name) => records.filter((r) => r.kind === 'op' && r.name === name).length
    return {
      minutes: round(minutes),
      // Podíl času s vykreslováním; minutový update ze skryté záložky čeká na rAF
      coverage: round((100 * frameMsTotal) / (now() - startedAt)),
      hidden: visibility.filter((v) => v.state === 'hidden').map((v) => new Date(performance.timeOrigin + v.t).toISOString()),
      histogram: Object.fromEntries(FRAME_BUCKETS.map((limit, i) => [`≤${limit}`, histogram[i]])),
      slowFrames: stats(records.filter((r) => r.kind === 'frame').map((r) => r.ms)),
      longtasks: stats(records.filter((r) => r.kind === 'longtask').map((r) => r.ms)),
      longtasksPerMinute: {
        minutesWithLongtask: perMinute.length,
        n: stats(perMinute.map((m) => m.n)),
        max: stats(perMinute.map((m) => m.max)),
        sum: stats(perMinute.map((m) => m.sum)),
      },
      ops: opsStats(records, 'op'),
      spans: opsStats(records, 'span'),
      perMinute: {
        drawStatic: round(opCount('drawStatic') / minutes),
        drawDynamic: round(opCount('drawDynamic') / minutes),
        minuteFlush: round(opCount('minuteFlush') / minutes),
      },
      websocketLoaf: loafStats(wsLoaf),
      loaf: loafStats(loaf.filter(outside)),
      // Sekunda minuty, kdy končí minutový update (pro nastavení pb.quiet)
      minuteUpdateSecond: records
        .filter((r) => r.kind === 'span' && r.name === 'minuteUpdate')
        .map((r) => new Date(performance.timeOrigin + r.t).getSeconds()),
    }
  }

  function report() {
    harvest()
    return {
      build: P.perfExport(undefined, { records: false }).build,
      startedAt: startedAt === null ? null : new Date(performance.timeOrigin + startedAt).toISOString(),
      at: new Date().toISOString(),
      userAgent: navigator.userAgent,
      viewport: { w: innerWidth, h: innerHeight, dpr: devicePixelRatio },
      canvas: (() => {
        const box = overlay()?.getBoundingClientRect()
        return box ? { w: Math.round(box.width), h: Math.round(box.height) } : null
      })(),
      initial,
      logSize: log.length,
      segments: segments.filter((s) => s.t1 !== null).map(summarizeSegment),
      passive: startedAt === null ? null : passive(),
      resources,
    }
  }

  // Studené načtení dne (po reloadu): navigace, /api/replay a bufferované LoAF
  function coldLoad() {
    const nav = performance.getEntriesByType('navigation')[0]
    const replay = performance
      .getEntriesByType('resource')
      .filter((e) => /\/api\/replay/.test(e.name))
      .map((e) => ({
        url: e.name.replace(location.origin, ''),
        start: round(e.startTime),
        ttfb: round(e.responseStart - e.startTime),
        download: round(e.responseEnd - e.responseStart),
        end: round(e.responseEnd),
        kB: Math.round((e.encodedBodySize || 0) / 1024),
        decodedKB: Math.round((e.decodedBodySize || 0) / 1024),
      }))
    return new Promise((resolve) => {
      const entries = []
      const observer = new PerformanceObserver((list) => entries.push(...list.getEntries().map(loafRecord)))
      observer.observe({ type: 'long-animation-frame', buffered: true })
      setTimeout(() => {
        observer.disconnect()
        resolve({
          at: new Date().toISOString(),
          domContentLoaded: round(nav?.domContentLoadedEventEnd),
          load: round(nav?.loadEventEnd),
          replay,
          longFrames: entries
            .filter((e) => e.t < 30_000)
            .map((e) => ({ t: round(e.t), ms: round(e.ms), blocking: round(e.blocking), top: e.scripts.slice(0, 3).map((s) => `${s.type}:${s.invoker} ${round(s.ms)}`) })),
        })
      }, 500)
    })
  }

  const pb = { start, stop, gate, run, restore, report, coldLoad, harvest, readControls, segments, log, loaf, visibility, resources, quiet: [40, 58] }
  window.pb = pb
  console.info('[pb] sběrač #1412 připraven: pb.start(), await pb.gate(), await pb.run("1m", false)')
})()
