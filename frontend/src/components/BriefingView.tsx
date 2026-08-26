/** Ranní briefing (#674): plán dne na jedné obrazovce před US openem.

Čistá kompozice existujících dat (žádný nový výpočet): režim gammy + úrovně,
včerejší settle + overnight rozsah, dnešní odpad gammy (#576), Forward GEX
útesy týdne (#519), makro kalendář dne, ΔOI přes noc, stav sentimentu per
instrument. Tlačítko ☀ předvyplní ranní plán deníku (#673).
*/
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  VOL_BUCKET_LABELS,
  barsRange,
  briefingToPlanText,
  fetchBars,
  fetchCliffToday,
  fetchEmRespectSummary,
  fetchLevelsSeries,
  fetchOiDelta,
  fetchStoredDays,
  fetchVolRegimeLatest,
  gammaRegimeLabel,
  latestLevels,
  previousStoredDay,
  usOpenMs,
} from '../api/briefing'
import type { CliffToday, EmRespectSummary, LevelsRow, OiDeltaSummary, RangeSummary, VolRegimeRow } from '../api/briefing' // prettier-ignore
import type { ExpectedMove } from '../instrument/expectedmove'
import { categoryGlyph, fetchSentimentState, fetchUpcoming, isHighImpact } from '../api/news'
import type { NewsRow, SentimentStateInfo } from '../api/news'
import { useGexForward } from '../hooks/useGexForward'
import { sessionDateIso } from '../instrument/tz'
import { useAppState } from '../state/AppState'

const REFRESH_MS = 60_000
/** Instrumenty se stavem sentimentu — per symbol od ADR-0026. */
const SENTIMENT_SYMBOLS = ['ES', 'NQ'] as const

function formatCountdown(msLeft: number): string {
  if (msLeft <= 0) return 'US seance běží'
  const minutes = Math.floor(msLeft / 60_000)
  const hours = Math.floor(minutes / 60)
  return hours > 0 ? `US open za ${hours} h ${minutes % 60} min` : `US open za ${minutes} min`
}

function formatSigned(value: number): string {
  const rounded = Math.round(value)
  return `${rounded >= 0 ? '+' : ''}${rounded.toLocaleString('cs-CZ')}`
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="briefing-card" aria-label={title}>
      <h3>{title}</h3>
      {children}
    </section>
  )
}

