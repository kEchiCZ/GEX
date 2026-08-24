/** Chip RiskOn/RiskOff/Neutral v hlavičce (#295, SPEC 9.0).

Zelený RISK ON / červený RISK OFF / šedý NEUTRAL; tečka při nepotvrzené
intradenní změně. Klik otevírá popover se sparkline dnešního SentIndexu,
MA5/MA10 a aktivními topicy — data popoveru se stahují až při otevření.
*/
import { useEffect, useState } from 'react'
import { categoryLabel, fetchSentimentSeries, fetchTopics } from '../api/news'
import type { SentimentPoint, SentimentStateInfo, TopicRow } from '../api/news'
import { useAppState } from '../state/AppState'
import { useSentimentState } from '../hooks/useSentimentState'

const STATE_LABELS: Record<SentimentStateInfo['state'], string> = {
  RiskOn: 'RISK ON',
  RiskOff: 'RISK OFF',
  Neutral: 'NEUTRAL',
}

/** Sparkline SentIndexu jako SVG polyline; nulová osa čárkovaně.

Se `sigma` (#640) se hodnoty dělí σ(100 seancí) — výchylka se čte jako
„kolik σ od normálu", srovnatelně napříč érami feedu; surová čísla nese
dl blok pod grafem, žádná dvojí pravda. */
export function Sparkline({ series, sigma }: { series: SentimentPoint[]; sigma?: number | null }) {
  const width = 220
  const height = 48
  if (series.length < 2) return <p className="muted">Dnešní index zatím nemá data</p>
  const scale = sigma && sigma > 0 ? sigma : 1
  const values = series.map((point) => point.value / scale)
  const max = Math.max(...values.map(Math.abs), 0.01)
  const stepX = width / (series.length - 1)
  const y = (value: number) => height / 2 - (value / max) * (height / 2 - 2)
  const points = values.map((value, index) => `${(index * stepX).toFixed(1)},${y(value).toFixed(1)}`) // prettier-ignore
  const last = values[values.length - 1]
  return (
    <svg
      width={width}
      height={height}
      className="state-spark"
      role="img"
      aria-label="SentIndex sparkline"
    >
      <line x1={0} y1={height / 2} x2={width} y2={height / 2} className="state-spark-zero" />
      <polyline
        points={points.join(' ')}
        fill="none"
        className={last >= 0 ? 'state-spark-line positive' : 'state-spark-line negative'}
      />
    </svg>
  )
}

export function StateChip() {
  const { symbol } = useAppState()
  const state = useSentimentState()
  const [open, setOpen] = useState(false)
  const [series, setSeries] = useState<SentimentPoint[]>([])
  const [topics, setTopics] = useState<TopicRow[]>([])

  // Data popoveru až při otevření — chip sám žádný fetch navíc nepotřebuje
  useEffect(() => {
    if (!open) return
    let cancelled = false
    void Promise.all([fetchSentimentSeries(symbol), fetchTopics(true)]).then(
      ([points, topicRows]) => {
        if (cancelled) return
        setSeries(points)
        setTopics(topicRows)
      },
    )
    return () => {
      cancelled = true
    }
  }, [open, symbol])

  if (!state) return null
  const format = (value: number | null) => (value === null ? '—' : value.toFixed(2))
  return (
    <div className="state-chip-wrap">
      <button
        type="button"
        className={`state-chip state-${state.state.toLowerCase()}`}
        data-testid="state-chip"
        aria-label={`Stav sentimentu: ${STATE_LABELS[state.state]}`}
        title={
          state.unconfirmed
            ? `Nepotvrzená intradenní změna na ${state.unconfirmed_state} — potvrdí až denní close`
            : 'Poloha SentIndexu vůči MA5/MA10 (#563): RISK OFF = index pod oběma průměry ' +
              '(převažují negativní zprávy), RISK ON = nad oběma, NEUTRAL = mezi. ' +
              'Šipka = trend MA5 vs. MA10. Popisuje náladu, NE směr ceny — historicky ' +
              'se risk-off epizody vykupovaly. Klik → detail.'
        }
        onClick={() => setOpen((value) => !value)}
      >
        {STATE_LABELS[state.state]}
        {/* Polarita trendu (#563): ▲ MA5 nad MA10, ▼ pod — atribut, ne směr ceny */}
        {state.polarity && (
          <span className="state-polarity">{state.polarity === 'up' ? '▲' : '▼'}</span>
        )}
        {state.unconfirmed && <span className="state-dot">●</span>}
      </button>
      {open && (
        <div className="state-popover" role="dialog" aria-label="Detail stavu sentimentu">
          <Sparkline series={series} sigma={state.sigma} />
          {state.sigma != null && state.sigma > 0 && (
            <p className="muted state-sigma-note">
              osa v σ (100 seancí, #640) · dnes{' '}
              {state.last_close != null ? (state.last_close / state.sigma).toFixed(2) : '—'} σ
            </p>
          )}
          <dl className="state-metrics">
            <dt>Close</dt>
            <dd>{format(state.last_close)}</dd>
            <dt>MA5</dt>
            <dd>{format(state.ma5)}</dd>
            <dt>MA10</dt>
            <dd>{format(state.ma10)}</dd>
            <dt>Práh</dt>
            <dd>{format(state.threshold)}</dd>
          </dl>
          {topics.length > 0 && (
            <div className="state-topics">
              {topics.map((topic) => (
                <span
                  key={topic.category}
                  className={topic.value >= 0 ? 'state-topic positive' : 'state-topic negative'}
                >
                  {categoryLabel(topic.category)} {topic.value >= 0 ? '+' : ''}
                  {topic.value.toFixed(2)}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
