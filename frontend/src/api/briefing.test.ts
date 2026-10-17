/** Testy skládacích helperů briefingu (#674) — čisté funkce nad bary/levels. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  CANDLE_LIMITS,
  barsRange,
  briefingToPlanText,
  fetchStoredVerdict,
  gammaRegimeLabel,
  latestLevels,
  previousStoredDay,
  usOpenMs,
  verdictCard,
} from './briefing'
import type { BarRow, LevelsRow, StoredVerdict, StoredVerdictResult } from './briefing'

function bar(ts: string, high: number, low: number, close: number): BarRow {
  return { ts_min: ts, open: close, high, low, close, volume: 100 }
}

const BARS: BarRow[] = [
  bar('2026-08-13T04:00:00Z', 6410, 6390, 6400),
  bar('2026-08-13T10:00:00Z', 6425, 6405, 6420),
  bar('2026-08-13T14:00:00Z', 6460, 6415, 6455),
]

describe('barsRange', () => {
  it('extrémy a poslední close přes celou seanci', () => {
    expect(barsRange(BARS)).toEqual({
      high: 6460,
      low: 6390,
      last: 6455,
      lastTs: '2026-08-13T14:00:00Z',
    })
  })

  it('untilMs ořízne na overnight (před US openem)', () => {
    const open = usOpenMs('2026-08-13') // 13:30 UTC v srpnu (EDT)
    const summary = barsRange(BARS, open)
    expect(summary).toEqual({ high: 6425, low: 6390, last: 6420, lastTs: '2026-08-13T10:00:00Z' })
  })

  it('prázdné bary → null', () => {
    expect(barsRange([])).toBeNull()
  })
})

describe('usOpenMs', () => {
  it('9:30 New York = 13:30 UTC v létě (EDT)', () => {
    expect(new Date(usOpenMs('2026-08-13')).toISOString()).toBe('2026-08-13T13:30:00.000Z')
  })

  it('9:30 New York = 14:30 UTC v zimě (EST)', () => {
    expect(new Date(usOpenMs('2026-01-15')).toISOString()).toBe('2026-01-15T14:30:00.000Z')
  })
})

const at = (iso: string) => new Date(iso).getTime()

/** Uložený řádek enginu: čtvrtek 24. 9. 2026, US open 13:30 UTC = 15:30 SELČ. */
const ROW: StoredVerdict = {
  session_date: '2026-09-24',
  verdict: 'long',
  score: 5,
  votes: [
    { name: 'trend_higher', vote: 2, reason: 'vyšší TF (týden, den) rostoucí' },
    { name: 'gamma', vote: 0, reason: 'pozitivní gamma, ale po útesu 83 % gammy — tlumení tenké, nehlasuje' }, // prettier-ignore
  ],
  updated_at: '2026-09-24T13:25:00+00:00',
}

function card(
  dateIso: string,
  stored: StoredVerdictResult | null,
  nowIso: string,
  extra: { symbol?: string; pipelineSymbols?: string } = {},
) {
  return verdictCard({
    dateIso,
    symbol: extra.symbol ?? 'ES',
    pipelineSymbols: 'pipelineSymbols' in extra ? extra.pipelineSymbols : 'ES,NQ',
    stored,
    nowMs: at(nowIso),
  })
}

