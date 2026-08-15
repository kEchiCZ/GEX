/** Snímek GEX kontextu k okamžiku záznamu (#711). */
import { describe, expect, it } from 'vitest'
import type { BarRow, LevelsRow } from '../api/briefing'
import type { TendencyRow } from '../api/tendency'
import { CONTEXT_VERSION, composeContext, nearestLevel } from './context'

function bar(minute: number, close: number, high = close + 2, low = close - 2): BarRow {
  return {
    ts_min: `2026-08-14T14:${String(minute).padStart(2, '0')}:00+00:00`,
    open: close,
    high,
    low,
    close,
    volume: 100,
  }
}

function levels(minute: number, flip: number | null, totalGex: number): LevelsRow {
  return {
    ts_min: `2026-08-14T14:${String(minute).padStart(2, '0')}:00+00:00`,
    flip,
    call_wall: 6850,
    put_wall: 6750,
    centroid: 6800,
    total_gex: totalGex,
  }
}

const BASE = {
  symbol: 'ES',
  expiry: '20260814',
  tsRef: '2026-08-14T14:30:00+00:00',
  bars: [bar(28, 6800), bar(30, 6810), bar(32, 6830)],
  levels: [levels(28, 6790, 100), levels(30, 6805, -50), levels(32, 6820, -80)],
  prevDayBars: [bar(10, 6700, 6720, 6690)],
  cliff: { session_date: '2026-08-14', cliff_share: 0.15, is_opex: false },
  tendency: [] as TendencyRow[],
  profile: 'futures' as const,
  volRegime: null,
  macroEvent: null,
}

describe('composeContext', () => {
  it('bere řádek NEJBLIŽŠÍ k ts_ref, ne poslední dne', () => {
    // Záznam se často zapisuje zpětně — poslední řádek by popisoval jinou minutu
    const context = composeContext(BASE)
    expect(context.flip).toBe(6805)
    expect(context.spot).toBe(6810)
    expect(context.total_gex).toBe(-50)
  })

  it('vzdálenost k flipu je se znaménkem', () => {
    expect(composeContext(BASE).dist_to_flip).toBeCloseTo(5)
  })

  it('rozsah seance končí u ts_ref — pozdější extrémy jsem tehdy neviděl', () => {
    const context = composeContext(BASE)
    // Do rozsahu jde jen bar(28); bar(32) je až po záznamu a bar(30) je
    // minuta záznamu samotného, která ještě nebyla uzavřená (stejná
    // konvence jako overnight řez v briefingu). V retrospektivě by pozdější
    // extrémy tvořily falešnou jistotu „to jsem přece viděl".
    expect(context.session?.high).toBe(6802)
    expect(context.session?.low).toBe(6798)
  })

  it('chybějící zdroj dá null, nikdy nulu', () => {
    const context = composeContext({ ...BASE, levels: [], bars: [], cliff: null })
    expect(context.flip).toBeNull()
    expect(context.spot).toBeNull()
    expect(context.total_gex).toBeNull()
    expect(context.dist_to_flip).toBeNull()
    expect(context.cliff_share).toBeNull()
    expect(context.regime).toBeNull()
    expect(context.nearest_level).toBeNull()
  })

  it('nese verzi schématu — význam polí se bude vyvíjet', () => {
    expect(composeContext(BASE).version).toBe(CONTEXT_VERSION)
  })

  it('nevalidní ts_ref nespadne', () => {
    const context = composeContext({ ...BASE, tsRef: 'nesmysl' })
    expect(context.flip).toBeNull()
    expect(context.session).toBeNull()
  })

  it('tendence se páruje na nejbližší minutu', () => {
    const tendency: TendencyRow[] = [
      {
        ts_min: '2026-08-14T14:29:00+00:00',
        symbol: 'ES',
        score: 0.4,
        band: 'long',
        votes: [],
        weights_version: 1,
      },
    ]
    const context = composeContext({ ...BASE, tendency })
    expect(context.tendency_band).toBe('long')
    expect(context.tendency_score).toBeCloseTo(0.4)
  })
})

describe('nearestLevel', () => {
  it('vybere úroveň nejblíž ceně a nese znaménko vzdálenosti', () => {
    const near = nearestLevel(levels(30, 6805, -50), 6810)
    expect(near?.name).toBe('flip')
    expect(near?.distance).toBeCloseTo(-5)
  })

  it('úrovně bez hodnoty přeskočí', () => {
    const near = nearestLevel({ ...levels(30, null, -50), centroid: null }, 6810)
    expect(near?.name).toBe('call_wall')
  })

  it('bez ceny nebo bez úrovní nevymýšlí', () => {
    expect(nearestLevel(null, 6810)).toBeNull()
    expect(nearestLevel(levels(30, 6805, -50), null)).toBeNull()
  })
})

describe('futures vrstva v kontextu (#713)', () => {
  it('zachytí seanci, kontrakt a roll týden', () => {
    const context = composeContext(BASE)
    // 14:30 UTC = hodina po US open (13:30 UTC v létě) → RTH dopoledne
    expect(context.session_segment).toBe('dopoledne')
    expect(context.contract).toBe('ESU6')
    expect(context.roll_week).toBe(false)
  })

  it('volatilitní bucket se přebírá, nedopočítává', () => {
    const context = composeContext({
      ...BASE,
      volRegime: {
        session_date: '2026-08-14',
        symbol: 'ES',
        session_range: 60,
        percentile: 0.9,
        bucket: 'crisis',
        sample: 200,
        version: 1,
      },
    })
    expect(context.vol_bucket).toBe('crisis')
    expect(context.vol_percentile).toBeCloseTo(0.9)
  })

  it('chybějící režim zůstane null — NIKDY se nedosazuje "normal"', () => {
    expect(composeContext(BASE).vol_bucket).toBeNull()
    expect(composeContext(BASE).macro_event).toBeNull()
  })

  it('profil smb futures pole nepočítá — u akcií nedávají smysl', () => {
    const context = composeContext({ ...BASE, profile: 'smb' })
    expect(context.session_segment).toBeNull()
    expect(context.contract).toBeNull()
    expect(context.roll_week).toBeNull()
  })
})
