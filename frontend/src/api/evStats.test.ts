/** Expected Value na obchod (#911).

Klíčová vlastnost: (WinRate × AvgWin) − (LossRate × AvgLoss) je matematicky
totéž co prostý průměr výsledků — test to hlídá, aby budoucí úprava vzorce
(např. zaokrouhlení složek) EV tiše nerozjela od Ø R.
*/
import { describe, expect, it } from 'vitest'
import { evStats, evTooltip } from './setups'

describe('evStats', () => {
  it('rozloží výsledky na winrate a průměry', () => {
    const stats = evStats([100, 300, -150, -50])
    expect(stats).not.toBeNull()
    expect(stats?.n).toBe(4)
    expect(stats?.winRate).toBeCloseTo(0.5)
    expect(stats?.lossRate).toBeCloseTo(0.5)
    expect(stats?.avgWin).toBeCloseTo(200)
    expect(stats?.avgLoss).toBeCloseTo(100) // kladné číslo, vzorec ho odečítá
    expect(stats?.ev).toBeCloseTo(50)
  })

  it('EV ≡ prostý průměr výsledků (v R totéž co Ø R)', () => {
    const pnls = [2, -1, 0.5, -1, 3, -0.25, 0]
    const mean = pnls.reduce((sum, value) => sum + value, 0) / pnls.length
    expect(evStats(pnls)?.ev).toBeCloseTo(mean)
  })

  it('hrany: prázdno → null, jen výhry / jen prohry, nula je prohra', () => {
    expect(evStats([])).toBeNull()
    const onlyWins = evStats([1, 2])
    expect(onlyWins?.lossRate).toBe(0)
    expect(onlyWins?.avgLoss).toBe(0)
    expect(onlyWins?.ev).toBeCloseTo(1.5)
    const onlyLosses = evStats([-1, -3])
    expect(onlyLosses?.winRate).toBe(0)
    expect(onlyLosses?.ev).toBeCloseTo(-2)
    // Break-even (0) padá do proher — nesmí uměle zvyšovat win rate
    expect(evStats([0, 2])?.winRate).toBeCloseTo(0.5)
  })

  it('tooltip je odřádkovaný a nese výklad znaménka', () => {
    const stats = evStats([100, -50])
    const text = evTooltip(stats!, '$')
    expect(text).toContain('\n')
    expect(text).toContain('dlouhodobě vydělává')
    expect(text).toContain('dlouhodobě ztrácí')
    expect(text).toContain('(WinRate × AvgWin) − (LossRate × AvgLoss)')
  })
})
