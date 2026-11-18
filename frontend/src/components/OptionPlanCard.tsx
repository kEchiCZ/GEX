/** Karta plánu opce QQQ (F5a #1746, ADR-0062) v Briefingu → Scénář dne pod scénářem NQ, ze
kterého plán vznikl (Rozhodnuto 18. 11. F5a Q2 A v #1738). Zobrazuje odpověď `GET /option-plans`
a nic nepočítá: R:R, P3, P4, ceny opce a třídu rozpočtu má engine.

- Před spouští (K12 A): opce s kotacemi z 9:15 a z přecenění v 9:31, model v TP1/TP2, vstup A,
  cíle, exit a P5. SL, R:R a rozpočet jsou známé až při spoušti.
- Od spouště do vyhodnocení: osa odeslaných přechodů F3b (`live_alerts`, Q1 B). Ceny a R:R
  nese jen zpráva (zvonek, Telegram).
- Po vyhodnocení (`result`, F3): spoušť, SL, průběh a po variantách P3, P4 a naměřené P&L.

Vstup B hlavní plán nemá, je jen stínem v track recordu (K6 B). */
import { useEffect, useState } from 'react'
import {
  ACCOUNT_TIMING,
  LEVEL_LABELS,
  LIVE_EVENT_LABELS,
  fetchOptionPlanImage,
} from '../api/optionPlans'
import type {
  OptionPlanEntry,
  OptionPlanExit,
  OptionPlanMain,
  OptionPlanPricing,
  OptionPlanQuote,
  OptionPlanRow,
  OptionPlanVariantResult,
} from '../api/optionPlans'

export const OPTION_PLAN_TOOLTIPS = {
  quote: [
    'Kotace z řetězu QQQ 1DTE (tastytrade):',
    '• bid / ask, IV a delta kontraktu',
    '• čas = okamžik kotace',
    '• přecenění v 9:31 ET mění jen kotace, IV a model (K4 C)',
  ].join('\n'),
  model: [
    'Model ceny opce v TP1 / TP2:',
    '• Black–Scholes s IV kontraktu z kotace',
    '• v čase cíle podle cesty scénáře, nejpozději v exitu',
    '• naměřené ceny přinese vyhodnocení po close',
  ].join('\n'),
  atTrigger: [
    'Až při spoušti vstupu A (ADR-0062 K12 A):',
    '• SL = 5min close za extrémem od testu + 0,05 EM',
    '• R:R z ceny opce, pod 2 = neobchodovat (P3)',
    '• ztráta při SL a čerpání proti rozpočtu (P4)',
  ].join('\n'),
  entryA: [
    'Vstup A = odmítnutí od úrovně proti směru:',
    '• nejdřív 5min close za úrovní',
    '• pak první 5min close zpět = vstup',
    '• okno od prvního 5min close po US openu do exitu',
  ].join('\n'),
  entryB: [
    'Vstup B (průraz) hlavní plán nemá (K6 B):',
    '• jen stín v track recordu ve Stats',
    '• živá upozornění nemá',
  ].join('\n'),
  p5: [
    'P5 = stop navázaný na podklad:',
    '• podmíněný příkaz v TWS (cena QQQ → prodat opci)',
    '• podmínku spouští cena (poslední obchod), ne 5min close',
  ].join('\n'),
  live: [
    'Odeslané přechody živého stavu (F3b):',
    '• jen událost a čas',
    '• ceny, R:R a verdikt při spoušti nese zpráva (zvonek, Telegram)',
    '• výsledek z naměřených kotací ~15 min po close',
  ].join('\n'),
  result: [
    'Vyhodnocení po close (F3) z naměřených barů a kotací:',
    '• vstup za ask v minutě spouště, výstup za bid',
    '• cena při SL a TP z modelu s IV kotace při spoušti',
    '• $ na 1 kontrakt',
  ].join('\n'),
  budget: [
    'Třída rozpočtu (P4) podle Settings v době vyhodnocení:',
    '• čerpání P4 proti rozpočtu na obchod',
    '• hodnota rozpočtu se neukládá',
  ].join('\n'),
} as const

const RIGHT_LABELS: Record<string, string> = { C: 'call', P: 'put' }

/** Barva verdiktu podle hlavního slova (sloupec `option_plans.verdict`). */
const VERDICT_CLASSES: Record<string, string> = {
  obchodovat: 'option-plan-go',
  neobchodovat: 'option-plan-stop',
}

/** Místní čas 24h; `h23` i v prohlížeči s 12h locale (vedle štítků „9:15 ET“ a PNG). */
function hhmm(value: string | number): string {
  return new Date(value).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  })
}

/** `2026-11-18` → `18. 11.` */
function dayLabel(iso: string): string {
  const [, month, day] = iso.split('-')
  return `${Number(day)}. ${Number(month)}.`
}

