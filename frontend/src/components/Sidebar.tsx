/** Sbalitelný levý sidebar (SPEC 7.1): navigace obrazovek, watchlist, téma, verze.

Watchlist je editovatelný (CRUD /watchlist, issue #21) a kliknutí na symbol
přepne aktivní ticker celé aplikace (graf, expirace, dashboard).
*/
import { useCallback, useEffect, useState } from 'react'
import { Legend } from './Legend'
import { API_BASE, APP_ENV, APP_VERSION } from '../config'
import { frontContractCode } from '../instrument/expiry'
import { useAppState } from '../state/AppState'
import type { AppView } from '../state/AppState'

const NAV_ITEMS: Array<{ view: AppView; label: string }> = [
  { view: 'chart', label: 'Graf' },
  { view: 'dashboard', label: 'Dashboard' },
  { view: 'chain', label: 'Řetěz' },
  { view: 'setups', label: 'Setupy' },
  { view: 'briefing', label: 'Briefing' },
  { view: 'journal', label: 'Deník' },
  { view: 'news', label: 'News' },
  { view: 'stats', label: 'Stats' },
  { view: 'settings', label: 'Settings' },
]

interface WatchlistItem {
  id: number
  symbol: string
}

/** Interval opakování po neúspěšném načtení watchlistu (#407). */
const WATCHLIST_RETRY_MS = 15_000

