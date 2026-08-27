/** Index volatility zpráv (#567, princip z analýzy #561) — sekce ve Stats.

Směr a velikost jsou dvě různé informace: SentIndex říká KAM nálada táhne,
tenhle index JAK MOC trh na zprávy reaguje (denní průměr |reakce| v bp,
kontaminovaná okna mimo). Dlouhodobá pásma min/průměr/max dávají hodnotě
měřítko — bez nich nejde poznat, jestli je číslo velké. Extrémy jsou
ověřitelné: vrcholy musí sedět na známé epizody, jinak ukazatel nefunguje.
*/
import { useEffect, useMemo, useState } from 'react'
import { fetchNewsVol } from '../api/news'
import type { NewsVolBands, NewsVolPoint } from '../api/news'

const REFRESH_MS = 10 * 60_000
const WIDTH = 640
const HEIGHT = 140

/** Tooltip — odřádkovaný dle pravidla z 27. 8. */
function sectionTooltip(bands: NewsVolBands): string {
  return [
    'Index volatility zpráv = denní průměr |naměřené reakce| (bp, okno 5 min)',
    'přes všechny měřené zprávy dne; kontaminovaná okna se vynechávají (SPEC 5.1).',
    '',
    'Čtení:',
    '• u dlouhodobého minima — trh zprávy ignoruje (klid)',
    '• kolem průměru — běžný provoz',
    '• u maxima — panika/euforie: každá zpráva hýbe trhem',
    '',
    `Pásma z celé historie: min ${bands.min.toFixed(1)} · průměr ${bands.mean.toFixed(1)} · max ${bands.max.toFixed(1)} bp.`,
    'Velikost bez směru — KAM táhne nálada, říká SentIndex.',
  ].join('\n')
}

export function NewsVolSection({ symbol }: { symbol: string }) {
  const [series, setSeries] = useState<NewsVolPoint[]>([])
  const [bands, setBands] = useState<NewsVolBands | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      void fetchNewsVol(symbol).then((payload) => {
        if (cancelled) return
        // Defenzivně: starší API nebo cizí odpověď bez polí nesmí shodit Stats
        setSeries(payload.series ?? [])
        setBands(payload.bands ?? null)
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol])

  // Vrcholy jmenovitě (ověření AC: extrémy musí sedět na známé epizody)
  const peaks = useMemo(() => [...series].sort((a, b) => b.value - a.value).slice(0, 3), [series])

  if (series.length === 0 || bands === null) {
    return (
      <section className="stats-section" aria-label="Volatilita zpráv">
        <h2>Volatilita zpráv — {symbol}</h2>
        <p className="muted">
          Bez dat — index se skládá z naměřených reakcí na zprávy (news_reactions); řada se plní,
          jak reakční job měří.
        </p>
      </section>
    )
  }

  const top = Math.max(bands.max, 1e-9)
  const x = (index: number) => (index / Math.max(1, series.length - 1)) * WIDTH
  const y = (value: number) => HEIGHT - (value / top) * HEIGHT
  const path = series.map((point, index) => `${x(index).toFixed(1)},${y(point.value).toFixed(1)}`)
  const latest = series[series.length - 1]

  return (
    <section className="stats-section" aria-label="Volatilita zpráv">
      <h2 title={sectionTooltip(bands)}>Volatilita zpráv — {symbol}</h2>
      <p>
        Dnes{' '}
        <strong data-testid="newsvol-latest">
          {latest.value.toFixed(1)} bp ({latest.date})
        </strong>{' '}
        <span className="muted">
          · dlouhodobě min {bands.min.toFixed(1)} · průměr {bands.mean.toFixed(1)} · max{' '}
          {bands.max.toFixed(1)} bp
        </span>
      </p>
      <svg
        width={WIDTH}
        height={HEIGHT + 16}
        role="img"
        aria-label={`Index volatility zpráv ${symbol}`}
        data-testid="newsvol-chart"
      >
        {/* Pásma: průměr plnou, max čárkovaně — minimum splývá s osou */}
        <line x1={0} y1={y(bands.mean)} x2={WIDTH} y2={y(bands.mean)} className="newsvol-mean" />
        <line x1={0} y1={y(bands.max)} x2={WIDTH} y2={y(bands.max)} className="newsvol-max" />
        <polyline points={path.join(' ')} fill="none" className="newsvol-line" />
        <text x={0} y={HEIGHT + 12} className="stats-axis-label">
          {series[0].date}
        </text>
        <text x={WIDTH} y={HEIGHT + 12} textAnchor="end" className="stats-axis-label">
          {latest.date}
        </text>
      </svg>
      <p className="muted" data-testid="newsvol-peaks">
        Největší naměřené vrcholy:{' '}
        {peaks.map((peak) => `${peak.date} (${peak.value.toFixed(1)} bp)`).join(' · ')} — vrcholy
        mají sedět na známé epizody; pokud nesedí, ukazatel nefunguje.
      </p>
    </section>
  )
}