function px(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(2)
}

/** $ na 1 kontrakt se znaménkem; null = mezera (kotace chybí). */
function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const rounded = Math.round(value)
  const sign = rounded > 0 ? '+' : rounded < 0 ? '−' : ''
  return `${sign}${Math.abs(rounded)} $`
}

function contract(option: { right: string; strike: number; expiry: string }): string {
  return `${RIGHT_LABELS[option.right] ?? option.right} ${option.strike} · exp. ${dayLabel(option.expiry)}`
}

function level(key: string, price: number): string {
  return `${LEVEL_LABELS[key] ?? key} ${px(price)}`
}

function label(verdict: string, reasons: readonly string[]): string {
  return reasons.length > 0 ? `${verdict} — ${reasons.join('; ')}` : verdict
}

function QuoteCell({ quote }: { quote: OptionPlanQuote }) {
  return (
    <>
      {px(quote.bid)} / {px(quote.ask)} · IV {(quote.iv * 100).toFixed(1)} % · Δ{' '}
      {quote.delta.toFixed(2)}
      <span className="muted">
        {' '}
        · {quote.source} {hhmm(quote.quote_ts)}
      </span>
    </>
  )
}

function PricingLine({ pricing }: { pricing: OptionPlanPricing }) {
  return (
    <>
      zaplaceno {px(pricing.paid)} · při SL {px(pricing.at_stop)} · R:R{' '}
      {pricing.rr === null ? 'nejde určit' : pricing.rr.toFixed(2)} · ztráta při SL{' '}
      {usd(pricing.loss_at_stop_usd)} · čerpání P4 {usd(pricing.drawdown_usd)} · TP1{' '}
      {usd(pricing.pnl_tp1_usd)}
      {pricing.pnl_tp2_usd !== null ? ` · TP2 ${usd(pricing.pnl_tp2_usd)}` : ''}
    </>
  )
}

function EntryLine({ entry }: { entry: OptionPlanEntry }) {
  return (
    <>
      vstup {entry.kind} {hhmm(entry.ts)} za {px(entry.price)} od{' '}
      {level(entry.level, entry.level_price)} · SL {px(entry.stop)} (5min close na podkladu)
    </>
  )
}

