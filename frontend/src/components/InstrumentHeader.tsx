/** Hlavička instrumentu (SPEC 7.1): ticker, last + změna, expirace, Live, notifikace. */
import { useEffect, useState } from 'react'
import { coverageLabel, greeksCoverage, oiCoverage } from '../instrument/coverage'
import type { Coverage } from '../instrument/coverage'
import { expiryCountdown, expiryIsoDate, expiryKind, expirySettleUtc } from '../instrument/expiry'
import { formatSettleWatch } from '../instrument/settlewatch'
import { REGIME_HINTS, REGIME_LABELS } from '../instrument/regime'
import { useAppState } from '../state/AppState'
import { ExpiryCalendar } from './ExpiryCalendar'
import { GammaCliffChip } from './GammaCliffChip'
import { IvRankChip } from './IvRankChip'
import { RelativeStrengthChip } from './RelativeStrengthChip'
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

/** Pokrytí s progress barem — číslo `84/158` samo o sobě přehlédneš (#470).

Neznámé pokrytí prvek NESKRÝVÁ (#758): do té doby zmizel celý a v panelu zbyly
jen ty ukazatele, které data měly — což vypadá jako rozbité rozhraní, ne jako
chybějící data. Přitom „tohle zrovna neměřím" je nejcennější právě při výpadku
zdroje. Místo zmizení se ukáže pomlčka a prázdný proužek. */
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
  // Tři stavy, tři barvy: neznámé (ztlumené), neúplné (žluté), plné (zelené).
  // Čekat na 100 % u Greeks je normální jen chvíli po startu.
  const state = coverage === null ? 'unknown' : coverage.ratio < 1 ? 'partial' : 'full'
  return (
    <span
      className={state === 'full' ? 'coverage' : `coverage coverage-${state}`}
      data-testid={testId}
      title={
        coverage === null
          ? `${title} Hodnotu teď nelze změřit — engine ji (ještě) neposlal, ` +
            'typicky při odpojeném IBKR nebo krátce po startu.'
          : title
      }
    >
      {label} {coverage === null ? '—' : coverageLabel(coverage)}
      <span className="coverage-bar" aria-hidden="true">
        <span
          className="coverage-fill"
          style={{ width: `${coverage === null ? 0 : Math.round(coverage.ratio * 100)}%` }}
        />
      </span>
    </span>
  )
}

