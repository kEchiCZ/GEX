/** Dialog zpráv news markeru (#408): klik na glyf v grafu otevře detail clusteru.

Marker v grafu unese jen barvu a glyf — titulky, důležitost a hlavně očekávaný
dopad na trh (Long/Short) potřebují vlastní plochu. Dopad se odvozuje stejně,
jako se barví marker (sentiment_dir, jinak znaménko skóre) — dialog nesmí
tvrdit něco jiného, než co uživatel vidí v grafu.
*/
import { memo, useEffect, useMemo, useState } from 'react'
import { categoryGlyph, categoryLabel, countdownLabel } from '../api/news'
import type { ChartNewsRow } from '../api/news'
import { REACTION_RANGE_MINUTES } from '../instrument/rangeselect'
import { expectedImpact } from '../heatmap/newsMarkers'
import { NewsExplain } from './NewsExplain'
import type { NewsMarker } from '../heatmap/newsMarkers'

/** Čas události v lokální zóně uživatele (osa grafu je ve stejné zóně);
`withDate` přidá den — zprávy mimo zobrazený den (historie, proklik z upozornění). */
function eventTime(iso: string, withDate = false): string {
  const at = new Date(iso)
  const time = at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  return withDate ? `${at.getDate()}. ${at.getMonth() + 1}. ${time}` : time
}

/** Badge očekávaného dopadu; nadcházející event dopad nemá — ukazuje countdown. */
function ImpactBadge({ row, upcoming }: { row: ChartNewsRow; upcoming: boolean }) {
  if (upcoming) {
    return <span className="news-impact upcoming">{countdownLabel(row.ts_event)}</span>
  }
  const impact = expectedImpact(row)
  if (impact === 1) return <span className="news-impact long">Long ▲</span>
  if (impact === -1) return <span className="news-impact short">Short ▼</span>
  return <span className="news-impact neutral">Neutrální</span>
}

/** Prahy verdiktu — shodné se `surprise_bucket` v news-engine (±0,5σ / ±1,5σ). */
const SURPRISE_SMALL = 0.5
const SURPRISE_LARGE = 1.5

/** Verdikt a směr z překvapení (#462 A): čitelný souhrn místo holých čísel.

Exportováno kvůli testům. Vrací null, dokud actual/surprise_z chybí. */
export function surpriseVerdict(row: ChartNewsRow): {
  text: string
  direction: number | null
} | null {
  const z = typeof row.surprise_z === 'number' ? row.surprise_z : Number(row.surprise_z)
  if (row.surprise_z === null || row.surprise_z === undefined || !Number.isFinite(z)) return null
  const magnitude = Math.abs(z)
  const sigma = `${z >= 0 ? '+' : ''}${z.toFixed(1)}σ`
  if (magnitude < SURPRISE_SMALL) return { text: `dle očekávání (${sigma})`, direction: null }
  const size = magnitude >= SURPRISE_LARGE ? 'výrazně ' : ''
  const side = z > 0 ? 'vyšší' : 'nižší'
  // Směr jen při překvapení ≥ 0,5σ — pod prahem je „flat" i pro statistiky
  // reakcí a šipka by předstírala signál, který tam není
  const direction = row.surprise_direction ?? null
  return { text: `${size}${side} než očekávání (${sigma})`, direction }
}

/** Řádek forecast/previous/actual u scheduled eventů — jinde nemá smysl. */
function ScheduledNumbers({ row }: { row: ChartNewsRow }) {
  if (row.kind !== 'scheduled') return null
  const parts: string[] = []
  if (row.forecast !== null) parts.push(`očekávání ${row.forecast}`)
  if (row.previous !== null) parts.push(`minule ${row.previous}`)
  if (row.actual !== null) parts.push(`výsledek ${row.actual}`)
  if (parts.length === 0) return null
  const verdict = surpriseVerdict(row)
  return (
    <>
      <p className="muted news-dialog-numbers">{parts.join(' · ')}</p>
      {verdict && (
        <p className="news-dialog-verdict" data-testid="scheduled-verdict">
          {verdict.text}
          {verdict.direction !== null && verdict.direction !== 0 && (
            <span
              className={verdict.direction > 0 ? 'news-impact long' : 'news-impact short'}
              title={
                'Odhad z konvence řady (nižší inflace = risk-on, silnější payrolls = risk-on…). ' +
                'Polarita je režimově závislá — v období „good news is bad news" se obrací. ' +
                'Není to signál, jen čtení překvapení.'
              }
            >
              {verdict.direction > 0 ? ' → risk-on ▲' : ' → risk-off ▼'}
            </span>
          )}
        </p>
      )}
    </>
  )
}

/** ! až !!! podle důležitosti — stejná škála, jakou marker kóduje tloušťkou. */
function importanceMark(importance: number | null): string {
  return '!'.repeat(Math.min(3, Math.max(1, importance ?? 1)))
}

/** Nad tolik zpráv v clusteru se drobné zprávy sbalí (#1290): na 60m nese
cluster i ~200 zpráv a významná by se ztratila mezi šumem. */
export const NEWS_DIALOG_COLLAPSE_OVER = 6

/** Zpráva, která se v dialogu nesbaluje: plánovaný event nebo importance ≥ 2. */
function isProminent(row: ChartNewsRow): boolean {
  return row.kind === 'scheduled' || (row.importance ?? 1) >= 2
}