function PlanBody({ plan, main }: { plan: OptionPlanRow; main: OptionPlanMain }) {
  const effective = plan.effective
  const repriced = effective.prices?.['main'] ?? null
  const [tp1, tp2] = main.targets
  const [tp1Ts, tp2Ts] = main.target_ts
  return (
    <>
      <p className={`option-plan-verdict ${VERDICT_CLASSES[plan.verdict ?? ''] ?? ''}`}>
        <b>{main.verdict}</b>
      </p>
      <div className="option-plan-table-wrap">
        <table className="option-plan-variants">
          <thead>
            <tr>
              <th>varianta</th>
              <th>kontrakt</th>
              <th title={OPTION_PLAN_TOOLTIPS.quote}>kotace při vzniku</th>
              <th title={OPTION_PLAN_TOOLTIPS.quote}>přecenění 9:31</th>
              <th title={OPTION_PLAN_TOOLTIPS.model}>model TP1 / TP2</th>
              <th>stav</th>
            </tr>
          </thead>
          <tbody>
            {main.variants.map((variant) => {
              const later = repriced?.find(
                (quote) => quote.role === variant.role && quote.strike === variant.strike,
              )
              return (
                <tr key={variant.role} data-testid={`option-plan-variant-${variant.role}`}>
                  <td>{variant.role}</td>
                  <td>{contract(variant)}</td>
                  <td>
                    <QuoteCell quote={variant} />
                  </td>
                  <td>
                    {later ? (
                      <QuoteCell quote={later} />
                    ) : (
                      <span className="muted">
                        {effective.slot === '9:31'
                          ? 'plán sestaven v 9:31'
                          : effective.repricing_reasons === null
                            ? 'čeká na 9:31 ET'
                            : 'nezapsáno'}
                      </span>
                    )}
                  </td>
                  <td>{(later ?? variant).at_targets.map((value) => px(value)).join(' / ')}</td>
                  <td>
                    {label(variant.verdict, variant.reasons)}
                    {variant.pricing && (
                      <div className="muted">
                        <PricingLine pricing={variant.pricing} />
                      </div>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <ul className="option-plan-lines">
        <li title={OPTION_PLAN_TOOLTIPS.entryA}>
          {main.level_a
            ? `Vstup A: odmítnutí od ${level(main.level_a[0], main.level_a[1])} (5min close)`
            : 'Vstup A: žádná úroveň do 1 EM'}
          <span className="muted" title={OPTION_PLAN_TOOLTIPS.entryB}>
            {' '}
            · vstup B jen stín v track recordu
          </span>
        </li>
        <li>
          TP1 {px(tp1)}
          {tp1Ts ? ` (${hhmm(tp1Ts)})` : ''}
          {tp2 !== undefined ? ` → TP2 ${px(tp2)}${tp2Ts ? ` (${hhmm(tp2Ts)})` : ''}` : ''} · okno
          spouště do exitu {hhmm(main.exit_ts)}
        </li>
        {main.entry ? (
          <li>
            <EntryLine entry={main.entry} />
          </li>
        ) : (
          <li className="muted" title={OPTION_PLAN_TOOLTIPS.atTrigger}>
            SL, cena opce při SL, ztráta při SL, P&L na TP1 / TP2 a R:R: až při spoušti vstupu A
          </li>
        )}
        <li className="muted" title={OPTION_PLAN_TOOLTIPS.p5}>
          P5: {main.p5}
        </li>
        {main.missing.length > 0 && (
          <li className="option-plan-gap">chybí: {main.missing.join(', ')}</li>
        )}
        {effective.repricing_reasons !== null && effective.repricing_reasons.length > 0 && (
          <li className="option-plan-gap">9:31: {effective.repricing_reasons.join('; ')}</li>
        )}
      </ul>
    </>
  )
}

/** `loadedAt` = čas načtení seznamu: bez pollingu musí být stáří osy vidět (výpadek WS). */
function LiveLine({ plan, loadedAt }: { plan: OptionPlanRow; loadedAt: number | null }) {
  if (plan.result !== null || plan.effective.main === null) return null
  // Chybí u API před F4d #1841 (frontend jde nasadit i bez nového API)
  const alerts = plan.live_alerts
  return (
    <p className="muted" title={OPTION_PLAN_TOOLTIPS.live} data-testid="option-plan-live">
      Průběh živě{loadedAt === null ? '' : ` (stav k ${hhmm(loadedAt)})`}:{' '}
      {alerts === undefined
        ? 'zvonek a Telegram (API průběh zatím nevrací)'
        : alerts.length === 0
          ? 'zatím bez přechodu'
          : alerts
              .map(
                (alert) =>
                  `${LIVE_EVENT_LABELS[alert.event] ?? alert.event} ${hhmm(alert.ts)}${alert.final ? ' (konec)' : ''}`,
              )
              .join(' · ')}
      {' · ceny a R:R ve zprávě, výsledek ~15 min po close'}
    </p>
  )
}

function exitLabel(leg: OptionPlanExit | null): string {
  if (leg === null) return '—'
  return `${leg.event} ${hhmm(leg.ts)} ${usd(leg.pnl_usd)}`
}

function VariantResultRow({ variant }: { variant: OptionPlanVariantResult }) {
  const rejected = variant.kind === 'vyřazeno P3'
  return (
    <tr
      className={rejected ? 'option-plan-rejected' : undefined}
      data-testid={`option-plan-result-${variant.role}`}
    >
      <td>{variant.role}</td>
      <td>{contract(variant)}</td>
      <td>{rejected ? 'vyřazeno P3 — hypoteticky' : variant.kind}</td>
      <td>{variant.pricing ? <PricingLine pricing={variant.pricing} /> : '—'}</td>
      <td>{exitLabel(variant.exit_tp1)}</td>
      <td>{exitLabel(variant.exit_hold)}</td>
      <td title={OPTION_PLAN_TOOLTIPS.budget}>
        {variant.budget === 'nad rozpočtem' ? (
          <span className="option-plan-over">⚠ nad rozpočtem</span>
        ) : (
          (variant.budget ?? '—')
        )}
        {variant.gaps.length > 0 && <div className="muted">{variant.gaps.join('; ')}</div>}
      </td>
    </tr>
  )
}

function ResultBlock({ plan }: { plan: OptionPlanRow }) {
  const result = plan.result
  if (result === null) return null
  const part = result.main
  return (
    <div className="option-plan-result" data-testid="option-plan-result">
      <p title={OPTION_PLAN_TOOLTIPS.result}>
        <b>
          Výsledek: {part.status}
          {part.outcome ? ` → ${part.outcome}` : ''}
        </b>
        {plan.evaluated_at ? (
          <span className="muted"> · vyhodnoceno {hhmm(plan.evaluated_at)}</span>
        ) : null}
        {part.reasons.length > 0 ? (
          <span className="muted"> · {part.reasons.join('; ')}</span>
        ) : null}
      </p>
      {part.entry && (
        <p>
          <EntryLine entry={part.entry} />
        </p>
      )}
      {part.events.length > 0 && (
        <p className="muted">
          průběh:{' '}
          {part.events
            .map((event) => `${event.kind} ${hhmm(event.ts)} ${px(event.price)}`)
            .join(' → ')}
        </p>
      )}
      {part.variants.length > 0 && (
        <div className="option-plan-table-wrap">
          <table className="option-plan-variants">
            <thead>
              <tr>
                <th>varianta</th>
                <th>kontrakt</th>
                <th>druh</th>
                <th title={OPTION_PLAN_TOOLTIPS.result}>při spoušti (P3, P4)</th>
                <th>výstup v TP1</th>
                <th>držení do TP2</th>
                <th title={OPTION_PLAN_TOOLTIPS.budget}>rozpočet</th>
              </tr>
            </thead>
            <tbody>
              {part.variants.map((variant) => (
                <VariantResultRow key={variant.role} variant={variant} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/** PNG ze serveru se stavem plánu v klíči: po přecenění nebo vyhodnocení se načte znovu. */
function PlanImage({ plan }: { plan: OptionPlanRow }) {
  const stateKey = `${plan.id}|${plan.effective.slot}|${String(plan.effective.repricing_reasons !== null)}|${plan.evaluated_at ?? ''}`
  const [image, setImage] = useState<{ key: string; url: string | null; error: string | null }>()
  useEffect(() => {
    let cancelled = false
    let url: string | null = null
    void fetchOptionPlanImage(plan.id).then((loaded) => {
      if (cancelled) return
      if (loaded.ok) {
        url = URL.createObjectURL(loaded.blob)
        setImage({ key: stateKey, url, error: null })
      } else {
        setImage({ key: stateKey, url: null, error: loaded.error })
      }
    })
    return () => {
      cancelled = true
      if (url !== null) URL.revokeObjectURL(url)
    }
  }, [plan.id, stateKey])
  if (image?.key !== stateKey) return <p className="muted">Načítám graf plánu…</p>
  if (image.error !== null || image.url === null) {
    return (
      <p className="option-plan-error" role="alert" data-testid="option-plan-image-error">
        Graf plánu se nenačetl: {image.error}
      </p>
    )
  }
  return (
    <img
      className="scenario-image"
      src={image.url}
      alt={`Graf plánu opce QQQ ${plan.id}`}
      data-testid="option-plan-image"
    />
  )
}

function headline(plan: OptionPlanRow): string {
  const main = plan.effective.main
  const option = main?.variants[0]
  const what = option ? contract(option) : 'bez opce'
  return `Plán opce QQQ · ${dayLabel(plan.day)} · ${main ? `${main.direction} → ` : ''}${what}`
}

/** Důvody plánu bez opce z 9:15 i z 9:31; prázdné = viditelně „důvod chybí“, ne prázdný řádek. */
function noOptionReasons(plan: OptionPlanRow): string {
  const reasons = [...plan.effective.reasons, ...(plan.effective.repricing_reasons ?? [])]
  return reasons.length > 0 ? reasons.join('; ') : 'důvod chybí'
}

function stateLabel(plan: OptionPlanRow): string {
  const result = plan.result?.main
  if (result) return `${result.status}${result.outcome ? ` → ${result.outcome}` : ''}`
  return plan.verdict ?? 'bez opce'
}

export function OptionPlanCard({
  plan,
  compact = false,
  accountMissing,
  loadedAt = null,
}: {
  plan: OptionPlanRow
  compact?: boolean
  /** null = Settings se nenačetly */
  accountMissing: boolean | null
  /** Čas načtení seznamu plánů (ms) pro stáří živé osy */
  loadedAt?: number | null
}) {
  const main = plan.effective.main
  if (compact) {
    return (
      <div className="option-plan-card option-plan-compact" data-testid={`option-plan-${plan.id}`}>
        {headline(plan)} · <b>{stateLabel(plan)}</b>
      </div>
    )
  }
  return (
    <article className="option-plan-card" data-testid={`option-plan-${plan.id}`}>
      <div className="scenario-head">
        <strong>{headline(plan)}</strong>
        <span className="muted">plán z {plan.effective.slot} ET</span>
        <span className="scenario-verdict scenario-pending">{stateLabel(plan)}</span>
      </div>
      {accountMissing === true && (
        <p className="option-plan-warning" role="status" data-testid="option-plan-account-missing">
          ⚠ Účet plánu nenastaven (Settings → Plán opce QQQ — účet): plán dá „neobchodovat —
          rozpočet nenastaven“. {ACCOUNT_TIMING}
        </p>
      )}
      {accountMissing === null && (
        <p className="option-plan-error" data-testid="option-plan-account-unknown">
          Nastavení účtu plánu se nenačetlo — nevím, jestli je rozpočet nastaven.
        </p>
      )}
      {main ? (
        <PlanBody plan={plan} main={main} />
      ) : (
        <p className="option-plan-gap" data-testid="option-plan-no-option">
          Bez opce: {noOptionReasons(plan)}
        </p>
      )}
      <LiveLine plan={plan} loadedAt={loadedAt} />
      <ResultBlock plan={plan} />
      <PlanImage plan={plan} />
    </article>
  )
}
