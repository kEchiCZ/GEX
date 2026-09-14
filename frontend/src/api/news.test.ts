/** Testy klienta SentimentLensu (#288): párování řady a formátování. */
import { describe, expect, it, test } from 'vitest'
import {
  alignSeriesToLabels,
  categoryGlyph,
  categoryLabel,
  countdownLabel,
  episodeBadge,
  episodeTooltip,
  latestCrowd,
  primaryReaction,
  relativeAge,
} from './news'
import type { NewsRow, SentimentStateInfo } from './news'

function point(hour: number, minute: number, value: number) {
  const at = new Date(2026, 6, 28, hour, minute)
  return { ts_min: at.toISOString(), value }
}

/** Zrcadlí formát osy grafu (Intl, bez vodicí nuly u hodin): `9:05`. */
const label = (iso: string): string => {
  const at = new Date(iso)
  return `${at.getHours()}:${String(at.getMinutes()).padStart(2, '0')}`
}

describe('alignSeriesToLabels', () => {
  test('páruje hodnoty podle času, ne podle pořadí', () => {
    const series = [point(9, 0, 1), point(9, 2, 3)]
    expect(alignSeriesToLabels(series, ['9:00', '9:01', '9:02'], label)).toEqual([1, 1, 3])
  })

  test('minuty bez hodnoty drží poslední známou — index je spojitý', () => {
    const series = [point(9, 0, 2)]
    expect(alignSeriesToLabels(series, ['9:00', '9:01', '9:02'], label)).toEqual([2, 2, 2])
  })

  test('před první hodnotou je nula, ne extrapolace dozadu', () => {
    const series = [point(9, 2, 5)]
    expect(alignSeriesToLabels(series, ['9:00', '9:01', '9:02'], label)).toEqual([0, 0, 5])
  })

  test('formátování popisků musí sedět s osou, jinak řada tiše vyjde nulová', () => {
    // Vlastní formát s vodicí nulou se s osou (`9:00`) nesejde
    const wrong = (iso: string) => {
      const at = new Date(iso)
      return `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`
    }
    expect(alignSeriesToLabels([point(9, 0, 7)], ['9:00'], wrong)).toEqual([0])
    expect(alignSeriesToLabels([point(9, 0, 7)], ['9:00'], label)).toEqual([7])
  })

  test('prázdné vstupy nepadají', () => {
    expect(alignSeriesToLabels([], ['9:00'], label)).toEqual([0])
    expect(alignSeriesToLabels([point(9, 0, 1)], [], label)).toEqual([])
  })
})

describe('popisky', () => {
  test('kategorie mají české názvy a glyfy', () => {
    expect(categoryLabel('MACRO_INFLATION')).toBe('Inflace')
    expect(categoryGlyph('FED')).toBe('🏛')
    // Neznámá kategorie se nesmí ztratit ani shodit UI
    expect(categoryLabel('NOVA')).toBe('NOVA')
    expect(categoryLabel(null)).toBe('Nezařazeno')
    expect(categoryGlyph(null)).toBe('•')
  })

  test('countdown je čitelný', () => {
    const now = new Date(2026, 6, 28, 12, 0)
    const at = (h: number, m: number) => new Date(2026, 6, 28, h, m).toISOString()
    expect(countdownLabel(at(12, 8), now)).toBe('za 8 m')
    expect(countdownLabel(at(13, 12), now)).toBe('za 1 h 12 m')
    expect(countdownLabel(at(14, 0), now)).toBe('za 2 h')
    expect(countdownLabel(at(11, 0), now)).toBe('právě teď')
  })
})

describe('latestCrowd', () => {
  const row = (ts: string, metric: string, value: number, symbol = '') => ({
    ts,
    source: metric === 'pcr_volume' ? 'gexlens' : 'cnn_fg',
    metric,
    symbol,
    value,
    raw: null,
  })

  test('vybere nejnovější bod každé řady (#290)', () => {
    const latest = latestCrowd([
      row('2026-07-28T00:00:00Z', 'score', 40),
      row('2026-07-29T00:00:00Z', 'score', 55),
      row('2026-07-27T00:00:00Z', 'score', 70),
      row('2026-07-29T14:00:00Z', 'pcr_volume', 0.8, 'ES'),
      row('2026-07-29T14:05:00Z', 'pcr_volume', 0.9, 'ES'),
    ])
    expect(latest.get('cnn_fg|score|')?.value).toBe(55)
    expect(latest.get('gexlens|pcr_volume|ES')?.value).toBe(0.9)
    // Symboly tvoří samostatné řady
    expect(latest.get('gexlens|pcr_volume|NQ')).toBeUndefined()
  })
})