describe('verdictCard (#1595): stav karty z uloženého verdiktu enginu', () => {
  it('neobchodní den: víkend i svátek NYSE — verdikt se nevydává, ani s řádkem', () => {
    const saturday = card('2026-09-26', { ok: true, row: ROW }, '2026-09-26T10:00:00Z')
    expect(saturday.kind).toBe('no_session')
    expect(saturday.message).toBe('Verdikt se nevydává — sobota 26. 9. není obchodní seance.')
    const thanksgiving = card('2026-11-26', { ok: false, error: 'x' }, '2026-11-26T10:00:00Z')
    expect(thanksgiving.message).toBe(
      'Verdikt se nevydává — čtvrtek 26. 11. (svátek NYSE) není obchodní seance.',
    )
  })

  it('symbol bez pipeline enginu; bez status.symbols se nic netvrdí', () => {
    const adhoc = card('2026-09-24', { ok: true, row: null }, '2026-09-24T12:00:00Z', { symbol: 'AAPL' }) // prettier-ignore
    expect(adhoc).toEqual({ kind: 'no_pipeline', message: 'Engine pro tento symbol verdikt nepočítá.', row: null }) // prettier-ignore
    // Přesná shoda tickeru: pinovaný ESZ6 je jiná pipeline než ES
    expect(card('2026-09-24', null, '2026-09-24T12:00:00Z', { symbol: 'ESZ6' }).kind).toBe('no_pipeline') // prettier-ignore
    const unknown = card('2026-09-24', { ok: true, row: null }, '2026-09-24T12:00:00Z', { symbol: 'AAPL', pipelineSymbols: undefined }) // prettier-ignore
    expect(unknown.kind).toBe('pending')
    // Uložený řádek má přednost: pipeline zmizela až po zápisu (symbol odebraný z watchlistu)
    const removed = card('2026-09-24', { ok: true, row: { ...ROW, updated_at: '2026-09-24T13:29:05Z' } }, '2026-09-24T14:00:00Z', { symbol: 'AAPL' }) // prettier-ignore
    expect(removed.kind).toBe('frozen')
    // Prázdné symbols = engine bez pipeline (IBKR nepřipojené) — výpadek, ne trvalý stav
    expect(card('2026-09-24', { ok: true, row: null }, '2026-09-24T12:00:00Z', { pipelineSymbols: '' }).kind).toBe('pending') // prettier-ignore
  })

  it('neúplný řádek (#1595 e1) nese chybějící vstupy — předběžný, zmrazený i plán', () => {
    const row = { ...ROW, updated_at: '2026-09-24T13:29:05Z', missing: ['gamma útes: HTTP 500', 'levels: engine je ještě nespočítal'] } // prettier-ignore
    const frozen = card('2026-09-24', { ok: true, row }, '2026-09-24T13:30:00Z')
    expect(frozen.message).toBe('zmrazený, neúplný (chybí: gamma útes: HTTP 500; levels: engine je ještě nespočítal) — poslední zápis 15:29') // prettier-ignore
    const early = card('2026-09-24', { ok: true, row }, '2026-09-24T13:29:30Z')
    expect(early.message).toMatch(/^předběžný, neúplný \(chybí: gamma útes: HTTP 500; /)
    // Úplný ([]) i řádek z Briefingu před #1595 (null) bez dovětku
    expect(card('2026-09-24', { ok: true, row: { ...row, missing: [] } }, '2026-09-24T13:30:00Z').message).toBe('zmrazený — poslední zápis 15:29') // prettier-ignore
    expect(card('2026-09-24', { ok: true, row: { ...row, missing: null } }, '2026-09-24T13:30:00Z').message).toBe('zmrazený — poslední zápis 15:29') // prettier-ignore
  })

  it('chyba čtení je výslovná, ne „bez verdiktu"; bez odpovědi se načítá', () => {
    const error = card('2026-09-24', { ok: false, error: 'Verdikt se nepodařilo načíst (HTTP 503)' }, '2026-09-24T12:00:00Z') // prettier-ignore
    expect(error).toEqual({ kind: 'error', message: 'Verdikt se nepodařilo načíst (HTTP 503)', row: null }) // prettier-ignore
    expect(card('2026-09-24', null, '2026-09-24T12:00:00Z').kind).toBe('loading')
  })

  it('řádek před openem = předběžný s okny v pražském čase a stářím zápisu', () => {
    const result = card('2026-09-24', { ok: true, row: ROW }, '2026-09-24T13:28:00Z')
    expect(result.kind).toBe('preliminary')
    expect(result.row).toBe(ROW)
    expect(result.message).toBe(
      'předběžný: engine ho přepočítává každých 5 min, od 15:15 každou minutu; platí poslední stav před openem 15:30. Uloženo 15:25, před 3 min.',
    )
    // Engine stojí → stáří roste a je vidět (ne zmrzlé číslo bez kontextu)
    const stale = card('2026-09-24', { ok: true, row: { ...ROW, updated_at: '2026-09-24T10:55:00Z' } }, '2026-09-24T13:00:00Z') // prettier-ignore
    expect(stale.message).toContain('Uloženo 12:55, před 2 h 5 min.')
  })

  it('USA v letním čase, Evropa v zimním: open 14:30 pražského, okno od 14:15', () => {
    const row = { ...ROW, session_date: '2026-10-27', updated_at: '2026-10-27T12:50:00Z' }
    const result = card('2026-10-27', { ok: true, row }, '2026-10-27T13:00:00Z')
    expect(result.message).toContain('od 14:15 každou minutu; platí poslední stav před openem 14:30.') // prettier-ignore
    expect(result.message).toContain('Uloženo 13:50, před 10 min.')
  })

  it('řádek od openu = zmrazený s posledním zápisem', () => {
    const row = { ...ROW, updated_at: '2026-09-24T13:29:05Z' }
    const result = card('2026-09-24', { ok: true, row }, '2026-09-24T13:30:00Z')
    expect(result).toEqual({ kind: 'frozen', message: 'zmrazený — poslední zápis 15:29', row })
  })

  it('bez řádku: před openem vznikne, po openu viditelná mezera bez náhradního výpočtu', () => {
    const pending = card('2026-09-24', { ok: true, row: null }, '2026-09-24T13:29:59Z')
    expect(pending).toEqual({
      kind: 'pending',
      message: 'Verdikt zatím nevznikl — engine ho ukládá od začátku seance každých 5 min.',
      row: null,
    })
    const missing = card('2026-09-24', { ok: true, row: null }, '2026-09-24T13:30:00Z')
    expect(missing).toEqual({ kind: 'missing', message: 'Verdikt pro tuto seanci nevznikl.', row: null }) // prettier-ignore
  })
})

describe('fetchStoredVerdict (#1595)', () => {
  const fetchMock = vi.fn()
  afterEach(() => {
    vi.unstubAllGlobals()
    fetchMock.mockReset()
  })

  function respond(body: unknown, status = 200) {
    vi.stubGlobal('fetch', fetchMock)
    fetchMock.mockResolvedValue({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) }) // prettier-ignore
  }

  it('řádek prohlížené seance; jiné seance se ignorují; days od dnešního UTC data', async () => {
    respond({ verdicts: [{ ...ROW, session_date: '2026-09-25' }, ROW] })
    const result = await fetchStoredVerdict('ES', '2026-09-24', at('2026-09-24T13:28:00Z'))
    expect(result).toEqual({ ok: true, row: ROW })
    expect(String(fetchMock.mock.calls[0][0])).toContain('/briefing/verdicts?symbol=ES&days=1')
  })

  it('days pokryje starší datum i zítřejší seanci po 17:00 CT', async () => {
    respond({ verdicts: [] })
    await fetchStoredVerdict('NQ', '2026-09-20', at('2026-09-24T12:00:00Z'))
    expect(String(fetchMock.mock.calls[0][0])).toContain('symbol=NQ&days=5')
    await fetchStoredVerdict('NQ', '2026-09-25', at('2026-09-24T23:00:00Z'))
    expect(String(fetchMock.mock.calls[1][0])).toContain('days=1')
    await fetchStoredVerdict('NQ', '2024-01-02', at('2026-09-24T12:00:00Z'))
    expect(String(fetchMock.mock.calls[2][0])).toContain('days=400') // strop endpointu
  })

  it('bez řádku seance = ok bez řádku', async () => {
    respond({ verdicts: [] })
    expect(await fetchStoredVerdict('ES', '2026-09-24', at('2026-09-24T12:00:00Z'))).toEqual({ ok: true, row: null }) // prettier-ignore
  })

  it('HTTP chyba, síť i neplatná odpověď jsou výslovná chyba, ne „bez verdiktu"', async () => {
    const now = at('2026-09-24T12:00:00Z')
    respond({ detail: 'db down' }, 503)
    expect(await fetchStoredVerdict('ES', '2026-09-24', now)).toEqual({ ok: false, error: 'Verdikt se nepodařilo načíst (HTTP 503)' }) // prettier-ignore
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    expect(await fetchStoredVerdict('ES', '2026-09-24', now)).toEqual({ ok: false, error: 'Verdikt se nepodařilo načíst (síť: Failed to fetch)' }) // prettier-ignore
    respond({})
    expect(await fetchStoredVerdict('ES', '2026-09-24', now)).toEqual({ ok: false, error: 'Verdikt se nepodařilo načíst (odpověď bez seznamu verdiktů)' }) // prettier-ignore
    respond({ verdicts: [{ ...ROW, verdict: 'maybe' }] })
    expect(await fetchStoredVerdict('ES', '2026-09-24', now)).toEqual({ ok: false, error: 'Verdikt se nepodařilo načíst (neplatný řádek seance 2026-09-24)' }) // prettier-ignore
  })
})

