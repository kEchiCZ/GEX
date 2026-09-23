/** Stav „tenká mapa" (#1245): popisky a tooltip nad stavem z /status. */
import { expect, test } from 'vitest'
import { mapStateLabel, mapStateTooltip } from './mapstate'
import type { MapStateInfo } from './mapstate'

const thin: MapStateInfo = {
  thin: true,
  thin_gamma: true,
  weak_walls: true,
  fused: true,
  reasons: ['tenká gamma', 'slabé zdi', 'slitá mapa'],
  gex_abs: 1.5,
  gamma_abs: 0.1,
  spread_pct: 0.00013,
  version: 1,
}

test('slitá mapa po OPEX: štítek 3/3 a tooltip s podmínkami pod sebou', () => {
  expect(mapStateLabel(thin)).toBe('tenká mapa (3/3)')
  const tooltip = mapStateTooltip(thin)
  expect(tooltip.split('\n')[0]).toContain('Tenká mapa')
  expect(tooltip).toContain('✓ slabé zdi')
  expect(tooltip).toContain('Rozpětí flip/zdi/max pain: 0.01 % ceny.')
  expect(tooltip).toContain('bez tlumení a bez pinu')
})

test('21. 9. 2026 (#1241): jen slabá zeď, tenká gamma bez historie → mapa OK (1/2)', () => {
  const partial: MapStateInfo = {
    ...thin,
    thin: false,
    thin_gamma: null,
    weak_walls: true,
    fused: false,
    reasons: ['slabé zdi'],
    spread_pct: 0.0157,
  }
  expect(mapStateLabel(partial)).toBe('mapa OK (1/3)')
  const tooltip = mapStateTooltip(partial)
  expect(tooltip).toContain('? tenká gamma')
  expect(tooltip).toContain('✗ slitá mapa')
  expect(tooltip).toContain('Prahy jsou relativní')
})
