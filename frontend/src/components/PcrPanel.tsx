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
  PCR_UNITS,
  PCR_UNIT_LABELS,
  computePcr,
  formatMoney,
  topPremiumStrikes,
} from '../profile/pcr'
import type { PcrBasis, PcrUnit } from '../profile/pcr'
import { pointValue } from '../instrument/tick'
import { oneOf, usePersistentState } from '../state/persist'

export function PcrPanel({
  rows,
  symbol,
  expiry,
  spot,
  windowLabel = null,
}: {
  rows: ProfileRow[]
  symbol: string
  expiry: string | null
  spot: number | null
  /** Okenní režim (#486): popisek aktivního range — `rows` jsou pak OKENNÍ
      řádky (#484). Panel váží premiem (hlavní) + kusově (sekundární řádek),
      základ jen Vol (OI je v okně statické) a tooltip nese top 5 strikes. */
  windowLabel?: string | null
}) {
  const [basis, setBasis] = usePersistentState<PcrBasis>('pcrBasis', 'vol_oi', oneOf(PCR_BASES))
  const [unit, setUnit] = usePersistentState<PcrUnit>('pcrUnit', 'premium', oneOf(PCR_UNITS))
  const windowed = windowLabel !== null
  // Okno (#486): premium hlavní bar, kusový poměr sekundárně — oba najednou
  const effectiveBasis: PcrBasis = windowed ? 'vol' : basis
  const effectiveUnit: PcrUnit = windowed ? 'premium' : unit
  const result = useMemo(
    () => computePcr(rows, effectiveBasis, effectiveUnit, pointValue(symbol), spot),
    [rows, effectiveBasis, effectiveUnit, symbol, spot],
  )
  const contractsResult = useMemo(
    () => (windowed ? computePcr(rows, 'vol', 'contracts', pointValue(symbol), spot) : null),
    [windowed, rows, symbol, spot],
  )
  const topStrikes = useMemo(
    () => (windowed ? topPremiumStrikes(rows, pointValue(symbol)) : []),
    [windowed, rows, symbol],
  )
  if (rows.length === 0) return null

  const format = effectiveUnit === 'contracts' ? formatAmount : formatMoney
  const total = result.put + result.call
  const putShare = total > 0 ? result.put / total : 0.5
  const unreliable = result.missingShare > PCR_MISSING_LIMIT
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
      </div>
      <div className="pcr-bar" aria-hidden="true">
        <span className="pcr-bar-put" style={{ width: `${(putShare * 100).toFixed(1)}%` }} />
      </div>
      <div className="pcr-values">
        <span className="pcr-put">PUT {format(result.put)}</span>
        <span className="pcr-ratio" data-testid="pcr-ratio">
          P/C {result.ratio === null ? '—' : result.ratio.toFixed(2)}
        </span>
        <span className="pcr-call">CALL {format(result.call)}</span>
      </div>
      {/* Sekundární kusový poměr okna (#486) — premium hlavní, kusy vedle */}
      {windowed && contractsResult && (
        <div className="muted pcr-window-contracts" data-testid="pcr-window-contracts">
          kusově PUT {formatAmount(contractsResult.put)} · P/C{' '}
          {contractsResult.ratio === null ? '—' : contractsResult.ratio.toFixed(2)} · CALL{' '}
          {formatAmount(contractsResult.call)}
        </div>
      )}
      {/* Popiska původu čísla — metrika · základ · expirace (#469) */}
      <div className="muted pcr-caption">
        {windowed
          ? `Prémie $ (≈ objem okna × mid k t2) · jen Vol`
          : `${PCR_UNIT_LABELS[unit]} · ${PCR_BASIS_LABELS[basis]}`}
        {expiry ? ` · exp ${expiry}` : ''}
      </div>
    </div>
  )
}
