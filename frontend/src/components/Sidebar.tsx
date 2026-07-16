/** Sbalitelný levý sidebar (SPEC 7.1): navigace obrazovek, watchlist, téma, verze. */
import { useState } from 'react'
import { APP_VERSION } from '../config'
import { useAppState } from '../state/AppState'
import type { AppView } from '../state/AppState'

const NAV_ITEMS: Array<{ view: AppView; label: string }> = [
  { view: 'chart', label: 'Graf' },
  { view: 'dashboard', label: 'Dashboard' },
  { view: 'console', label: 'IBKR Console' },
  { view: 'settings', label: 'Settings' },
]

export interface WatchlistEntry {
  symbol: string
  changePct: number | null
}

export function Sidebar({ watchlist = [] }: { watchlist?: WatchlistEntry[] }) {
  const [collapsed, setCollapsed] = useState(false)
  const { view, setView, theme, setTheme } = useAppState()

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
                <a className="nav-item nav-link" href="/manual/" target="_blank" rel="noreferrer">
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
            {watchlist.length === 0 && <p className="muted">Prázdný</p>}
            <ul>
              {watchlist.map((entry) => (
                <li key={entry.symbol} className="watchlist-row">
                  <span>{entry.symbol}</span>
                  <span
                    className={
                      entry.changePct === null
                        ? 'muted'
                        : entry.changePct >= 0
                          ? 'change-up'
                          : 'change-down'
                    }
                  >
                    {entry.changePct === null ? '—' : `${entry.changePct.toFixed(2)} %`}
                  </span>
                </li>
              ))}
            </ul>
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
