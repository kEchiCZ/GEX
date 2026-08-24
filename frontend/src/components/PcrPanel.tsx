/** PUT/CALL panel pod strike profilem (#469).

Dva nezávislé přepínače: CO se počítá (Vol + OI / Vol / OI) a V ČEM
(kontrakty / prémie $ / notional $). Absolutní hodnoty obou stran + poměr —
samotný poměr nerozliší klidný den od dne s trojnásobným objemem. Popiska
vždy říká, z čeho číslo vzniká (metrika · základ · expirace) — bez ní nikdo
neví, jestli poměr počítá dnešní tok, držené pozice, nebo obojí.

Sdílený výpočet `computePcr` převezme okenní panel #486 (tam pro range).
*/
import { useMemo } from 'react'
import type { ProfileRow } from '../profile/bars'
import { formatAmount } from '../profile/bars'
import {
  PCR_BASES,
  PCR_BASIS_LABELS,
  PCR_MISSING_LIMIT,
  PCR_SCOPES,
  PCR_SCOPE_LABELS,
  PCR_UNITS,
  PCR_UNIT_LABELS,
  computePcr,
  formatMoney,
  topPremiumStrikes,
} from '../profile/pcr'
import type { PcrBasis, PcrScope, PcrUnit } from '../profile/pcr'
import { pointValue } from '../instrument/tick'
import { oneOf, usePersistentState } from '../state/persist'

