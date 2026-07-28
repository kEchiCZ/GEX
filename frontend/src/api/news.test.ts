/** Testy klienta SentimentLensu (#288): párování řady a formátování. */
import { describe, expect, test } from 'vitest'
import { alignSeriesToLabels, categoryGlyph, categoryLabel, countdownLabel } from './news'

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
