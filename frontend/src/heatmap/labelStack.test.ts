/** Testy stohování cenovek úrovní (#238) — čistá funkce nad svislými boxy. */
import { expect, test } from 'vitest'
import { stackLabelRows } from './labelStack'

test('nekolidující štítky zůstávají na místě', () => {
  expect(
    stackLabelRows([
      { y: 10, height: 12 },
      { y: 40, height: 12 },
      { y: 100, height: 12 },
    ]),
  ).toEqual([10, 40, 100])
})

test('dvě úrovně na témže striku: druhý štítek pod první, pořadí vstupu', () => {
  // Max Pain a call zeď na 7525 → stejné y
  expect(
    stackLabelRows([
      { y: 200, height: 12 },
      { y: 200, height: 12 },
    ]),
  ).toEqual([200, 212])
})

test('částečný překryv se posune jen o nejmenší nutný kus, s mezerou', () => {
  expect(
    stackLabelRows(
      [
        { y: 50, height: 12 },
        { y: 55, height: 12 },
      ],
      2,
    ),
  ).toEqual([50, 64])
})

test('svislé pořadí drží pořadí podle y, výsledek indexovaný jako vstup', () => {
  // Vstup není seřazený: nižší štítek (y=30) je uveden první
  const result = stackLabelRows([
    { y: 30, height: 10 },
    { y: 25, height: 10 },
    { y: 28, height: 10 },
  ])
  expect(result).toEqual([45, 25, 35])
  // Řetězový posun: tři štítky na jednom místě → tři řádky pod sebou
  expect(
    stackLabelRows([
      { y: 0, height: 12 },
      { y: 0, height: 12 },
      { y: 0, height: 12 },
    ]),
  ).toEqual([0, 12, 24])
  expect(stackLabelRows([])).toEqual([])
})