describe('CANDLE_LIMITS', () => {
  it('táž tabulka jako engine scenario_auto.CANDLE_LIMITS (#1595)', () => {
    expect(CANDLE_LIMITS).toEqual({ W: 80, D: 120, '240': 120, '60': 120, '15': 120 })
  })
})

describe('previousStoredDay', () => {
  it('poslední uložený den před datem', () => {
    expect(previousStoredDay(['2026-08-10', '2026-08-11', '2026-08-13'], '2026-08-13')).toBe(
      '2026-08-11',
    )
  })

  it('bez staršího dne → null', () => {
    expect(previousStoredDay(['2026-08-13'], '2026-08-13')).toBeNull()
  })
})

const LEVELS: LevelsRow = {
  ts_min: '2026-08-13T14:00:00Z',
  flip: 6430,
  call_wall: 6500,
  put_wall: 6400,
  centroid: 6445,
  total_gex: 1200,
}

describe('gammaRegimeLabel', () => {
  it('pozitivní gamma nad flipem', () => {
    expect(gammaRegimeLabel(LEVELS, 6455)).toBe('pozitivní gamma (pohyb se tlumí), cena nad flipem')
  })

  it('negativní gamma pod flipem', () => {
    expect(gammaRegimeLabel({ ...LEVELS, total_gex: -50 }, 6410)).toBe(
      'negativní gamma (pohyb se zesiluje), cena pod flipem',
    )
  })

  it('bez levels → bez dat; bez flipu jen znaménko', () => {
    expect(gammaRegimeLabel(null, 6400)).toBe('bez dat')
    expect(gammaRegimeLabel({ ...LEVELS, flip: null }, 6400)).toBe(
      'pozitivní gamma (pohyb se tlumí)',
    )
  })
})

