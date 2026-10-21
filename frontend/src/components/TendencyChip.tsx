/** Souhrnný indikátor tendence v hlavičce (#350).

Pětipásmová škála Strong Short … Strong Long; klik → popover s rozpadem
hlasů složek (podmínka „žádná černá skříňka") a přiznaným badge
„nekalibrováno" — váhy zatím nejsou ověřené proti datům (#232).
Hlas s vahou 0 (tendence v5: poloha mezi zdmi, E-0.12 C) se dál počítá
a ukládá, v rozpadu je zašedlý — do skóre nevstupuje (ADR-0057 doplněk 1b).
Není to doporučení k obchodu: popisuje positioning a tok, ne co dělat.
*/
import { useState } from 'react'
import { BAND_LABELS, BAND_ORDER, VOTE_LABELS } from '../api/tendency'
import { useTendency } from '../hooks/useTendency'

/** Tooltip hlasu s vahou 0 — odrážky a `\n` (vzor `ivRankTooltip`). */
const ZERO_WEIGHT_TOOLTIP = [
  'Hlas s vahou 0 — do skóre tendence nevstupuje:',
  '• složka se dál počítá a ukládá, data zůstávají pro další revize vah',
  '• váhu 0 nastavila revize vah (tendence v5: poloha mezi zdmi, #1113)',
].join('\n')

export function TendencyChip() {
  const row = useTendency()
  const [open, setOpen] = useState(false)
  if (!row) return null
  const activeIndex = BAND_ORDER.indexOf(row.band as (typeof BAND_ORDER)[number])
  return (
    <div className="tendency-wrap">
      <button
        type="button"
        className={`tendency-chip tendency-${row.band}`}
        data-testid="tendency-chip"
        aria-label={`Tendence ceny: ${BAND_LABELS[row.band] ?? row.band}`}
        title="Souhrn positioningu a toku napříč ukazateli — ne doporučení k obchodu (klik → rozpad hlasů)"
        onClick={() => setOpen((value) => !value)}
      >
        <span className="tendency-scale" aria-hidden="true">
          {BAND_ORDER.map((band, index) => (
            <span
              key={band}
              className={`tendency-dot${index === activeIndex ? ` active tendency-${band}` : ''}`}
            />
          ))}
        </span>
        {BAND_LABELS[row.band] ?? row.band}
      </button>
      {open && (
        <div className="tendency-popover" role="dialog" aria-label="Rozpad hlasů tendence">
          <p className="muted tendency-meta">
            skóre {row.score.toFixed(2)} · váhy v{row.weights_version}{' '}
            <span
              className="tendency-uncalibrated"
              title="Váhy zatím nejsou kalibrované proti datům (#232) — indikátor je orientační"
            >
              nekalibrováno
            </span>
          </p>
          <ul className="tendency-votes">
            {row.votes.map((vote) => (
              <li
                key={vote.name}
                className={vote.weight === 0 ? 'tendency-vote-zero' : undefined}
                title={vote.weight === 0 ? ZERO_WEIGHT_TOOLTIP : undefined}
              >
                <span
                  className={
                    vote.vote > 0
                      ? 'tendency-vote positive'
                      : vote.vote < 0
                        ? 'tendency-vote negative'
                        : 'tendency-vote'
                  }
                >
                  {vote.vote > 0 ? '+' : ''}
                  {vote.vote.toFixed(1)}
                </span>
                <span className="tendency-vote-name">{VOTE_LABELS[vote.name] ?? vote.name}</span>
                <span className="muted tendency-vote-detail">
                  {vote.detail}
                  {vote.weight === 0 && ' · váha 0, do skóre nevstupuje'}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