export function BriefingView({ expectedMove = null }: { expectedMove?: ExpectedMove | null }) {
  const { symbol, selectedExpiry, setJournalDraft, setView } = useAppState()
  const dateIso = sessionDateIso()

  const [bars, setBars] = useState<RangeSummary | null>(null)
  const [overnight, setOvernight] = useState<RangeSummary | null>(null)
  const [prevDay, setPrevDay] = useState<RangeSummary | null>(null)
  const [prevDate, setPrevDate] = useState<string | null>(null)
  const [levels, setLevels] = useState<LevelsRow | null>(null)
  const [cliff, setCliff] = useState<CliffToday | null>(null)
  const [oiDelta, setOiDelta] = useState<OiDeltaSummary | null>(null)
  const [upcoming, setUpcoming] = useState<NewsRow[]>([])
  const [sentiments, setSentiments] = useState<Array<[string, SentimentStateInfo | null]>>([])
  // Karta Volatilita (#873): vol režim (ADR-0028) + statistika EM respect (#872)
  const [volRegime, setVolRegime] = useState<VolRegimeRow | null>(null)
  const [emRespect, setEmRespect] = useState<EmRespectSummary | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const forward = useGexForward(symbol, true)

  const reload = useCallback(() => {
    const openMs = usOpenMs(dateIso)
    void fetchBars(symbol, dateIso).then((rows) => {
      setBars(barsRange(rows))
      setOvernight(barsRange(rows, openMs))
    })
    void fetchStoredDays(symbol).then(async (days) => {
      const previous = previousStoredDay(days, dateIso)
      setPrevDate(previous)
      setPrevDay(previous ? barsRange(await fetchBars(symbol, previous)) : null)
    })
    if (selectedExpiry) {
      void fetchLevelsSeries(symbol, selectedExpiry, dateIso).then((rows) =>
        setLevels(latestLevels(rows)),
      )
      void fetchOiDelta(symbol, selectedExpiry).then(setOiDelta)
    }
    void fetchCliffToday(symbol).then(setCliff)
    void fetchVolRegimeLatest(symbol).then(setVolRegime)
    void fetchEmRespectSummary(symbol).then(setEmRespect)
    // Týdenní horizont (#830): bez něj nejde poznat, jestli je dnešek
    // klidný den, nebo den před velkým tiskem — a to mění čtení positioningu
    void fetchUpcoming(24 * 7).then(setUpcoming)
    void Promise.all(
      SENTIMENT_SYMBOLS.map(async (sym) => [sym, await fetchSentimentState(sym)] as const),
    ).then((pairs) => setSentiments(pairs.map(([sym, info]) => [sym, info])))
    setNow(Date.now())
  }, [symbol, selectedExpiry, dateIso])

  useEffect(() => {
    reload()
    const timer = window.setInterval(reload, REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [reload])

  const regime = useMemo(() => gammaRegimeLabel(levels, bars?.last ?? null), [levels, bars])

  // Makro dne: jen dnešní seance, významné (importance ≥ 2) napřed
  const todayEvents = useMemo(() => {
    const dayPrefix = dateIso
    return upcoming
      .filter((row) => row.ts_event.slice(0, 10) === dayPrefix)
      .sort(
        (a, b) => (b.importance ?? 1) - (a.importance ?? 1) || a.ts_event.localeCompare(b.ts_event),
      ) // prettier-ignore
      .slice(0, 8)
      .sort((a, b) => a.ts_event.localeCompare(b.ts_event))
  }, [upcoming, dateIso])

  // Výhled na týden (#830): nejbližší High-impact události PO dnešku —
  // odpovídá na „kde v týdnu leží těžiště rizika"
  const weekAhead = useMemo(() => {
    return upcoming
      .filter((row) => row.ts_event.slice(0, 10) > dateIso && isHighImpact(row))
      .sort((a, b) => a.ts_event.localeCompare(b.ts_event))
      .slice(0, 4)
  }, [upcoming, dateIso])

  // Volatility box (#873): EM z App (bez brány Traders mode), vol režim z API
  const planEm = expectedMove
    ? { em: expectedMove.em, anchor: expectedMove.anchor, preOpen: expectedMove.preOpen }
    : null

  const createPlan = () => {
    setJournalDraft({
      tsRef: new Date().toISOString(),
      text: briefingToPlanText({
        symbol,
        regime,
        levels,
        overnight,
        prevDay,
        cliff,
        vol: volRegime,
        em: planEm,
      }),
    })
    setView('journal')
  }

  const fmt = (value: number | null | undefined) =>
    value === null || value === undefined ? '—' : String(value)

  return (
    <main className="briefing-view" aria-label="Ranní briefing">
      <header className="briefing-header">
        <h2>
          ☀ Ranní briefing · {symbol} · {dateIso.split('-').reverse().join('.')}
        </h2>
        <span className="chip">{formatCountdown(usOpenMs(dateIso) - now)}</span>
        <button className="chip" onClick={createPlan} title="Otevře Deník s kostrou ranního plánu">
          ☀ Založit ranní plán do deníku
        </button>
      </header>

      <div className="briefing-grid">
        <Card title="Režim a úrovně">
          <p className="briefing-em">{regime}</p>
          {levels ? (
            <table className="briefing-table">
              <tbody>
                <tr>
                  <td>Flip</td>
                  <td>{fmt(levels.flip)}</td>
                </tr>
                <tr>
                  <td>Call wall</td>
                  <td>{fmt(levels.call_wall)}</td>
                </tr>
                <tr>
                  <td>Put wall</td>
                  <td>{fmt(levels.put_wall)}</td>
                </tr>
                <tr>
                  <td>Těžiště</td>
                  <td>{fmt(levels.centroid)}</td>
                </tr>
              </tbody>
            </table>
          ) : (
            <p className="muted">Levels dnešní seance zatím nejsou.</p>
          )}
        </Card>

        {/* Volatilita (#873, D1): vědomé potvrzení volatilitního kontextu před
        seancí. Hodnoty vedle sebe, ŽÁDNÉ slévání do jedné nálepky; bez dat se
        říká proč — nikdy se nedosazuje „normal" (zásada ADR-0028). */}
        <Card title="Volatilita">
          <table className="briefing-table">
            <tbody>
              <tr>
                <td>Režim (rozsah)</td>
                <td data-testid="vol-bucket">
                  {volRegime
                    ? `${VOL_BUCKET_LABELS[volRegime.bucket] ?? volRegime.bucket} · p${Math.round(volRegime.percentile * 100)} (${volRegime.sample} seancí)`
                    : 'bez dat — málo vzorků, nebo engine ještě nepočítal'}
                </td>
              </tr>
              <tr>
                <td>Expected move</td>
                <td data-testid="vol-em">
                  {planEm
                    ? `±${planEm.em.toFixed(1)} b (${((100 * planEm.em) / planEm.anchor).toFixed(2)} % spotu)${planEm.preOpen ? ' · pre-open odhad, openem se zamkne' : ' · zamknuto openem'}`
                    : 'bez straddlu — čeká na kotace ATM'}
                </td>
              </tr>
              <tr>
                <td>EM drží</td>
                <td data-testid="vol-emrespect">
                  {emRespect
                    ? `close uvnitř pásma ${Math.round(100 * emRespect.close_in_band_share)} % dnů (n=${emRespect.n}/${emRespect.window_days} d)`
                    : 'statistika se teprve sbírá (#872)'}
                </td>
              </tr>
            </tbody>
          </table>
        </Card>

        <Card title="Včera a overnight">
          <table className="briefing-table">
            <tbody>
              <tr>
                <td>
                  Včerejší settle
                  {prevDate ? ` (${prevDate.slice(5).split('-').reverse().join('.')}.)` : ''}
                </td>
                <td>{fmt(prevDay?.last)}</td>
              </tr>
              <tr>
                <td>Včerejší rozsah</td>
                <td>{prevDay ? `${prevDay.low} – ${prevDay.high}` : '—'}</td>
              </tr>
              <tr>
                <td>Overnight rozsah</td>
                <td>{overnight ? `${overnight.low} – ${overnight.high}` : '—'}</td>
              </tr>
              <tr>
                <td>Aktuální cena</td>
                <td>{fmt(bars?.last)}</td>
              </tr>
            </tbody>
          </table>
        </Card>

        <Card title="Gamma dnes a přes týden">
          {cliff?.cliff_share != null ? (
            <p className="briefing-em">
              Dnes odpadá ~{Math.round(cliff.cliff_share * 100)} % gammy
              {cliff.is_opex ? ' — OPEX!' : ''}
            </p>
          ) : (
            <p className="muted">Odpad gammy se spočítá po ranním OI archivu.</p>
          )}
          {forward.length > 1 && (
            <table className="briefing-table">
              <tbody>
                {forward.slice(1).map((block) => (
                  <tr key={block.day}>
                    <td>po {block.day.slice(5).split('-').reverse().join('.')}.</td>
                    <td>
                      {block.droppedShare !== null
                        ? `−${Math.round(block.droppedShare * 100)} % gammy`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="Makro kalendář dne">
          {todayEvents.length === 0 ? (
            <p className="muted">Dnes žádné plánované eventy v kalendáři.</p>
          ) : (
            <ul className="briefing-list">
              {todayEvents.map((row) => (
                <li key={row.id}>
                  <span className="muted">
                    {new Date(row.ts_event).toLocaleTimeString([], {
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </span>{' '}
                  {categoryGlyph(row.category)} {row.title}
                  {(row.importance ?? 1) >= 3 ? ' ❗' : ''}
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Výhled na týden">
          {weekAhead.length === 0 ? (
            <p className="muted">Do konce týdne nic s vysokým dopadem.</p>
          ) : (
            <ul className="briefing-list" data-testid="briefing-week-ahead">
              {weekAhead.map((row) => {
                const at = new Date(row.ts_event)
                const days = Math.round((at.getTime() - Date.now()) / 86_400_000)
                return (
                  <li key={row.id}>
                    <span className="muted">
                      {at.toLocaleDateString([], {
                        weekday: 'short',
                        day: 'numeric',
                        month: 'numeric',
                      })}{' '}
                      {at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                    </span>{' '}
                    {categoryGlyph(row.category)} {row.title} ❗
                    <span className="muted">{days <= 0 ? ' · dnes' : ` · za ${days} d`}</span>
                  </li>
                )
              })}
            </ul>
          )}
        </Card>

        <Card title="ΔOI přes noc">
          {oiDelta?.days?.previous ? (
            <>
              <p>
                Call {formatSigned(oiDelta.call_delta ?? 0)} · Put{' '}
                {formatSigned(oiDelta.put_delta ?? 0)}{' '}
                <span className="muted">
                  ({oiDelta.days.previous.slice(5)} → {oiDelta.days.current.slice(5)})
                </span>
              </p>
              <ul className="briefing-list">
                {(oiDelta.movers ?? []).slice(0, 5).map((row) => (
                  <li key={`${row.strike}${row.right}`}>
                    {row.strike} {row.right} {formatSigned(row.delta)}{' '}
                    <span className="muted">(OI {Math.round(row.oi).toLocaleString('cs-CZ')})</span>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="muted">ΔOI bude po ranním OI archivu (srovnání dvou dnů).</p>
          )}
        </Card>

        <Card title="Sentiment">
          {sentiments.length === 0 ? (
            <p className="muted">Stav sentimentu není k dispozici.</p>
          ) : (
            <ul className="briefing-list">
              {sentiments.map(([sym, info]) => (
                <li key={sym}>
                  <strong>{sym}</strong>:{' '}
                  {info ? (
                    <>
                      {info.state}
                      {info.unconfirmed ? ' (nepotvrzený, dnešní průběh)' : ''}
                    </>
                  ) : (
                    <span className="muted">bez dat</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </main>
  )
}
