/** Dialog zpráv news markeru (#408): klik na glyf v grafu otevře detail clusteru.

Marker v grafu unese jen barvu a glyf — titulky, důležitost a hlavně očekávaný
dopad na trh (Long/Short) potřebují vlastní plochu. Dopad se odvozuje stejně,
jako se barví marker (sentiment_dir, jinak znaménko skóre) — dialog nesmí
tvrdit něco jiného, než co uživatel vidí v grafu.
*/
import { useEffect } from 'react'
import { categoryGlyph, categoryLabel, countdownLabel } from '../api/news'
import type { NewsRow } from '../api/news'
import { REACTION_RANGE_MINUTES } from '../instrument/rangeselect'
import { expectedImpact } from '../heatmap/newsMarkers'
import type { NewsMarker } from '../heatmap/newsMarkers'

/** Čas události v lokální zóně uživatele (osa grafu je ve stejné zóně). */
function eventTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/** Badge očekávaného dopadu; nadcházející event dopad nemá — ukazuje countdown. */
function ImpactBadge({ row, upcoming }: { row: NewsRow; upcoming: boolean }) {
  if (upcoming) {
    return <span className="news-impact upcoming">{countdownLabel(row.ts_event)}</span>
  }
  const impact = expectedImpact(row)
  if (impact === 1) return <span className="news-impact long">Long ▲</span>
  if (impact === -1) return <span className="news-impact short">Short ▼</span>
  return <span className="news-impact neutral">Neutrální</span>
}

/** Řádek forecast/previous/actual u scheduled eventů — jinde nemá smysl. */
function ScheduledNumbers({ row }: { row: NewsRow }) {
  if (row.kind !== 'scheduled') return null
  const parts: string[] = []
  if (row.forecast !== null) parts.push(`očekávání ${row.forecast}`)
  if (row.previous !== null) parts.push(`minule ${row.previous}`)
  if (row.actual !== null) parts.push(`výsledek ${row.actual}`)
  if (parts.length === 0) return null
  return <p className="muted news-dialog-numbers">{parts.join(' · ')}</p>
}

/** ! až !!! podle důležitosti — stejná škála, jakou marker kóduje tloušťkou. */
function importanceMark(importance: number | null): string {
  return '!'.repeat(Math.min(3, Math.max(1, importance ?? 1)))
}

export function NewsMarkerDialog({
  marker,
  onClose,
  onSetRange,
  onSetPrePost,
}: {
  marker: NewsMarker
  onClose: () => void
  /** Range na reakční okno zprávy (#488): ts_event → +minut (okna 15/60 jako
      `news_reactions`). Bez handleru se tlačítka nekreslí (Daily pohled). */
  onSetRange?: (row: NewsRow, minutes: number) => void
  /** Pre/post srovnání (#489): A = event−15→event, B = event→+15. */
  onSetPrePost?: (row: NewsRow) => void
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Události seřazené časem; v clusteru jedné minuty rozhoduje pořadí příchodu
  const rows = [...marker.rows].sort((a, b) => a.ts_event.localeCompare(b.ts_event))

  return (
    <div className="legend-backdrop" onClick={onClose} role="presentation">
      <div
        className="legend-modal news-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Zprávy v čase markeru"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="legend-header">
          <h2>
            {marker.upcoming ? 'Nadcházející událost' : 'Zprávy'} · {eventTime(rows[0].ts_event)}
          </h2>
          <button className="legend-close" onClick={onClose} aria-label="Zavřít zprávy">
            ×
          </button>
        </header>
        <div className="legend-body">
          <ul className="news-dialog-list">
            {rows.map((row) => (
              <li key={row.id} className="news-dialog-item">
                <div className="news-dialog-head">
                  <span className="news-dialog-glyph" aria-hidden="true">
                    {categoryGlyph(row.category)}
                  </span>
                  <span className="muted">
                    {eventTime(row.ts_event)} · {categoryLabel(row.category)} ·{' '}
                    <span
                      className="news-dialog-importance"
                      title={`Důležitost ${row.importance ?? 1}/3`}
                    >
                      {importanceMark(row.importance)}
                    </span>
                  </span>
                  <ImpactBadge row={row} upcoming={marker.upcoming} />
                </div>
                <p className="news-dialog-title">{row.title}</p>
                {row.summary && <p className="muted news-dialog-summary">{row.summary}</p>}
                <ScheduledNumbers row={row} />
                {/* Range na reakční okno (#488) — u budoucích eventů okno ještě
                    neexistuje, tlačítka nemají co vybrat */}
                {onSetRange && !marker.upcoming && (
                  <div className="news-dialog-range">
                    {REACTION_RANGE_MINUTES.map((minutes) => (
                      <button
                        key={minutes}
                        className="chip"
                        onClick={() => onSetRange(row, minutes)}
                        title={`Nastaví okno ${minutes} min od události — profil a P/C ukážou, co se v reakci zobchodovalo (stejné okno jako měřené reakce)`}
                      >
                        ⧉ +{minutes} min
                      </button>
                    ))}
                    {onSetPrePost && (
                      <button
                        className="chip"
                        onClick={() => onSetPrePost(row)}
                        title="Duální okna A/B (#489): A = 15 min PŘED událostí, B = 15 min PO ní — diferenční profil B−A ukáže, co event změnil"
                      >
                        ⧉ pre/post ±15
                      </button>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}
