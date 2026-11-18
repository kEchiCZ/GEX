/** Settings → Plán opce QQQ — účet (F5a #1746, ADR-0062 K2 A, Rozhodnuto 18. 11. F5a Q3 A):
dva klíče `settings` bez výchozí hodnoty. Prázdné pole = nenastaveno (`null`), plán pak dá
variantám „neobchodovat — rozpočet nenastaven“. Platná hodnota je kladné konečné číslo jako na
serveru (`compute/option_plan.account_value`), server ji kontroluje znovu a jeho chyba (422) se
ukáže. Hodnoty se nikam nelogují, velikost účtu je soukromá. */
import { useState } from 'react'
import {
  OPTION_PLAN_ACCOUNT_KEY,
  OPTION_PLAN_MAX_LOSS_KEY,
  accountValueSet,
} from '../api/optionPlans'
import type { ServerSettings } from '../api/settings'

interface AccountField {
  key: string
  label: string
  help: string
}

const FIELDS: readonly AccountField[] = [
  {
    key: OPTION_PLAN_ACCOUNT_KEY,
    label: 'Velikost účtu ($)',
    help: [
      'Velikost opčního účtu v $:',
      '• bez výchozí hodnoty, prázdné = nenastaveno',
      '• plán ji chce spolu s rozpočtem, jinak „neobchodovat — rozpočet nenastaven“',
      '• „% účtu“ zatím nikde nevzniká (nález #1842)',
    ].join('\n'),
  },
  {
    key: OPTION_PLAN_MAX_LOSS_KEY,
    label: 'Rozpočet na obchod — max. čerpání P4 ($ na kontrakt)',
    help: [
      'Rozpočet P4 na 1 kontrakt v $:',
      '• čerpání P4 = ztráta opce na SL + 0,2 EM v čase exitu',
      '• varianta s větším čerpáním je „nad rozpočtem“',
      '• obě varianty nad rozpočtem = neobchodovat',
    ].join('\n'),
  },
]

/** Text pole → hodnota pro server: `null` = nenastaveno, `undefined` = neplatné. */
export function parseAccountInput(text: string): number | null | undefined {
  const trimmed = text.trim().replace(',', '.')
  if (trimmed === '') return null
  const value = Number(trimmed)
  return Number.isFinite(value) && value > 0 ? value : undefined
}

function draftOf(value: unknown): string {
  return accountValueSet(value) ? String(value) : ''
}

export function OptionPlanAccountSettings({
  values,
  saveAll,
}: {
  values: ServerSettings
  saveAll: (entries: [string, unknown][]) => Promise<void>
}) {
  // null = pole ukazují hodnoty ze serveru (načítají se asynchronně), jinak rozepsaná úprava
  const [draft, setDraft] = useState<Record<string, string> | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const shown = draft ?? Object.fromEntries(FIELDS.map((f) => [f.key, draftOf(values[f.key])]))
  const parsed = FIELDS.map((field) => parseAccountInput(shown[field.key] ?? ''))
  const valid = parsed.every((value) => value !== undefined)

  const save = async () => {
    if (!valid) return
    setSaving(true)
    try {
      await saveAll(FIELDS.map((field, index) => [field.key, parsed[index]]))
      setDraft(null)
      setMessage('Uloženo. Plán čte hodnoty v 9:15 ET, živé upozornění a vyhodnocení aktuální.')
    } catch (err) {
      setMessage(`Uložení selhalo: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section aria-label="Plán opce QQQ — účet">
      <h2>Plán opce QQQ — účet (#1746)</h2>
      <p className="muted">
        Parametry účtu pro plán opce QQQ ze scénáře dne (ADR-0062). Bez výchozí hodnoty: prázdné
        pole = nenastaveno a plán pak doporučí „neobchodovat — rozpočet nenastaven“.
      </p>
      <div className="risk-settings-grid">
        {FIELDS.map((field, index) => {
          const value = parsed[index]
          return (
            <label key={field.key} title={field.help}>
              {field.label}
              <input
                type="text"
                inputMode="decimal"
                value={shown[field.key] ?? ''}
                aria-label={field.label}
                aria-invalid={value === undefined}
                onChange={(event) => {
                  setDraft({ ...shown, [field.key]: event.target.value })
                  setMessage(null)
                }}
              />
              {value === undefined ? (
                <span className="option-plan-error" data-testid={`account-invalid-${field.key}`}>
                  kladné číslo v $, nebo prázdné = nenastaveno
                </span>
              ) : value === null ? (
                <span className="option-plan-warning" data-testid={`account-unset-${field.key}`}>
                  nenastaveno
                </span>
              ) : null}
            </label>
          )
        })}
      </div>
      <button
        type="button"
        className="chip"
        disabled={saving || !valid || draft === null}
        onClick={() => void save()}
      >
        {saving ? 'Ukládám…' : 'Uložit účet plánu'}
      </button>
      {message && <p className="muted">{message}</p>}
    </section>
  )
}