/** Jedna zpráva dialogu — detail, vysvětlení a akce range. */
function NewsDialogItem({
  row,
  upcoming,
  withDate,
  inView,
  onSetRange,
  onSetPrePost,
}: {
  row: ChartNewsRow
  upcoming: boolean
  withDate: boolean
  inView: boolean
  onSetRange?: (row: ChartNewsRow, minutes: number) => void
  onSetPrePost?: (row: ChartNewsRow) => void
}) {
  return (
    <li className="news-dialog-item">
      <div className="news-dialog-head">
        <span className="news-dialog-glyph" aria-hidden="true">
          {categoryGlyph(row.category)}
        </span>
        <span className="muted">
          {eventTime(row.ts_event, withDate)} · {categoryLabel(row.category)} ·{' '}
          <span className="news-dialog-importance" title={`Důležitost ${row.importance ?? 1}/3`}>
            {importanceMark(row.importance)}
          </span>
        </span>
        <ImpactBadge row={row} upcoming={upcoming} />
      </div>
      <p className="news-dialog-title">{row.title}</p>
      {row.summary && <p className="muted news-dialog-summary">{row.summary}</p>}
      <ScheduledNumbers row={row} />
      {/* Vysvětlení zprávy přímo z grafu (#1126 3d, rozhodnutí uživatele 15. 9.) */}
      <NewsExplain eventId={row.id} title={row.title} />
      {/* Range na reakční okno (#488) — u budoucích eventů okno ještě
          neexistuje, tlačítka nemají co vybrat */}
      {onSetRange && !upcoming && inView && (
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
  )
}

/** Memo (#1290): rodič se překresluje se spot ticky (~5 Hz) a cluster na 60m
nese stovky zpráv — dialog se přestaví jen se změnou markeru nebo handlerů. */
export const NewsMarkerDialog = memo(function NewsMarkerDialog({
  marker,
  onClose,
  onSetRange,
  onSetPrePost,
  isInView,
}: {
  marker: NewsMarker
  onClose: () => void
  /** Range na reakční okno zprávy (#488): ts_event → +minut (okna 15/60 jako
      `news_reactions`). Bez handleru se tlačítka nekreslí (Daily pohled). */
  onSetRange?: (row: ChartNewsRow, minutes: number) => void
  /** Pre/post srovnání (#489): A = event−15→event, B = event→+15. */
  onSetPrePost?: (row: ChartNewsRow) => void
  /** Leží zpráva v zobrazeném dni? (#1290) Range umí jen osa zobrazeného dne —
      u historie a prokliku na jiný den se tlačítka nekreslí a čas nese datum.
      Bez predikátu = vše v zobrazeném dni. */
  isInView?: (row: ChartNewsRow) => boolean
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Pořadí: významné (plánované, importance ≥ 2) nahoře, drobné pod nimi —
  // obojí časem. Velký cluster drobné sbalí; čas se parsuje jednou per zprávu.
  const layout = useMemo(() => {
    const byTime = marker.rows
      .map((row) => ({ row, ms: Date.parse(row.ts_event), inView: isInView?.(row) ?? true }))
      .sort((a, b) => a.ms - b.ms)
    const prominent = byTime.filter((item) => isProminent(item.row))
    const minor = byTime.filter((item) => !isProminent(item.row))
    return {
      first: byTime[0]?.row ?? null,
      prominent,
      minor,
      collapsible: byTime.length > NEWS_DIALOG_COLLAPSE_OVER && minor.length > 0,
      withDate: byTime.some((item) => !item.inView),
    }
  }, [marker, isInView])
  // Rozbalení drobných platí pro jeden marker — nový marker začíná sbalený
  const [expandedFor, setExpandedFor] = useState<NewsMarker | null>(null)
  const showMinor = !layout.collapsible || expandedFor === marker
  const visible = showMinor ? [...layout.prominent, ...layout.minor] : layout.prominent
  if (!layout.first) return null

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
            {marker.upcoming ? 'Nadcházející událost' : 'Zprávy'} ·{' '}
            {eventTime(layout.first.ts_event, layout.withDate)}
          </h2>
          <button className="legend-close" onClick={onClose} aria-label="Zavřít zprávy">
            ×
          </button>
        </header>
        <div className="legend-body">
          <ul className="news-dialog-list">
            {visible.map((item) => (
              <NewsDialogItem
                key={item.row.id}
                row={item.row}
                upcoming={marker.upcoming}
                withDate={layout.withDate}
                inView={item.inView}
                onSetRange={onSetRange}
                onSetPrePost={onSetPrePost}
              />
            ))}
          </ul>
          {layout.collapsible && (
            <button
              type="button"
              className="chip news-dialog-more"
              aria-expanded={showMinor}
              onClick={() => setExpandedFor(showMinor ? null : marker)}
              title={
                'Drobné zprávy clusteru:\n' +
                '• důležitost 1, bez plánovaného eventu\n' +
                '• sbalené, aby se významné neztratily v šumu'
              }
            >
              {showMinor
                ? 'Skrýt drobné zprávy'
                : `+${layout.minor.length} drobných zpráv (důležitost 1)`}
            </button>
          )}
        </div>
      </div>
    </div>
  )
})
