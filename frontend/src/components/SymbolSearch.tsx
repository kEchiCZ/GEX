/** Vyhledání symbolu v hlavičce (#521, varianta C — rozhodnutí 27. 8.).

Našeptávač nad katalogem CME produktů (/search); výběr založí ad-hoc pohled
přes tastytrade (POST /adhoc) a přepne instrument — BEZ přidání do watchlistu
a bez zásahu do IBKR market data lines. Data ad-hoc symbolu drží engine,
dokud je pohled otevřený (ping řeší sidebar přes useAdhocPing).
*/
import { useEffect, useRef, useState } from 'react'
import { API_BASE } from '../config'
import { useAppState } from '../state/AppState'

interface SearchMatch {
  symbol: string
  name: string
}

/** Založení/prodloužení ad-hoc pohledu; chyby polyká — engine je zdroj pravdy. */
export async function requestAdhoc(symbol: string): Promise<void> {
  try {
    await fetch(`${API_BASE}/adhoc/${symbol}`, { method: 'POST' })
  } catch {
    // API nedostupné — pohled se prostě nezaloží, UI ukáže „bez dat"
  }
}

export function SymbolSearch() {
  const { setSymbol } = useAppState()
  const [query, setQuery] = useState('')
  const [matches, setMatches] = useState<SearchMatch[]>([])
  const debounce = useRef<number | null>(null)

  useEffect(() => {
    if (debounce.current !== null) window.clearTimeout(debounce.current)
    if (query.trim() === '') {
      setMatches([])
      return
    }
    debounce.current = window.setTimeout(() => {
      void fetch(`${API_BASE}/search?q=${encodeURIComponent(query.trim())}`)
        .then((response) => (response.ok ? response.json() : { matches: [] }))
        .then((payload: { matches?: SearchMatch[] }) => setMatches(payload.matches ?? []))
        .catch(() => setMatches([]))
    }, 200)
    return () => {
      if (debounce.current !== null) window.clearTimeout(debounce.current)
    }
  }, [query])

  const open = (symbol: string) => {
    void requestAdhoc(symbol)
    setSymbol(symbol)
    setQuery('')
    setMatches([])
  }

  const submit = () => {
    const needle = query.trim().toUpperCase()
    if (needle === '') return
    const exact = matches.find((match) => match.symbol === needle)
    const candidate = exact ?? matches[0]
    if (candidate) open(candidate.symbol)
  }

  return (
    <div className="symbol-search" data-testid="symbol-search">
      <input
        type="search"
        list="symbol-search-list"
        value={query}
        placeholder="Hledat symbol…"
        aria-label="Vyhledat symbol"
        onChange={(event) => {
          // Výběr položky z našeptávače (#983): prohlížeč hodnotu jen dosadí
          // (Chrome: inputType `insertReplacementText`), Enter nikdo nedá a
          // „vyhledávání nefunguje". Nabídnutý symbol se otevře rovnou.
          const inputType = (event.nativeEvent as InputEvent).inputType
          const picked = event.target.value.trim().toUpperCase()
          if (inputType === 'insertReplacementText' && matches.some((m) => m.symbol === picked)) {
            open(picked)
            return
          }
          setQuery(event.target.value)
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') submit()
        }}
        title={
          'Ad-hoc pohled přes tastytrade (#521): otevře positioning produktu bez přidání ' +
          'do watchlistu a bez IBKR linek. Bez flows/Cum Δ — ty nese jen plný sběr.'
        }
      />
      <datalist id="symbol-search-list">
        {matches.map((match) => (
          <option key={match.symbol} value={match.symbol}>
            {match.name}
          </option>
        ))}
      </datalist>
    </div>
  )
}
