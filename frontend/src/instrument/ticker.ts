/** Ticker instrumentu (#1191, ADR-0041): kořen produktu (`ES`) nebo pinovaný
kontrakt (`ESU6` = kód měsíce + poslední číslice roku). Zrcadlo enginu
`gexlens_engine/ticker.py` — stejná gramatika, aby watchlist nepustil nic,
co engine nezaloží. */

const MONTH_CODES = 'FGHJKMNQUVXZ'
const CONTRACT_RE = new RegExp(`^([A-Z0-9]{1,4})([${MONTH_CODES}])(\\d)$`)
const ROOT_RE = /^[A-Z0-9]{1,6}$/

export interface Ticker {
  root: string
  /** Kód kontraktu (`U6`); null = kořen, engine roluje sám. */
  contract: string | null
}

/** Rozloží ticker; null = neplatný (prázdný, znaky mimo A–Z/0–9, moc dlouhý). */
export function parseTicker(raw: string): Ticker | null {
  const text = raw.trim().toUpperCase()
  if (!text) return null
  const match = CONTRACT_RE.exec(text)
  if (match && text.length >= 3) return { root: match[1], contract: match[2] + match[3] }
  if (ROOT_RE.test(text)) return { root: text, contract: null }
  return null
}

/** CME produkty, které aplikace zná jako futures — zrcadlo `engine/ticker.py` (#206). */
const CME_FUTURES_ROOTS = new Set([
  'ES', 'NQ', 'RTY', 'YM', 'MES', 'MNQ', 'M2K', 'MYM',
  'CL', 'NG', 'GC', 'SI', 'HG', 'ZB', 'ZN', 'ZF', '6E', '6J', 'ZC', 'ZS', 'ZW',
]) // prettier-ignore

/** Futures (CME) vs. akcie/ETF/index (#206). */
export function isFutures(symbol: string): boolean {
  return CME_FUTURES_ROOTS.has(symbolRoot(symbol))
}

/** Kořen produktu pro lidské názvy a konfiguraci; nečitelný ticker beze změny. */
export function symbolRoot(symbol: string): string {
  return parseTicker(symbol)?.root ?? symbol.trim().toUpperCase()
}

/** Pinovaný kontrakt (`ESU6`), null u kořene. */
export function pinnedContract(symbol: string): string | null {
  const ticker = parseTicker(symbol)
  return ticker?.contract ? ticker.root + ticker.contract : null
}