export function PcrPanel({
  rows,
  symbol,
  expiry,
  spot,
  windowLabel = null,
  compareRows = null,
}: {
  rows: ProfileRow[]
  symbol: string
  expiry: string | null
  spot: number | null
  /** Okenní režim (#486): popisek aktivního range — `rows` jsou pak OKENNÍ
      řádky (#484). Panel váží premiem (hlavní) + kusově (sekundární řádek),
      základ jen Vol (OI je v okně statické) a tooltip nese top 5 strikes. */
  windowLabel?: string | null
  /** Duální rozsah (#489): okenní řádky A a B — panel ukáže oba premium
      poměry vedle sebe + Δ (respektuje zvolený rozsah striků #645). */
  compareRows?: { a: ProfileRow[]; b: ProfileRow[] } | null
}) {
  const [basis, setBasis] = usePersistentState<PcrBasis>('pcrBasis', 'vol_oi', oneOf(PCR_BASES))
  const [unit, setUnit] = usePersistentState<PcrUnit>('pcrUnit', 'premium', oneOf(PCR_UNITS))
  // Rozsah striků (#645): default Jen OTM — ITM prémie je vnitřní hodnota,
  // ne sázka na směr (doloženo měřením v issue: P/C 0,05 kvůli ITM callům)
  const [scope, setScope] = usePersistentState<PcrScope>('pcrScope', 'otm', oneOf(PCR_SCOPES))
  const windowed = windowLabel !== null
  // Okno (#486): premium hlavní bar, kusový poměr sekundárně — oba najednou
  const effectiveBasis: PcrBasis = windowed ? 'vol' : basis
  const effectiveUnit: PcrUnit = windowed ? 'premium' : unit
  const result = useMemo(
    () =>
      computePcr(rows, effectiveBasis, effectiveUnit, pointValue(symbol), spot, undefined, scope),
    [rows, effectiveBasis, effectiveUnit, symbol, spot, scope],
  )
  const contractsResult = useMemo(
    () =>
      windowed
        ? computePcr(rows, 'vol', 'contracts', pointValue(symbol), spot, undefined, scope)
        : null,
    [windowed, rows, symbol, spot, scope],
  )
  const compare = useMemo(() => {
    if (!compareRows) return null
    const a = computePcr(
      compareRows.a,
      'vol',
      'premium',
      pointValue(symbol),
      spot,
      undefined,
      scope,
    )
    const b = computePcr(
      compareRows.b,
      'vol',
      'premium',
      pointValue(symbol),
      spot,
      undefined,
      scope,
    )
    if (a.ratio === null || b.ratio === null) return { a: a.ratio, b: b.ratio, delta: null }
    return { a: a.ratio, b: b.ratio, delta: b.ratio - a.ratio }
  }, [compareRows, symbol, spot, scope])
  const topStrikes = useMemo(
    () => (windowed ? topPremiumStrikes(rows, pointValue(symbol), 5, undefined, scope, spot) : []),
    [windowed, rows, symbol, scope, spot],
  )
  if (rows.length === 0) return null

  const format = effectiveUnit === 'contracts' ? formatAmount : formatMoney
  const total = result.put + result.call
  const putShare = total > 0 ? result.put / total : 0.5
  const unreliable = result.missingShare > PCR_MISSING_LIMIT
  // Prémie bez jediné použitelné kotace: rozlišit „nula" od „nevíme" (#835)
  const premiumUnavailable =
    effectiveUnit === 'premium' && result.put === 0 && result.call === 0 && rows.length > 0
  const title = windowed
    ? `P/C vybraného okna ${windowLabel} — premium ≈ objem okna × mid k t2 × multiplikátor ` +
      '(aproximace: neváží ceny v okamžicích obchodů). Zmrzlé kotace a striky bez midu vyloučené.' +
      (topStrikes.length > 0
        ? '\nTop strikes dle premium podílu:\n' +
          topStrikes
            .map(
              (item) =>
                `${item.strike} ${item.side} ${formatMoney(item.premium)} (${Math.round(item.share * 100)} %)`,
            )
            .join('\n')
        : '')
    : unit === 'premium'
      ? 'Prémie = počet × mid × multiplikátor. Mid (bid+ask)/2 k zobrazené minutě — ' +
        'u volume aproximace (neváží cenu v okamžiku obchodu). Zmrzlé kotace ' +
        '(ADR-0015) a striky bez midu jsou vyloučené' +
        (unreliable
          ? ` — teď ${Math.round(result.missingShare * 100)} % kontraktů, hodnota je zašedlá, protože může být zavádějící.`
          : '.')
      : unit === 'notional'
        ? 'Notional = počet × spot × multiplikátor — dolarová expozice podkladu, cena opce v tom není.'
        : 'Počty kontraktů bez váhy penězi — levné OTM křídlo váží stejně jako ATM pozice.'

  return (
    <div
      className={`pcr-panel${unreliable ? ' pcr-unreliable' : ''}`}
      data-testid="pcr-panel"
      title={title}
    >
      <div className="pcr-head">
        <span className="muted">PUT / CALL{windowed ? ` · ⧉ ${windowLabel}` : ''}</span>
        {!windowed && (
          <>
            <select
              value={basis}
              onChange={(event) => setBasis(event.target.value as PcrBasis)}
              aria-label="Základ P/C poměru"
            >
              {PCR_BASES.map((value) => (
                <option key={value} value={value}>
                  {PCR_BASIS_LABELS[value]}
                </option>
              ))}
            </select>
            <select
              value={unit}
              onChange={(event) => setUnit(event.target.value as PcrUnit)}
              aria-label="Jednotka P/C poměru"
            >
              {PCR_UNITS.map((value) => (
                <option key={value} value={value}>
                  {PCR_UNIT_LABELS[value]}
                </option>
              ))}
            </select>
          </>
        )}
        <select
          value={scope}
          onChange={(event) => setScope(event.target.value as PcrScope)}
          aria-label="Rozsah striků P/C poměru"
          title="Jen OTM = sázky na směr (ITM strana se vynechá — její prémie je hlavně vnitřní hodnota). Čas. hodnota = mid − intrinsic, jen u Prémie $. Bez spotu se chová jako Vše."
        >
          {PCR_SCOPES.map((value) => (
            <option key={value} value={value}>
              {PCR_SCOPE_LABELS[value]}
            </option>
          ))}
        </select>
      </div>
      <div className="pcr-bar" aria-hidden="true">
        <span className="pcr-bar-put" style={{ width: `${(putShare * 100).toFixed(1)}%` }} />
      </div>
      {/* Tichá nula je horší než přiznaná díra (#835, týž princip jako
      oi_missing u #465): když jsou mid ještě nenačtené nebo všechny zmrzlé,
      panel by ukázal PUT $0 / CALL $0 a vypadalo by to jako měření */}
      {premiumUnavailable ? (
        <div className="pcr-values muted" data-testid="pcr-unavailable">
          Prémie nejsou k dispozici — kotace se načítají nebo jsou zmrzlé
        </div>
      ) : (
        <div className="pcr-values">
          <span className="pcr-put">PUT {format(result.put)}</span>
          <span className="pcr-ratio" data-testid="pcr-ratio">
            P/C {result.ratio === null ? '—' : result.ratio.toFixed(2)}
          </span>
          <span className="pcr-call">CALL {format(result.call)}</span>
        </div>
      )}
      {/* Sekundární kusový poměr okna (#486) — premium hlavní, kusy vedle */}
      {windowed && contractsResult && (
        <div className="muted pcr-window-contracts" data-testid="pcr-window-contracts">
          kusově PUT {formatAmount(contractsResult.put)} · P/C{' '}
          {contractsResult.ratio === null ? '—' : contractsResult.ratio.toFixed(2)} · CALL{' '}
          {formatAmount(contractsResult.call)}
        </div>
      )}
      {/* Srovnání oken A/B (#489): oba premium poměry + delta */}
      {compare && (
        <div className="muted pcr-compare" data-testid="pcr-compare">
          P/C A {compare.a === null ? '—' : compare.a.toFixed(2)} · B{' '}
          {compare.b === null ? '—' : compare.b.toFixed(2)}
          {compare.delta !== null &&
            ` · Δ ${compare.delta >= 0 ? '+' : ''}${compare.delta.toFixed(2)}`}
        </div>
      )}
      {/* Popiska původu čísla — metrika · základ · expirace (#469) */}
      <div className="muted pcr-caption">
        {windowed
          ? `Prémie $ (≈ objem okna × mid k t2) · jen Vol`
          : `${PCR_UNIT_LABELS[unit]} · ${PCR_BASIS_LABELS[basis]}`}
        {` · ${PCR_SCOPE_LABELS[scope]}`}
        {expiry ? ` · exp ${expiry}` : ''}
      </div>
    </div>
  )
}
