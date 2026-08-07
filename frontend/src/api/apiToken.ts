/** Sdílené tajemství API (#542) pro endpointy, které mění stav nebo exportují data.

Dnes ho potřebuje jen stažení zálohy (`GET /backup/postgres`) — dump nese celý
nenahraditelný archiv, takže endpoint stojí za tokenem i uvnitř sítě. Token se
drží v localStorage prohlížeče, ne v buildu: v image frontendu by se dostal ke
komukoli, kdo si stáhne bundle.
*/
const STORAGE_KEY = 'gexlens.apiToken'

export function loadApiToken(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return '' // privátní režim bez úložiště — token prostě nebude
  }
}

export function saveApiToken(token: string): void {
  try {
    const trimmed = token.trim()
    if (trimmed) localStorage.setItem(STORAGE_KEY, trimmed)
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Bez úložiště token nepřežije refresh; zápis selhat smí
  }
}

/** Hlavičky s tokenem; bez uloženého tokenu prázdné (API odpoví 401). */
export function tokenHeaders(): Record<string, string> {
  const token = loadApiToken()
  return token ? { 'X-GEXLens-Token': token } : {}
}
