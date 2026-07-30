/** Testy palet signed vrstvy (#204) — výchozí parita + charm/vanna barvy. */
import { expect, test } from 'vitest'
import { CHARM_PALETTE, DEFAULT_SIGNED_PALETTE, VANNA_PALETTE, renderGrid } from './render'
import type { HeatmapGrid } from './grid'

function signedGrid(values: number[], strikes: number[]): HeatmapGrid {
  return {
    minutes: values.length / strikes.length,
    strikes,
    layers: { signed: Float32Array.from(values) },
    staleAge: null,
  }
}

test('renderGrid: výchozí paleta signed vrstvy zůstává bit-shodná (#204)', () => {
  const grid = signedGrid([1, -1, 0.5, -0.5], [100, 105])
  const explicit = renderGrid(grid, 'gradient', DEFAULT_SIGNED_PALETTE)
  const implicit = renderGrid(grid, 'gradient')
  expect([...explicit.data]).toEqual([...implicit.data])
  // Historická parita se signedColor: kladná strana má R = 24 konstantně
  expect(explicit.data[0]).toBe(24)
})

test('renderGrid: charm a vanna palety obarví signed vrstvu jinak (#204)', () => {
  const grid = signedGrid([1], [100])
  const gamma = renderGrid(grid, 'gradient')
  const charm = renderGrid(grid, 'gradient', CHARM_PALETTE)
  const vanna = renderGrid(grid, 'gradient', VANNA_PALETTE)
  expect([...charm.data.slice(0, 3)]).toEqual([235, 170, 40])
  expect([...vanna.data.slice(0, 3)]).toEqual([20, 190, 170])
  expect([...gamma.data.slice(0, 3)]).not.toEqual([...charm.data.slice(0, 3)])

  // Záporná strana: charm modrá, vanna fialová
  const negativeCharm = renderGrid(signedGrid([-1], [100]), 'gradient', CHARM_PALETTE)
  expect([...negativeCharm.data.slice(0, 3)]).toEqual([70, 130, 240])
})
