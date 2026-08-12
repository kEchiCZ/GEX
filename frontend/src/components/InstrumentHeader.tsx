/** Hlavička instrumentu (SPEC 7.1): ticker, last + změna, expirace, Live, notifikace. */
import { useEffect, useState } from 'react'
import { coverageLabel, greeksCoverage } from '../instrument/coverage'
import type { Coverage } from '../instrument/coverage'
import { expiryCountdown, expiryIsoDate, expiryKind } from '../instrument/expiry'
import { REGIME_HINTS, REGIME_LABELS } from '../instrument/regime'
import { useAppState } from '../state/AppState'
import { GammaCliffChip } from './GammaCliffChip'
import { StateChip } from './StateChip'
import { TendencyChip } from './TendencyChip'

/** Čas alertu (unix s) → lokální datum + čas; prázdné, když ts chybí/nevalidní. */
function alertTimestamp(ts: number): string {
  if (!Number.isFinite(ts)) return ''
  return new Date(ts * 1000).toLocaleString([], {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Zobrazovací názvy běžných futures podkladů (jinak jen ticker). */
const SYMBOL_NAMES: Record<string, string> = {
  ES: 'E-mini S&P 500',
  NQ: 'E-mini Nasdaq-100',
  RTY: 'E-mini Russell 2000',
  YM: 'E-mini Dow',
  CL: 'Crude Oil',
  GC: 'Gold',
}

/** Pokrytí s progress barem — číslo `84/158` samo o sobě přehlédneš (#470). */
function CoverageBadge({
  label,
  coverage,
  title,
  testId,
}: {
  label: string
  coverage: Coverage | null
  title: string
  testId: string
}) {
  if (!coverage) return null
  // Neúplná data hlásí barvu: čekat na 100 % u Greeks je normální jen chvíli po startu
  const incomplete = coverage.ratio < 1
  return (
    <span
      className={incomplete ? 'coverage coverage-partial' : 'coverage'}
      data-testid={testId}
      title={title}
    >
      {label} {coverageLabel(coverage)}
      <span className="coverage-bar" aria-hidden="true">
        <span className="coverage-fill" style={{ width: `${Math.round(coverage.ratio * 100)}%` }} />
      </span>
    </span>
  )
}

export function InstrumentHeader({
  lastPrice,
  changePct,
  ohlc,
}: {
  lastPrice?: number
  changePct?: number
  /** Pokrytí OHLC barů pro zobrazený den (#470); `null` = osu nelze změřit. */
  ohlc?: Coverage | null
}) {
  const {
    symbol,
    setSymbol,
    expiries,
    selectedExpiry,
    setSelectedExpiry,
    status,
    alerts,
    unreadAlerts,
    markAlertsRead,
    setView,
    regimeInfo,
  } = useAppState()
  const [alertsOpen, setAlertsOpen] = useState(false)
  const live = status.engine === 'online'
  // Odpočet do expirace se obnovuje po minutě (velké expirace = velké OI)
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60_000)
    return () => clearInterval(timer)
  }, [])
  const kind = selectedExpiry ? expiryKind(selectedExpiry) : null
  const countdown = selectedExpiry ? expiryCountdown(selectedExpiry, now) : null
  // Vztah chainu k zobrazené seanci (#352): proběhlá expirace se čte jako
  // replay dne expirace; budoucí chain se obchoduje nad dnešní seancí — bez
  // vysvětlivky mate, že „budoucnost už má svíčky".
  const expiryDate = selectedExpiry ? expiryIsoDate(selectedExpiry) : null
  const todayIso = now.toISOString().slice(0, 10)
  const chainNote =
    expiryDate === null
      ? null
      : expiryDate < todayIso
        ? 'proběhla — zobrazen den expirace'
        : expiryDate > todayIso
          ? 'svíčky = dnešní seance'
          : null

  return (
    <header className="instrument-header">
      <div className="instrument-title">
        <span className="ticker">{symbol}</span>
        <span className="name muted">{SYMBOL_NAMES[symbol] ?? ''}</span>
      </div>
      <div className="instrument-price">
        <span className="last">{lastPrice !== undefined ? lastPrice.toFixed(2) : '—'}</span>
        {changePct !== undefined && (
          <span className={changePct >= 0 ? 'change-up' : 'change-down'}>
            {changePct >= 0 ? '+' : ''}
            {changePct.toFixed(2)} %
          </span>
        )}
      </div>
      <label className="expiry-select">
        Expirace
        <select
          value={selectedExpiry ?? ''}
          onChange={(event) => setSelectedExpiry(event.target.value)}
          disabled={expiries.length === 0}
        >
          {expiries.length === 0 && <option value="">—</option>}
          {expiries.map((expiry) => (
            <option key={expiry} value={expiry}>
              {expiry}
            </option>
          ))}
        </select>
      </label>
      {kind && (
        <span className="muted expiry-meta" data-testid="expiry-meta">
          {kind}
          {countdown && ` · expiruje ${countdown}`}
          {chainNote && ` · ${chainNote}`}
        </span>
      )}
      {/* Gamma útes (#576): kolik gammy dnešní expirací odpadne — jen informace */}
      <GammaCliffChip symbol={symbol} />
      {regimeInfo.state && (
        // GEX režim (#209): jediná datově podložená hodnota vrstvy — TYP obchodu,
        // ne směr. Tooltip nese playbook hint + polohu flip zóny.
        <span
          className={`regime-badge regime-${regimeInfo.state}`}
          data-testid="regime-badge"
          title={
            REGIME_HINTS[regimeInfo.state] +
            [
              regimeInfo.measuredFlip !== null
                ? ` Měřený flip ${regimeInfo.measuredFlip.toFixed(0)}.`
                : '',
              regimeInfo.dynamicFlip !== null
                ? ` Dynamický flip ${regimeInfo.dynamicFlip.toFixed(0)}.`
                : '',
            ].join('')
          }
        >
          {REGIME_LABELS[regimeInfo.state]}
        </span>
      )}
      {/* Souhrnná tendence ceny (#350) — úplně nahoře, jedním pohledem */}
      <TendencyChip />
      {/* Chip RiskOn/RiskOff/Neutral (#295, SPEC 9.0) — news sentiment vedle GEX režimu */}
      <StateChip />
      {/* Pravý blok hlavičky (#597): pokrytí dat, Live a zvoneček drží u sebe u pravého
      okraje. Odsazuje je JEDEN `margin-left: auto` na téhle skupině — dva (dřív na badge
      i na zvonečku) si volné místo rozdělily a mezi nimi zbyla mezera. */}
      <div className="header-right">
        {/* Pokrytí dat u grafu, ne ve spodní liště (#470) — díra v datech musí být vidět
        tam, kam se člověk dívá. Drobné (#597), ať neruší zbytek hlavičky. */}
        <div className="coverage-group">
          <CoverageBadge
            label="Greeks"
            testId="coverage-greeks"
            coverage={greeksCoverage(status.greeks_complete, status.greeks_total)}
            title={
              'Kolik striků chainu má kompletní řecká (delta/gamma/vega). Neúplné pokrytí ' +
              'znamená, že část striků čeká na dopočet nebo se opakovaně nedaří — hodnoty ' +
              'v profilu a Dyn GEX pak stojí na menším vzorku.'
            }
          />
          <CoverageBadge
            label="OHLC"
            testId="coverage-ohlc"
            coverage={ohlc ?? null}
            title={
              'Kolik minut zobrazeného dne má cenovou svíčku proti tomu, kolik jich mělo ' +
              'podle časového rozpětí osy být. Méně než 100 % = díra ve sběru barů; ' +
              'cenová křivka pak spojuje body přes chybějící úsek.'
            }
          />
        </div>
        <span className={live ? 'live-indicator live' : 'live-indicator stale'} role="status">
          {live ? '● Live' : '○ Offline'}
        </span>
        <div className="bell-wrap">
          <button
            className="bell"
            aria-label={`Notifikace (${unreadAlerts})`}
            onClick={() => {
              setAlertsOpen((open) => !open)
              markAlertsRead()
            }}
          >
            🔔{unreadAlerts > 0 && <span className="badge">{unreadAlerts}</span>}
          </button>
          {alertsOpen && (
            <div className="alerts-dropdown" role="dialog" aria-label="Historie alertů">
              {alerts.length === 0 && <p className="muted">Žádné alerty</p>}
              <ol>
                {[...alerts].reverse().map((alert, index) => {
                  const stamp = alertTimestamp(alert.ts)
                  // Zvoneček je globální (napříč instrumenty) → u alertu i symbol
                  const tag = [alert.symbol, alert.kind].filter(Boolean).join(' · ')
                  // Setup alerty jsou proklikávací (#186): nový setup → graf
                  // instrumentu (karta + linie), výsledek → stránka Setupy.
                  // Alerty od staršího enginu bez `event` rozliší text zprávy.
                  const isSetup = alert.kind === 'setup' && alert.symbol !== ''
                  const isResult =
                    alert.event === 'closed' ||
                    (alert.event === undefined && alert.message.includes('uzavřen'))
                  const content = (
                    <>
                      {stamp && <time className="alert-time muted">{stamp}</time>}
                      <span className="muted">[{tag}]</span> {alert.message}
                    </>
                  )
                  return (
                    <li key={index}>
                      {isSetup ? (
                        <button
                          type="button"
                          className="alert-link"
                          aria-label={
                            isResult
                              ? `Otevřít vyhodnocení setupů ${alert.symbol}`
                              : `Otevřít graf ${alert.symbol}`
                          }
                          title={
                            isResult
                              ? `Otevřít vyhodnocení setupů ${alert.symbol}`
                              : `Otevřít graf ${alert.symbol}`
                          }
                          onClick={() => {
                            setSymbol(alert.symbol)
                            setView(isResult ? 'setups' : 'chart')
                            setAlertsOpen(false)
                          }}
                        >
                          {content}
                        </button>
                      ) : (
                        content
                      )}
                    </li>
                  )
                })}
              </ol>
            </div>
          )}
        </div>
      </div>
    </header>
  )
}