describe('briefingToPlanText', () => {
  it('kostra plánu nese režim, úrovně, včerejšek i overnight', () => {
    const text = briefingToPlanText({
      symbol: 'ES',
      regime: 'pozitivní gamma (pohyb se tlumí), cena nad flipem',
      levels: LEVELS,
      overnight: { high: 6425, low: 6390, last: 6420, lastTs: '' },
      prevDay: { high: 6440, low: 6380, last: 6435, lastTs: '' },
      cliff: { session_date: '2026-08-13', cliff_share: 0.42, is_opex: false },
    })
    expect(text).toContain('Plán dne ES:')
    expect(text).toContain('flip 6430, call wall 6500, put wall 6400')
    expect(text).toContain('settle 6435, rozsah 6380–6440')
    expect(text).toContain('Overnight: 6390–6425')
    expect(text).toContain('odpadá ~42 % gammy')
    expect(text).toContain('Teze dne:')
  })

  it('volatility box (#873): hodnoty když jsou, checkbox rituálu vždy', () => {
    const withData = briefingToPlanText({
      symbol: 'ES',
      regime: 'pozitivní gamma',
      levels: null,
      overnight: null,
      prevDay: null,
      cliff: null,
      vol: { session_date: '2026-08-25', session_range: 42, percentile: 0.54, bucket: 'normal', sample: 252 }, // prettier-ignore
      em: { em: 38.5, anchor: 7600, preOpen: true },
    })
    expect(withData).toContain('Volatilita: normální (p54, 252 seancí) · EM ±38.5 b (0.51 %, pre-open odhad)') // prettier-ignore
    expect(withData).toContain('- [ ] riziko přizpůsobeno režimu (stop/velikost)')

    const withoutData = briefingToPlanText({
      symbol: 'ES',
      regime: 'bez dat',
      levels: null,
      overnight: null,
      prevDay: null,
      cliff: null,
    })
    // Bez dat se říká proč (zásada ADR-0028) — a checkbox zůstává
    expect(withoutData).toContain('Volatilita: bez dat (málo vzorků nebo chybí straddle)')
    expect(withoutData).toContain('- [ ] riziko přizpůsobeno režimu')
  })

  it('verdikt dne (#1595) jen z uloženého řádku enginu, s platností; bez řádku se vynechá', () => {
    const base = { symbol: 'ES', regime: 'bez dat', levels: null, overnight: null, prevDay: null, cliff: null } // prettier-ignore
    const preliminary = briefingToPlanText({ ...base, verdict: card('2026-09-24', { ok: true, row: ROW }, '2026-09-24T13:28:00Z') }) // prettier-ignore
    expect(preliminary).toContain('- Verdikt dne: Spíše long den (skóre +5) — nákupy u podpor ve směru trendu, short jen jako fade u zdí. (předběžný, uloženo 15:25)') // prettier-ignore
    const frozen = briefingToPlanText({ ...base, verdict: card('2026-09-24', { ok: true, row: { ...ROW, updated_at: '2026-09-24T13:29:00Z' } }, '2026-09-24T14:00:00Z') }) // prettier-ignore
    expect(frozen).toContain('(zmrazený, poslední zápis 15:29)')
    const pending = briefingToPlanText({ ...base, verdict: card('2026-09-24', { ok: true, row: null }, '2026-09-24T12:00:00Z') }) // prettier-ignore
    expect(pending).not.toContain('Verdikt dne')
  })

  it('latestLevels vrací poslední řádek', () => {
    expect(latestLevels([{ ...LEVELS, flip: 1 }, LEVELS])).toBe(LEVELS)
    expect(latestLevels([])).toBeNull()
  })
})
