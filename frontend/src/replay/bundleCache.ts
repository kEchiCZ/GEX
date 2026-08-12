/** In-memory LRU cache dekódovaných /replay balíků (#514).

Návrat na dříve zobrazený (symbol, expirace, den) renderuje okamžitě z paměti
místo nového stažení 12MB balíku. Kapacita malá (jednotky MB/kus); LRU pořadí
drží vestavěná Map (delete + set = přesun na konec). Cache žije na úrovni
modulu — přežívá přepínání symbolů, ne reload (to řeší HTTP cache, immutable
hlavičky pro uzavřené seance posílá API).
*/
import type { ReplayInputs } from './loader'

const CAPACITY = 4

const cache = new Map<string, ReplayInputs>()

export function bundleCacheKey(symbol: string, expiry: string, date: string): string {
  return `${symbol}|${expiry}|${date}`
}

export function getCachedBundle(key: string): ReplayInputs | null {
  const hit = cache.get(key)
  if (!hit) return null
  // LRU dotek: nejčerstvěji použitý na konec
  cache.delete(key)
  cache.set(key, hit)
  return hit
}

export function storeCachedBundle(key: string, inputs: ReplayInputs): void {
  cache.delete(key)
  cache.set(key, inputs)
  while (cache.size > CAPACITY) {
    const oldest = cache.keys().next().value
    if (oldest === undefined) break
    cache.delete(oldest)
  }
}

/** Testy: čistý stav mezi případy (modulová cache jinak přežívá — vzor localStorage). */
export function clearBundleCache(): void {
  cache.clear()
}
