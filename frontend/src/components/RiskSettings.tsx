/** Settings → Risk management (#1185): serverové parametry sizingu, brzd a
brány šablon. Ukládá se jako nová verze parametrů setupů (append-only, povinný
důvod) — engine přepne do sekund; existující setupy si nesou hodnoty vzniku. */
import { useEffect, useState } from 'react'
import { fetchSetupParams, saveSetupParams } from '../api/setups'
import type { SetupParamsVersion } from '../api/setups'

interface RiskField {
  key: string
  label: string
  step: number
  min: number
  max: number
  help: string
}

const RISK_FIELDS: readonly RiskField[] = [
  {
    key: 'account_equity_usd',
    label: 'Účet (USD, jednotky aplikace)',
    step: 1000,
    min: 0,
    max: 100_000_000,
    help: 'Aplikace počítá v plných kontraktech ES/NQ. Obchoduješ MES/MNQ (1/10) → reálných 5 000 $ = 50 000 $ zde; 1 kontrakt zde = 1 mikro.',
  },
  {
    key: 'risk_pct',
    label: 'Riziko na setup (%)',
    step: 0.25,
    min: 0,
    max: 100,
    help: '1 % = 500 $ zde (50 $ reálně) → 1 kontrakt při stopu ≤ 10 b ES / ≤ 25 b NQ. Delší stop = stín. 2 % až po ≥ 50 živých obchodech s edge ≥ +0,2 R.',
  },
  {
    key: 'risk_max_pct',
    label: 'Tvrdý strop ztráty (%)',
    step: 0.25,
    min: 0,
    max: 100,
    help: 'Ztráta jednoho setupu nikdy nad tento podíl účtu, i kdyby se riziko zvedlo.',
  },
  {
    key: 'fee_per_contract_usd',
    label: 'Poplatek za kontrakt a obchod (USD)',
    step: 0.5,
    min: 0,
    max: 1000,
    help: 'Round-trip v jednotkách aplikace (1 $ reálně na mikro × 10 = 10 $).',
  },
  {
    key: 'daily_brake_r',
    label: 'Denní brzda (R)',
    step: 0.5,
    min: 0,
    max: 100,
    help: 'Po této realizované ztrátě za seanci (obchodovatelné setupy, všechny symboly) nové setupy jen stínově do settle. 0 = vypnuto.',
  },
  {
    key: 'weekly_brake_r',
    label: 'Týdenní brzda (R)',
    step: 0.5,
    min: 0,
    max: 100,
    help: 'Totéž za obchodní týden (od pondělní seance). 0 = vypnuto.',
  },
  {
    key: 'max_template_stops_per_day',
    label: 'Max stopů šablony za den',
    step: 1,
    min: 0,
    max: 100,
    help: 'Po N stopech téže šablony za seanci je šablona do settle stínová. 0 = vypnuto.',
  },
  {
    key: 'template_gate_min_samples',
    label: 'Brána: minimální vzorek',
    step: 1,
    min: 1,
    max: 10_000,
    help: 'Šablona je obchodovatelná jen s kladnou dolní mezí očekávání (jednostranný 95% interval Ø R) při n ≥ tomuto počtu.',
  },
  {
    key: 'template_gate_days',
    label: 'Brána: okno (seancí)',
    step: 5,
    min: 1,
    max: 1000,
    help: 'Kolik posledních seancí track recordu brána hodnotí.',
  },
]

function numberOf(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function draftFrom(source: Record<string, unknown>, defaults: Record<string, unknown>): Draft {
  const values: Record<string, number> = {}
  for (const field of RISK_FIELDS) {
    values[field.key] = numberOf(source[field.key], numberOf(defaults[field.key], 0))
  }
  return { values, gate: source.template_gate_enabled !== false }
}

interface Draft {
  values: Record<string, number>
  gate: boolean
}

export function RiskSettings() {
  // undefined = načítá se; null = server bez verze (defaulty)
  const [current, setCurrent] = useState<SetupParamsVersion | null | undefined>(undefined)
  const [draft, setDraft] = useState<Draft>({ values: {}, gate: true })
  const [note, setNote] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    void fetchSetupParams().then((result) => {
      if (cancelled) return
      const defaults = result?.defaults ?? {}
      setCurrent(result?.current ?? null)
      setDraft(draftFrom(result?.current?.params ?? defaults, defaults))
    })
    return () => {
      cancelled = true
    }
  }, [])

  const save = async () => {
    if (current === undefined) return
    setSaving(true)
    const params = {
      ...(current?.params ?? {}),
      ...draft.values,
      template_gate_enabled: draft.gate,
    }
    const result = await saveSetupParams(params, note.trim() || 'risk: změna ze Settings')
    setSaving(false)
    if (result.ok) {
      setMessage(`Uloženo jako verze ${result.version} — engine přepne do sekund.`)
      const refreshed = await fetchSetupParams()
      setCurrent(refreshed?.current ?? null)
      setNote('')
    } else {
      setMessage(`Uložení selhalo: ${result.error}`)
    }
  }

  return (
    <section aria-label="Risk management">
      <h2>Risk management (#1185)</h2>
      <p className="muted">
        {current === undefined
          ? 'Načítám parametry…'
          : current === null
            ? 'Server zatím nemá verzi parametrů (engine ji založí při startu) — zobrazeny defaulty.'
            : `Platná verze ${current.version} (${current.created_by}): ${current.note}`}
      </p>
      <div className="risk-settings-grid">
        {RISK_FIELDS.map((field) => (
          <label key={field.key} title={field.help}>
            {field.label}
            <input
              type="number"
              min={field.min}
              max={field.max}
              step={field.step}
              value={draft.values[field.key] ?? ''}
              aria-label={field.label}
              onChange={(event) =>
                setDraft((prev) => ({
                  ...prev,
                  values: { ...prev.values, [field.key]: Number(event.target.value) || 0 },
                }))
              }
            />
          </label>
        ))}
        <label title="Vypnutá brána: obchodovatelné jsou všechny šablony se stopem v rozpočtu (brzdy platí dál).">
          <input
            type="checkbox"
            checked={draft.gate}
            aria-label="Brána šablon zapnuta"
            onChange={(event) => setDraft((prev) => ({ ...prev, gate: event.target.checked }))}
          />
          Brána šablon (obchodovat jen šablony s prokázaným edge)
        </label>
      </div>
      <label>
        Důvod změny
        <input
          value={note}
          maxLength={500}
          placeholder="proč se parametry mění (audit)"
          aria-label="Důvod změny risk parametrů"
          onChange={(event) => setNote(event.target.value)}
        />
      </label>
      <button
        type="button"
        className="chip"
        disabled={saving || current === undefined}
        onClick={() => void save()}
      >
        {saving ? 'Ukládám…' : 'Uložit jako novou verzi'}
      </button>
      {message && <p className="muted">{message}</p>}
      <p className="muted setting-help">
        Setup se stopem nad rozpočtem, po brzdě nebo z šablony bez prokázaného edge vzniká dál jako
        <b> stín</b>: šedý v Setupech, bez pushe, mimo bilanci účtu. Ostatní prahy šablon se mění
        skriptem (POST /setups/params).
      </p>
    </section>
  )
}