describe('karta zprávy (#656)', () => {
  const row = (reactions: Record<string, number | string> | null): NewsRow =>
    ({
      id: 1,
      ts_event: '2026-08-14T12:00:00Z',
      reactions_bp: reactions,
    }) as unknown as NewsRow

  it('primaryReaction preferuje 5m okno, jinak nejkratší', () => {
    expect(primaryReaction(row({ '5': -16.5, '15': -12 }))).toEqual({ windowMin: 5, bp: -16.5 })
    expect(primaryReaction(row({ '15': -12, '60': 3 }))).toEqual({ windowMin: 15, bp: -12 })
    expect(primaryReaction(row({ '1': '2.5' }))).toEqual({ windowMin: 1, bp: 2.5 }) // PG Decimal jako string
    expect(primaryReaction(row(null))).toBeNull()
    expect(primaryReaction(row({}))).toBeNull()
  })

  it('relativeAge: s → min → h → datum', () => {
    const now = Date.parse('2026-08-14T12:00:30Z')
    expect(relativeAge('2026-08-14T12:00:14Z', now)).toBe('před 16 s')
    expect(relativeAge('2026-08-14T11:57:30Z', now)).toBe('před 3 min')
    expect(relativeAge('2026-08-14T10:00:30Z', now)).toBe('před 2 h')
    expect(relativeAge('2026-08-12T10:00:30Z', now)).toMatch(/\d/) // starší = datum+čas
  })
})

describe('korekční epizody (#565)', () => {
  const base: SentimentStateInfo = {
    symbol: 'ES',
    state: 'Neutral',
    unconfirmed: false,
    unconfirmed_state: 'Neutral',
    last_close: -2.8,
    ma5: -3.2,
    ma10: -2.7,
    threshold: 0,
    current_wave: null,
    correction_threshold: -1.11,
    correction_threshold_d: 1,
    episode_horizon_h: 10,
    episode_params_version: 1,
  }

  it('badge: KOREKCE n d při probíhající, POKUS/NEGACE po rozhodnutí, nic bez epizody', () => {
    expect(episodeBadge({ ...base, episode_status: 'none', episode: null })).toBe('')
    expect(episodeBadge({ ...base })).toBe('')
    const open = {
      start_date: '2026-09-13',
      end_date: null,
      ref_level_z: 0.11,
      depth_z: 1.32,
      label: null,
      length_days: 1,
    }
    expect(episodeBadge({ ...base, episode_status: 'open', episode: open })).toBe('KOREKCE 1 d')
    expect(
      episodeBadge({ ...base, episode_status: 'attempt', episode: { ...open, label: 'attempt' } }),
    ).toBe('POKUS')
    expect(
      episodeBadge({
        ...base,
        episode_status: 'negation',
        episode: { ...open, label: 'negation' },
      }),
    ).toBe('NEGACE')
  })

  it('tooltip: odrážky pod sebou, práh v σ, předběžnost s počtem rozhodnutých', () => {
    const text = episodeTooltip(
      {
        ...base,
        episode_status: 'open',
        episode: {
          start_date: '2026-09-13',
          end_date: null,
          ref_level_z: 0.11,
          depth_z: 1.32,
          label: null,
          length_days: 1,
        },
        last_episode: {
          start_date: '2026-08-28',
          end_date: '2026-09-11',
          ref_level_z: 0.01,
          depth_z: 3.41,
          label: 'attempt',
          length_days: 10,
        },
      },
      3,
    )
    const lines = text.split('\n')
    expect(lines[0]).toContain('≥ 1.0 σ pod 20denní maximum')
    expect(lines).toContain(
      '• Probíhá od 2026-09-13: 1 d, hloubka 1.32 σ (třída až po zahlazení nebo 10. dni)',
    )
    expect(lines).toContain('• Práh dnes: -1.11 σ (20denní max − 1.0 σ)')
    expect(lines).toContain('• Poslední rozhodnutá: pokus 2026-08-28 → 2026-09-11, 3.41 σ')
    expect(lines[lines.length - 1]).toContain(
      'placeholder z prvního měření, ne kalibrace (rozhodnutých epizod: 3)',
    )
  })
})