/** Chip aktivního fallbacku na tastytrade (#614).

Vědomě se nekreslí, dokud oba zdroje jedou z IBKR: stav „vše normální" nemá
co říct a v hlavičce už je chipů dost. Zato jakmile se přepne cokoli, musí to
být vidět bez rozklikávání — uživatel jinak nepozná, že se dívá na jiná data
než obvykle. */
function FallbackChip({
  chainSource,
  spotSource,
}: {
  chainSource?: 'ibkr' | 'tasty'
  spotSource?: 'ibkr' | 'tasty' | 'none'
}) {
  const parts: string[] = []
  if (chainSource === 'tasty') parts.push('řetěz')
  if (spotSource === 'tasty') parts.push('cena')
  if (parts.length === 0) return null
  return (
    <span
      className="fallback-chip"
      data-testid="fallback-chip"
      title={
        `Data pro ${parts.join(' i ')} tečou z tastytrade, protože IBKR přestal ` +
        'dodávat — typicky souběh s přihlášením na mobilu (error 10197) nebo výpadek ' +
        'datové farmy. Graf běží dál. Po dobu fallbacku řetězu ale stojí CumΔ a net ' +
        'objem: tastytrade denní objem ve stejné sémantice nedodává, a vymyšlená nula ' +
        'by byla horší než viditelná díra.'
      }
    >
      ⤳ {parts.join(' + ')}: tastytrade
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
    expiryClasses,
    selectedExpiry,
    setSelectedExpiry,
    status,
    alerts,
    unreadAlerts,
    markAlertsRead,
    setView,
    regimeInfo,
    settleWatch,
  } = useAppState()
  const [alertsOpen, setAlertsOpen] = useState(false)
  const live = status.engine === 'online'
  // Odpočet do expirace se obnovuje po minutě (velké expirace = velké OI)
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60_000)
    return () => clearInterval(timer)
  }, [])
  // Zdroj per expirace (#616): expirace mimo IBKR množinu dodává tastytrade —
  // uživatel to musí poznat (BS greeks z kotací, bez objemů/flows), jinak by
  // jedna heatmapa tiše míchala dvě pravdy
  const extendedExpiries = new Set(status.tasty_extended_expiries?.[symbol] ?? [])
  const selectedIsExtended = selectedExpiry !== null && extendedExpiries.has(selectedExpiry)
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
      {/* Dva řádky (#752): identita a ovládání nahoře, kontextové chipy dole.
      Do #752 byla hlavička jediný `flex` bez `wrap` — jak přibývaly chipy
      (gamma útes, relativní síla, settle watch, režim, tendence, sentiment),
      vytlačily zvoneček i pokrytí mimo viditelnou plochu. Prosté `flex-wrap`
      by je zalamovalo náhodně podle šířky; tohle drží pevné pořadí, ve kterém
      „co se dívám" nikdy neuteče „na čem se dívám". */}
      <div className="header-row header-row-main">
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
        <div className="expiry-select">
          Expirace
          {/* Kalendářový popover místo dropdownu (#513, SPEC 3.2) — druh
          expirace i trading class jsou vidět při výběru, ne až po něm */}
          <ExpiryCalendar
            expiries={expiries}
            expiryClasses={expiryClasses}
            selected={selectedExpiry}
            onSelect={setSelectedExpiry}
            extended={extendedExpiries}
            now={now}
          />
        </div>
        {kind && (
          <span
            className="muted expiry-meta"
            data-testid="expiry-meta"
            title={
              selectedIsExtended
                ? 'Expirace mimo IBKR pokrytí — data z tastytrade (dxFeed): kotace + OI, greeks dopočtené BS modelem z mid ceny. Bez objemů a flows (ty nese jen IBKR).'
                : undefined
            }
          >
            {kind}
            {countdown && ` · expiruje ${countdown}`}
            {selectedIsExtended && ' · zdroj tastytrade'}
            {chainNote && ` · ${chainNote}`}
          </span>
        )}
      </div>
      {/* Druhý řádek: kontextové chipy (co POPISUJE stav trhu — režim, tendence,
      sentiment, settle watch) a vpravo stav dat se zvonečkem. Ten se sem vejde
      právě proto, že chipy odešly z prvního řádku; při užším okně se navíc
      zalomí na třetí řádek, místo aby zvoneček vytlačily z obrazovky. */}
      <div className="header-row header-row-context">
        {/* Gamma útes (#576): kolik gammy dnešní expirací odpadne — jen informace */}
        <GammaCliffChip symbol={symbol} />
        {/* Relativní síla ES vs. NQ (#680, Traders mode) — widget na zkoušku */}
        <RelativeStrengthChip />
        {/* Settle watch (#603): denní teze jednou větou — uzavřeme nad/pod klíčovou zdí? */}
        {settleWatch && selectedExpiry && (
          <span
            className="muted settle-watch"
            data-testid="settle-watch"
            title={
              `Settle watch (#603): nejvýznamnější zeď dne (${settleWatch.name}` +
              `${settleWatch.weak ? ', slabá/neznámá dominance' : ', silná dominance'}) ` +
              'a odstup ceny od ní se znaménkem (kladné = cena nad úrovní). ' +
              'Denní otázka: uzavřeme settle nad ní, nebo pod ní?'
            }
          >
            {`settle ${
              expirySettleUtc(selectedExpiry)?.toLocaleTimeString([], {
                hour: '2-digit',
                minute: '2-digit',
              }) ?? '—'
            } · ${formatSettleWatch(settleWatch)}`}
          </span>
        )}
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
                // Fallback bez flipu (#864): profil celý na jedné straně nuly
                regimeInfo.fromProfileSign
                  ? ' Flip leží mimo měřené pásmo — celý Dyn GEX profil je na jedné straně nuly, režim je odvozen ze znaménka profilu u spotu.'
                  : '',
              ].join('')
            }
          >
            {REGIME_LABELS[regimeInfo.state]}
          </span>
        )}
        {/* IV percentil (#871): kontext implied volatility vedle gamma režimu —
        režim říká TYP obchodu, IVR říká, jak draho trh oceňuje dnešní pohyb */}
        <IvRankChip />
        {/* Souhrnná tendence ceny (#350) — úplně nahoře, jedním pohledem */}
        <TendencyChip />
        {/* Chip RiskOn/RiskOff/Neutral (#295, SPEC 9.0) — news sentiment vedle GEX režimu */}
        <StateChip />
        {/* Aktivní fallback na tastytrade (#614) — ADR-0025 pravidlo 5 zakazuje
        tiché přepnutí zdroje. Chip svítí JEN při fallbacku: za normálního
        provozu by trvalé „zdroj: IBKR" jen zabíralo místo. */}
        <FallbackChip chainSource={status.chain_source} spotSource={status.spot_source} />
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
              label="OI"
              testId="coverage-oi"
              coverage={oiCoverage(status.oi_present, status.oi_filled, status.oi_missing)}
              title={
                'Kolik kontraktů aktivních řetězů má hodnotu OI (denní archiv IBKR + případné ' +
                'doplnění z tastytrade). Díra je typická pro denní expiraci ráno, než CME ' +
                'publikuje OI — flip a GEX pak stojí na řídké páteři a kreslí se ztlumeně.' +
                (status.oi_filled ? ` Z toho ${status.oi_filled} doplněno z tastytrade.` : '')
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
      </div>
    </header>
  )
}
