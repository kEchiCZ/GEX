/** Sbalitelný levý sidebar (SPEC 7.1): navigace obrazovek, watchlist, téma, verze.

Watchlist je editovatelný (CRUD /watchlist, issue #21) a kliknutí na symbol
přepne aktivní ticker celé aplikace (graf, expirace, dashboard).
*/
import { useEffect, useState } from 'react'
import { API_BASE, APP_VERSION } from '../config'
import { useAppState } from '../state/AppState'
import type { AppView } from '../state/AppState'

const NAV_ITEMS: Array<{ view: AppView; label: string }> = [
  { view: 'chart', label: 'Graf' },
  { view: 'dashboard', label: 'Dashboard' },
  { view: 'console', label: 'IBKR Console' },
  { view: 'settings', label: 'Settings' },
]

interface WatchlistItem {
  id: number
  symbol: string
}

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(false)
  const { view, setView, theme, setTheme, symbol: activeSymbol, setSymbol } = useAppState()
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([])
  const [newSymbol, setNewSymbol] = useState('')

  useEffect(() => {
    let cancelled = false
    fetch(`${API_BASE}/watchlist`)
      .then((response) => (response.ok ? response.json() : { watchlist: [] }))
      .then((payload: { watchlist?: WatchlistItem[] }) => {
        if (!cancelled) setWatchlist(payload.watchlist ?? [])
      })
      .catch(() => {
        // API neběží — watchlist ukáže aspoň aktivní symbol
        if (!cancelled) setWatchlist([])
      })
    return () => {
      cancelled = true
    }
  }, [])

  const addSymbol = async () => {
    const symbol = newSymbol.trim().toUpperCase()
    if (!symbol) return
    try {
      const response = await fetch(`${API_BASE}/watchlist`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol }),
      })
      if (!response.ok) return // duplicitní symbol (409) apod. — beze změny
      const item = (await response.json()) as WatchlistItem
      setWatchlist((previous) => [...previous, item])
      setNewSymbol('')
    } catch {
      // API neběží — přidání se neprovede
    }
  }

  const removeSymbol = async (item: WatchlistItem) => {
    try {
      const response = await fetch(`${API_BASE}/watchlist/${item.id}`, { method: 'DELETE' })
      if (response.ok || response.status === 404) {
        setWatchlist((previous) => previous.filter((entry) => entry.id !== item.id))
      }
    } catch {
      // API neběží — smazání se neprovede
    }
  }

  // Aktivní symbol vždy viditelný, i když (ještě) není ve watchlistu
  const rows: Array<{ id: number | null; symbol: string }> =
    watchlist.some((item) => item.symbol === activeSymbol) || watchlist.length === 0
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
              {rows.map((entry) => (
                <li key={entry.symbol} className="watchlist-row">
                  <button
                    className={
                      entry.symbol === activeSymbol ? 'watchlist-symbol active' : 'watchlist-symbol'
                    }
                    onClick={() => setSymbol(entry.symbol)}
                    aria-label={`Přepnout na ${entry.symbol}`}
                  >
                    {entry.symbol}
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
              ))}
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
          </section>
          <footer className="sidebar-footer">
            <button className="nav-item">Sign out</button>
            <span className="muted">v{APP_VERSION}</span>
          </footer>
        </>
      )}
    </aside>
  )
}
