/** Změna stádia buňky Knihovny setupů (#1323): Auto / Stín / Zkouška.

Důvod je povinný (čip nebo text) — bez něj se uložit nedá, náhradní text
neexistuje. Zkoušku zahajuje i obnovuje server s časem „teď" a rozpočtem od
nuly; klient posílá jen rozpočet. Sizing a brzdy nepřebije žádné stádium. */
import { useEffect, useId, useRef, useState } from 'react'
import {
  NOTE_MAX,
  NOTE_MIN,
  REASON_CHIPS,
  STAGE_EFFECTS,
  STAGE_HELP,
  STAGE_LABELS,
  TRIAL_BUDGET_R_RANGE,
  TRIAL_BUDGET_SETUPS_RANGE,
  TRIAL_END_SHORT,
  USER_STAGES,
  cellTitle,
  edgeUnproven,
  gateLabel,
  lbText,
  saveSetupStage,
  stageLabel,
  trialEnd,
  trialUsageText,
} from '../api/setupLibrary'
import type { LibraryBrakes, LibraryCell, LibraryParams, UserStage } from '../api/setupLibrary'

/** Celé číslo z pole formuláře; jinak null (prázdné, 2.5, text). */
function parseInteger(text: string): number | null {
  const value = Number(text.trim())
  return text.trim() !== '' && Number.isInteger(value) ? value : null
}

/** Desetinné číslo z pole formuláře (čárka i tečka); jinak null. */
function parseDecimal(text: string): number | null {
  const value = Number(text.trim().replace(',', '.'))
  return text.trim() !== '' && Number.isFinite(value) ? value : null
}

function formatDay(iso: string | null): string {
  if (iso === null) return ''
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' : `, ${date.getDate()}. ${date.getMonth() + 1}.`
}

function brakeLimit(value: number): string {
  return value > 0 ? `−${value.toFixed(1)} R` : 'vypnutá'
}

