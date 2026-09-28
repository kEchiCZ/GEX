/** Expected Value na obchod (#911): tooltip nad serverovým rozkladem (#1319).

Výpočet EV (včetně hlídání EV ≡ Ø R) žije na serveru v `compute/setup_summary.py`
a jeho testech; frontend jen vykresluje dosazený vzorec.
*/
import { describe, expect, it } from 'vitest'
import { evTooltip } from './setups'

describe('evTooltip', () => {
  it('je odřádkovaný a nese dosazený vzorec i výklad znaménka', () => {
    const text = evTooltip(
      { ev: 25, win_rate: 0.5, loss_rate: 0.5, avg_win: 100, avg_loss: 50, n: 2 },
      '$',
    )
    expect(text).toContain('\n')
    expect(text).toContain('(WinRate × AvgWin) − (LossRate × AvgLoss)')
    expect(text).toContain('= 50 % × 100 $ − 50 % × 50 $')
    expect(text).toContain('= +25 $ (n=2)')
    expect(text).toContain('dlouhodobě vydělává')
    expect(text).toContain('dlouhodobě ztrácí')
  })
})
