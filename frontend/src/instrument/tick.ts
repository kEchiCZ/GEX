/** Minimální cenový tick futures podkladů (IBKR nedodává → tabulka + default). */

/** Min tick per symbol; default 0,25 (index futures). Rozšiřuj dle potřeby. */
const PRICE_TICKS: Record<string, number> = {
  ES: 0.25,
  MES: 0.25,
  NQ: 0.25,
  MNQ: 0.25,
  RTY: 0.1,
  M2K: 0.1,
  YM: 1,
  MYM: 1,
  CL: 0.01,
  GC: 0.1,
}

/** Min tick daného symbolu (default 0,25). */
export function priceTick(symbol: string): number {
  return PRICE_TICKS[symbol] ?? 0.25
}

/** Zaokrouhlení ceny na nejbližší tick (crosshair na ose Y nemá jemnější rozlišení). */
export function snapToTick(price: number, tick: number): number {
  if (tick <= 0) return price
  return Math.round(price / tick) * tick
}

/** Počet desetinných míst pro daný tick (0,25 → 2, 0,1 → 1, 1 → 0). */
export function tickDecimals(tick: number): number {
  const text = String(tick)
  const dot = text.indexOf('.')
  return dot === -1 ? 0 : text.length - dot - 1
}
