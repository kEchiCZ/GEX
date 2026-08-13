/** Forward GEX (#519/#572): REST klient GET /gexforward/{symbol}. */
import { API_BASE } from '../config'
import type { ForwardBlock } from '../heatmap/dailyforward'

interface ForwardBlockWire {
  day: string
  grid_start: number
  grid_step: number
  values: number[]
  dropped_expiries: string[]
  dropped_share: number | null
  iv_fallback_share: number
}

export async function fetchGexForward(symbol: string): Promise<ForwardBlock[]> {
  try {
    const response = await fetch(`${API_BASE}/gexforward/${symbol}`)
    if (!response.ok) return []
    const payload = (await response.json()) as { days?: ForwardBlockWire[] }
    return (payload.days ?? []).map((block) => ({
      day: block.day,
      gridStart: block.grid_start,
      gridStep: block.grid_step,
      values: block.values,
      droppedExpiries: block.dropped_expiries,
      droppedShare: block.dropped_share,
      ivFallbackShare: block.iv_fallback_share,
    }))
  } catch {
    return [] // výpadek API = žádná projekce, ne rozbitý Daily pohled
  }
}
