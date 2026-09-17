/** Ticker = kořen nebo pinovaný kontrakt (#1191) — stejná gramatika jako engine. */
import { expect, test } from 'vitest'
import { parseTicker, pinnedContract, symbolRoot } from './ticker'

test('kořen vs. pinovaný kontrakt', () => {
  expect(parseTicker('ES')).toEqual({ root: 'ES', contract: null })
  expect(parseTicker(' esz6 ')).toEqual({ root: 'ES', contract: 'Z6' })
  expect(parseTicker('RTYH7')).toEqual({ root: 'RTY', contract: 'H7' })
  expect(parseTicker('6EZ6')).toEqual({ root: '6E', contract: 'Z6' })
  // Kořeny končící kódem měsíce zůstávají kořeny
  expect(parseTicker('6J')?.contract).toBeNull()
  expect(parseTicker('M2K')).toEqual({ root: 'M2K', contract: null })
  expect(parseTicker('')).toBeNull()
  expect(parseTicker('ES-Z6')).toBeNull()
  expect(parseTicker('TOOLONGROOT')).toBeNull()
})

test('pomocné', () => {
  expect(symbolRoot('NQU6')).toBe('NQ')
  expect(symbolRoot('NQ')).toBe('NQ')
  expect(pinnedContract('NQU6')).toBe('NQU6')
  expect(pinnedContract('NQ')).toBeNull()
})
