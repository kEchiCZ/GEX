/** Karta Breaking news (E-6.28c, ADR-0059 bod 8): dvě místa, jedna komponenta.

* `BreakingNewsSection` — sekce na stránce News, vždy rozbalená;
* `BreakingNewsOverlay` — sbalitelný panel v rohu heatmapy. Sbalený je jen
  štítek: graf nezmenšuje a na API se neptá.

Dopad je naměřený z barů (API, #1497): hlavní číslo je změna ceny od posledního
close před zprávou, vedle výchylka; do 5. minuty „běží X min“, pak zafixováno.
Skóre z textu se nezobrazuje (#740).
*/
import { useEffect, useState } from 'react'
import {
  BREAKING_HOURS,
  BREAKING_LIMIT,
  BREAKING_SYMBOLS,
  UNCONFIRMED_TOOLTIP,
  delayLabel,
  excursionLabel,
  formatBp,
  groupLabel,
  impactStateLabel,
  impactTooltip,
  themeLabel,
} from '../api/breaking'
import type { BreakingCard, BreakingImpact, BreakingItem } from '../api/breaking'
import { relativeAge } from '../api/news'
import { useBreakingNews } from '../hooks/useBreakingNews'
import { usePersistentState } from '../state/persist'

/** Barva dle znaménka — stejný jazyk jako feed: teal +, červená −. */
function impactClass(impact: BreakingImpact): string {
  if (impact.ret_bp === null || impact.ret_bp === 0) return 'breaking-impact neutral'
  return impact.ret_bp > 0 ? 'breaking-impact positive' : 'breaking-impact negative'
}

function ImpactCell({ symbol, impact }: { symbol: string; impact: BreakingImpact }) {
  const showValue = impact.state === 'running' || impact.state === 'fixed'
  const excursion = showValue ? excursionLabel(impact) : null
  return (
    <span
      className={impactClass(impact)}
      title={impactTooltip(symbol, impact)}
      data-testid={`breaking-impact-${symbol}`}
    >
      <span className="breaking-symbol">{symbol}</span>{' '}
      {showValue && <strong>{formatBp(impact.ret_bp)}</strong>}
      {excursion && <span className="muted"> · {excursion}</span>}
      <span className="muted"> · {impactStateLabel(impact)}</span>
      {impact.contaminated && ' ⚠'}
    </span>
  )
}

function BreakingRow({ item, nowMs }: { item: BreakingItem; nowMs: number }) {
  const time = new Date(item.ts_event).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
  const theme = themeLabel(item.theme)
  return (
    <li className="breaking-item" data-testid={`breaking-${item.id}`}>
      <div className="breaking-meta">
        <span title={item.ts_event}>{time}</span>
        <span className="muted"> · {relativeAge(item.ts_event, nowMs)}</span>
        {item.is_key && <span className="breaking-badge key">zásadní</span>}
        <span className="breaking-badge">{groupLabel(item.group)}</span>
        {theme && <span className="breaking-badge theme">{theme}</span>}
        {!item.confirmed && (
          <span className="breaking-badge unconfirmed" title={UNCONFIRMED_TOOLTIP}>
            článek, zatím nepotvrzeno
          </span>
        )}
      </div>
      <div className="breaking-title">{item.title}</div>
      <div className="breaking-impacts">
        {BREAKING_SYMBOLS.map((symbol) =>
          item.impact[symbol] ? (
            <ImpactCell key={symbol} symbol={symbol} impact={item.impact[symbol]} />
          ) : null,
        )}
      </div>
      <div className="breaking-sources muted">
        {item.sources.map((source) => (
          <span
            key={source.source}
            title={`tier ${source.content_tier ?? '—'}\npublikace ${source.published_at}\npříjem ${source.fetched_at}`}
          >
            {source.source} {delayLabel(source.delay_s)}
          </span>
        ))}
      </div>
    </li>
  )
}

/** Seznam položek karty; chyba a prázdný stav jsou vidět, nic se nepředstírá. */
export function BreakingNewsList({
  card,
  error,
  nowMs,
}: {
  card: BreakingCard | null
  error: string | null
  nowMs: number
}) {
  if (error) {
    return (
      <p className="breaking-error" role="status" data-testid="breaking-error">
        Karta Breaking news se nenačetla: {error}
      </p>
    )
  }
  if (!card) return <p className="muted">Načítám…</p>
  const asOf = new Date(card.as_of).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
  // Stáří dat musí být vidět — visící dotaz jinak nechá čísla zmrzlá (SPEC 3.7)
  const footer = (
    <p className="muted breaking-footer" data-testid="breaking-as-of">
      stav k {asOf}
      {card.items.length >= BREAKING_LIMIT && ` · ${BREAKING_LIMIT} nejnovějších`}
    </p>
  )
  if (card.items.length === 0) {
    return (
      <>
        <p className="muted">
          Za posledních {BREAKING_HOURS} h žádná zpráva, která by na kartu patřila.
        </p>
        {footer}
      </>
    )
  }
  return (
    <>
      <ul className="breaking-list" aria-label="Breaking news">
        {card.items.map((item) => (
          <BreakingRow key={item.id} item={item} nowMs={nowMs} />
        ))}
      </ul>
      {footer}
    </>
  )
}

/** „Před X s/min“ tiká s obnovou karty; render zůstává čistý (#1123). */
function useNow(): number {
  const [nowMs, setNowMs] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 15_000)
    return () => window.clearInterval(timer)
  }, [])
  return nowMs
}

/** Sekce na stránce News — vždy rozbalená, dotazy jen dokud je stránka otevřená. */
export function BreakingNewsSection() {
  const { card, error } = useBreakingNews(true)
  const nowMs = useNow()
  return (
    <section className="breaking-section" aria-label="Breaking news">
      <h3>Breaking news</h3>
      <BreakingNewsList card={card} error={error} nowMs={nowMs} />
    </section>
  )
}

/** Sbalitelný panel v rohu heatmapy; sbalený se na API neptá a graf nezmenšuje. */
export function BreakingNewsOverlay() {
  const [open, setOpen] = usePersistentState<boolean>('breakingOpen', false, (value, fallback) =>
    typeof value === 'boolean' ? value : fallback,
  )
  const { card, error } = useBreakingNews(open)
  const nowMs = useNow()
  if (!open) {
    return (
      <button
        type="button"
        className="breaking-overlay-chip"
        onClick={() => setOpen(true)}
        title="Rozbalit kartu Breaking news (významné zprávy a naměřený dopad na ES/NQ)"
        data-testid="breaking-open"
      >
        ⚡ Breaking news
      </button>
    )
  }
  return (
    <aside className="breaking-overlay" aria-label="Breaking news">
      <header>
        <strong>⚡ Breaking news</strong>
        {card?.market_closed && <span className="muted"> · trh zavřený</span>}
        <button
          type="button"
          className="chip"
          onClick={() => setOpen(false)}
          title="Sbalit"
          data-testid="breaking-close"
        >
          ✕
        </button>
      </header>
      <BreakingNewsList card={card} error={error} nowMs={nowMs} />
    </aside>
  )
}
