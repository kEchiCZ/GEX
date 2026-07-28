/** Testy klienta SentimentLensu (#288): párování řady a formátování. */
import { describe, expect, test } from 'vitest'
import { alignSeriesToLabels, categoryGlyph, categoryLabel, countdownLabel } from './news'

function point(hour: number, minute: number, value: number) {
  const at = new Date(2026, 6, 28, hour, minute)
  return { ts_min: at.toISOString(), value }
}

describe('alignSeriesToLabels', () => {
  test('páruje hodnoty podle času, ne podle pořadí', () => {
    const series = [point(9, 0, 1), point(9, 2, 3)]
    expect(alignSeriesToLabels(series, ['09:00', '09:01', '09:02'])).toEqual([1, 1, 3])
  })

  test('minuty bez hodnoty drží poslední známou — index je spojitý', () => {
    const series = [point(9, 0, 2)]
    expect(alignSeriesToLabels(series, ['09:00', '09:01', '09:02'])).toEqual([2, 2, 2])
  })

  test('před první hodnotou je nula, ne extrapolace dozadu', () => {
    const series = [point(9, 2, 5)]
    expect(alignSeriesToLabels(series, ['09:00', '09:01', '09:02'])).toEqual([0, 0, 5])
  })

  test('prázdné vstupy nepadají', () => {
    expect(alignSeriesToLabels([], ['09:00'])).toEqual([0])
    expect(alignSeriesToLabels([point(9, 0, 1)], [])).toEqual([])
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
