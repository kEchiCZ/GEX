/** Sbalitelný levý sidebar (SPEC 7.1): navigace, watchlist, verze. */
import { useState } from 'react'
import { APP_VERSION } from '../config'

const NAV_ITEMS = ['Dashboard', 'Watchlist', 'IBKR Console', 'Theme', 'Settings'] as const

export interface WatchlistEntry {
  symbol: string
  changePct: number | null
}

export function Sidebar({ watchlist = [] }: { watchlist?: WatchlistEntry[] }) {
  const [collapsed, setCollapsed] = useState(false)

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
                <li key={item}>
                  <button className="nav-item">{item}</button>
                </li>
              ))}
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