/** DEV badge (#568): viditelné odlišení vývojového prostředí od produkce.
Mimo sbalovací blok sidebaru — musí být vidět i se sbaleným menu. */
export function EnvBadge({ env = APP_ENV }: { env?: string }) {
  if (env !== 'dev') return null
  return (
    <span className="env-badge" title="Vývojové prostředí — kopie dat, ne produkce">
      DEV
    </span>
  )
}

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(false)
  // Legenda grafu (#346) — modál nad aplikací, ať jde porovnávat s grafem
  const [legendOpen, setLegendOpen] = useState(false)
  const { view, setView, theme, setTheme, symbol: activeSymbol, setSymbol } = useAppState()
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([])
  const [newSymbol, setNewSymbol] = useState('')
  const [watchlistError, setWatchlistError] = useState<string | null>(null)

  /** Načte watchlist ze serveru; null = API nedostupné/chyba (#407). */
  const fetchWatchlist = useCallback(async (): Promise<WatchlistItem[] | null> => {
    try {
      const response = await fetch(`${API_BASE}/watchlist`)
      if (!response.ok) return null
      const payload = (await response.json()) as { watchlist?: WatchlistItem[] }
      return payload.watchlist ?? []
    } catch {
      return null
    }
  }, [])

  // Načtení při startu se opakuje, dokud neprojde, a obnovuje se při návratu
  // do okna — jednorázový fetch při startu nechal watchlist po výpadku API
  // (restart kontejnerů) trvale prázdný (#407)
  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const sync = async () => {
      const list = await fetchWatchlist()
      if (cancelled) return
      if (list !== null) {
        setWatchlist(list)
      } else {
        // Souběžný sync (onFocus během čekajícího retry) nesmí timer přepsat
        // bez zrušení — vznikaly by paralelní retry řetězy (#506)
        window.clearTimeout(timer)
        timer = window.setTimeout(() => void sync(), WATCHLIST_RETRY_MS)
      }
    }
    void sync()
    const onFocus = () => void sync()
    window.addEventListener('focus', onFocus)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      window.removeEventListener('focus', onFocus)
    }
  }, [fetchWatchlist])

  const addSymbol = async () => {
    const symbol = newSymbol.trim().toUpperCase()
    if (!symbol) return
    try {
      const response = await fetch(`${API_BASE}/watchlist`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol }),
      })
      if (response.status === 409) {
        // Symbol na serveru už je, jen ho lokální stav ztratil — místo tichého
        // zahození se seznam srovná se serverem a symbol se objeví (#407)
        const list = await fetchWatchlist()
        if (list !== null) setWatchlist(list)
        setNewSymbol('')
        setWatchlistError(null)
        return
      }
      if (!response.ok) {
        setWatchlistError(`Přidání ${symbol} selhalo (HTTP ${response.status})`)
        return
      }
      const item = (await response.json()) as WatchlistItem
      setWatchlist((previous) => [...previous, item])
      setNewSymbol('')
      setWatchlistError(null)
    } catch {
      setWatchlistError('API nedostupné — přidání se neprovedlo')
    }
  }

  const removeSymbol = async (item: WatchlistItem) => {
    try {
      const response = await fetch(`${API_BASE}/watchlist/${item.id}`, { method: 'DELETE' })
      if (response.ok || response.status === 404) {
        setWatchlist((previous) => previous.filter((entry) => entry.id !== item.id))
        setWatchlistError(null)
      } else {
        setWatchlistError(`Odebrání ${item.symbol} selhalo (HTTP ${response.status})`)
      }
    } catch {
      setWatchlistError('API nedostupné — smazání se neprovedlo')
    }
  }

  // Aktivní symbol vždy viditelný, i když (ještě) není ve watchlistu
  const rows: Array<{ id: number | null; symbol: string }> = watchlist.some(
    (item) => item.symbol === activeSymbol,
  )
    ? watchlist
    : [{ id: null, symbol: activeSymbol }, ...watchlist]

  return (
    <aside className={collapsed ? 'sidebar collapsed' : 'sidebar'} aria-expanded={!collapsed}>
      <button
        className="sidebar-toggle"
        aria-label={collapsed ? 'Rozbalit menu' : 'Sbalit menu'}
        onClick={() => setCollapsed((value) => !value)}
      >
        {collapsed ? '»' : '«'}
      </button>
      <EnvBadge />
      {!collapsed && (
        <>
          <nav aria-label="Hlavní navigace">
            <ul>
              {NAV_ITEMS.map((item) => (
                <li key={item.view}>
                  <button
                    className={view === item.view ? 'nav-item active' : 'nav-item'}
                    onClick={() => setView(item.view)}
                  >
                    {item.label}
                  </button>
                </li>
              ))}
              <li>
                {/* Uživatelský manuál (wiki) — statické HTML servírované aplikací */}
                {/* explicitní index.html — funguje v nginx i ve Vite dev serveru */}
                <a
                  className="nav-item nav-link"
                  href="/manual/index.html"
                  target="_blank"
                  rel="noreferrer"
                >
                  Manuál
                </a>
              </li>
              <li>
                <button
                  className="nav-item"
                  onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
                >
                  Theme: {theme === 'dark' ? 'Dark' : 'Light'}
                </button>
              </li>
            </ul>
          </nav>
          <section className="watchlist" aria-label="Watchlist">
            <h2>Watchlist</h2>
            {rows.length === 0 && <p className="muted">Prázdný</p>}
            <ul>
              {rows.map((entry) => {
                // TWS lokální symbol předního kontraktu (#189) — zadáním do
                // Interactive Brokers najdeš stejný graf (ES → ESU6)
                const twsCode = frontContractCode(entry.symbol, new Date())
                return (
                  <li key={entry.symbol} className="watchlist-row">
                    <button
                      className={
                        entry.symbol === activeSymbol
                          ? 'watchlist-symbol active'
                          : 'watchlist-symbol'
                      }
                      onClick={() => setSymbol(entry.symbol)}
                      aria-label={`Přepnout na ${entry.symbol}`}
                      title={twsCode ? `TWS: ${twsCode}` : undefined}
                    >
                      {entry.symbol}
                      {twsCode && <span className="muted tws-code"> ({twsCode})</span>}
                    </button>
                    {entry.id !== null && (
                      <button
                        className="watchlist-remove"
                        aria-label={`Odebrat ${entry.symbol}`}
                        title="Odebrat z watchlistu"
                        onClick={() => void removeSymbol(entry as WatchlistItem)}
                      >
                        ×
                      </button>
                    )}
                  </li>
                )
              })}
            </ul>
            <form
              className="watchlist-add"
              onSubmit={(event) => {
                event.preventDefault()
                void addSymbol()
              }}
            >
              <input
                value={newSymbol}
                onChange={(event) => setNewSymbol(event.target.value)}
                placeholder="Přidat ticker"
                aria-label="Nový symbol"
                maxLength={12}
              />
              <button type="submit" className="chip" aria-label="Přidat do watchlistu">
                +
              </button>
            </form>
            {watchlistError && (
              <p className="watchlist-error" role="alert">
                {watchlistError}
              </p>
            )}
          </section>
          <button className="nav-item legend-button" onClick={() => setLegendOpen(true)}>
            Legenda
          </button>
          <footer className="sidebar-footer">
            <button className="nav-item">Sign out</button>
            <span className="muted">v{APP_VERSION}</span>
          </footer>
        </>
      )}
      {legendOpen && <Legend onClose={() => setLegendOpen(false)} />}
    </aside>
  )
}