export function StageDialog({
  cell,
  params,
  brakes,
  onSaved,
  onCancel,
}: {
  cell: LibraryCell
  params: LibraryParams
  /** Limity brzd do upozornění; null = server je nedodal (text bez čísel). */
  brakes: LibraryBrakes | null
  onSaved: (version: number) => void
  onCancel: () => void
}) {
  const titleId = useId()
  const radioName = useId()
  const [stage, setStage] = useState<UserStage>(cell.stage)
  const [chips, setChips] = useState<string[]>([])
  const [text, setText] = useState('')
  const [budgetSetups, setBudgetSetups] = useState(String(params.trialBudgetSetups))
  const [budgetR, setBudgetR] = useState(String(params.trialBudgetR))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const currentRef = useRef<HTMLInputElement>(null)

  // Fokus na platné stádium — klávesnicí jde hned šipkami vybrat jiné
  useEffect(() => {
    currentRef.current?.focus()
  }, [])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  const note = [...chips, text.trim()].filter((part) => part !== '').join(' · ')
  const noteOk = note.length >= NOTE_MIN && note.length <= NOTE_MAX
  const unchanged = stage === cell.stage && stage !== 'trial'
  const renew = stage === 'trial' && cell.stage === 'trial'
  const trialEnded = trialEnd(cell)
  const setupsValue = parseInteger(budgetSetups)
  const rValue = parseDecimal(budgetR)
  const setupsOk =
    setupsValue !== null &&
    setupsValue >= TRIAL_BUDGET_SETUPS_RANGE.min &&
    setupsValue <= TRIAL_BUDGET_SETUPS_RANGE.max
  const rOk =
    rValue !== null && rValue >= TRIAL_BUDGET_R_RANGE.min && rValue <= TRIAL_BUDGET_R_RANGE.max
  const budgetOk = stage !== 'trial' || (setupsOk && rOk)
  const canSave = !saving && noteOk && !unchanged && budgetOk
  const nextVersion = params.version === null ? '' : ` → verze ${params.version + 1}`

  const toggleChip = (chip: string) =>
    setChips((previous) =>
      previous.includes(chip) ? previous.filter((item) => item !== chip) : [...previous, chip],
    )

  const save = async () => {
    if (!canSave) return
    setSaving(true)
    setError(null)
    const result = await saveSetupStage({
      cell: cell.cell,
      stage,
      note,
      ...(stage === 'trial' && setupsValue !== null && rValue !== null
        ? { budgetSetups: setupsValue, budgetR: rValue }
        : {}),
    })
    setSaving(false)
    if (result.ok) onSaved(result.version)
    else setError(result.error)
  }

  return (
    <div className="stage-backdrop" role="presentation">
      <div
        className="stage-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-testid="stage-dialog"
      >
        <header className="stage-dialog-head">
          <h3 id={titleId}>
            {cellTitle(cell)} · {cell.ticker}
          </h3>
          <span className="muted">
            platí: {stageLabel(cell)}
            {params.version !== null && ` (verze ${params.version}${formatDay(params.createdTs)})`}
          </span>
        </header>
        <fieldset className="stage-options">
          <legend className="muted">Stádium</legend>
          {USER_STAGES.map((option) => (
            <label key={option} className="stage-option">
              <input
                type="radio"
                name={radioName}
                value={option}
                checked={stage === option}
                ref={option === cell.stage ? currentRef : undefined}
                onChange={() => setStage(option)}
              />
              <span className="stage-option-name">{STAGE_LABELS[option]}</span>
              <span className="muted">
                {STAGE_HELP[option]}
                {option === 'auto' &&
                  ` · teď ${gateLabel(cell, params.gateMinSamples)} (${lbText(cell.gate_lb)}, n ${cell.gate_n})`}
              </span>
            </label>
          ))}
        </fieldset>
        {stage === 'trial' && (
          <div className="stage-trial" data-testid="stage-trial">
            <div className="stage-budget">
              <span>Rozpočet: konec po</span>
              <input
                type="number"
                inputMode="numeric"
                min={TRIAL_BUDGET_SETUPS_RANGE.min}
                max={TRIAL_BUDGET_SETUPS_RANGE.max}
                step={1}
                value={budgetSetups}
                aria-label="Rozpočet zkoušky — počet setupů"
                aria-invalid={!setupsOk}
                onChange={(event) => setBudgetSetups(event.target.value)}
              />
              <span>setupech nebo</span>
              <input
                type="number"
                inputMode="decimal"
                min={TRIAL_BUDGET_R_RANGE.min}
                max={TRIAL_BUDGET_R_RANGE.max}
                step={0.5}
                value={budgetR}
                aria-label="Rozpočet zkoušky — ztráta v R"
                aria-invalid={!rOk}
                onChange={(event) => setBudgetR(event.target.value)}
              />
              <span>R ztráty</span>
            </div>
            {!budgetOk && (
              <p className="stage-error" role="alert">
                Rozpočet: {TRIAL_BUDGET_SETUPS_RANGE.min}–{TRIAL_BUDGET_SETUPS_RANGE.max} setupů
                (celé číslo) a {TRIAL_BUDGET_R_RANGE.min}–{TRIAL_BUDGET_R_RANGE.max} R ztráty.
              </p>
            )}
            <p className="muted">
              Sizing a brzdy platí vždy · po vyčerpání se buňka sama vrátí na Auto (rozhoduje
              brána), verze se nezapisuje{renew && ' · obnovení začne čerpat od nuly'}
            </p>
            {edgeUnproven(cell) && (
              <p className="stage-warning" data-testid="edge-unproven">
                <strong>edge neprokázán</strong> —{' '}
                {cell.gate_lb === null
                  ? `vzorek n ${cell.gate_n} dolní mez ještě nedává`
                  : `${lbText(cell.gate_lb)} ≤ 0 při n ${cell.gate_n}`}
                : zkouška znamená obchody s reálnými penězi bez prokázané výhody.
              </p>
            )}
            {cell.gate_verdict === 'pass' && (
              <p className="muted">
                Brána teď prošla — dokud pouští, zkouška nic nepřebíjí a rozpočet nečerpá.
              </p>
            )}
            <p className="stage-warning" data-testid="brakes-shared">
              Brzdy jsou společné napříč symboly
              {brakes !== null &&
                ` (den ${brakeLimit(brakes.daily_brake_r)}, týden ${brakeLimit(brakes.weekly_brake_r)})`}
              : ztráty zkoušky můžou zastavit i ostatní obchodovatelné setupy na všech tickerech.
            </p>
            {cell.trial !== null && (
              <p className="muted" data-testid="stage-previous-trial">
                {trialEnded === null
                  ? 'Běžící zkouška'
                  : `Předchozí zkouška (${TRIAL_END_SHORT[trialEnded]})`}
                : {trialUsageText(cell.trial)}
              </p>
            )}
          </div>
        )}
        <div className="stage-reason">
          <span>Důvod*</span>
          {REASON_CHIPS.map((chip) => (
            <button
              key={chip}
              type="button"
              className={chips.includes(chip) ? 'chip active' : 'chip'}
              aria-pressed={chips.includes(chip)}
              onClick={() => toggleChip(chip)}
            >
              {chip}
            </button>
          ))}
          <input
            value={text}
            maxLength={NOTE_MAX}
            placeholder="vlastní důvod"
            aria-label="Důvod změny stádia"
            onChange={(event) => setText(event.target.value)}
          />
        </div>
        {!noteOk && (
          <p className="muted" data-testid="stage-note-required">
            Důvod je povinný — vyber čip nebo napiš aspoň {NOTE_MIN} znaky.
          </p>
        )}
        {unchanged && (
          <p className="muted">Buňka už je ve stádiu {STAGE_LABELS[stage]} — vyber jiné.</p>
        )}
        <p className="stage-effects">
          <span className="muted">Změní se:</span> {STAGE_EFFECTS[stage]}
        </p>
        {error !== null && (
          <p className="stage-error" role="alert">
            Uložení selhalo: {error}
          </p>
        )}
        <div className="stage-actions">
          <button type="button" className="chip" onClick={onCancel}>
            Zrušit
          </button>
          <button
            type="button"
            className="chip active"
            disabled={!canSave}
            onClick={() => void save()}
          >
            {saving ? 'Ukládám…' : `${renew ? 'Obnovit zkoušku' : 'Uložit'}${nextVersion}`}
          </button>
        </div>
      </div>
    </div>
  )
}
