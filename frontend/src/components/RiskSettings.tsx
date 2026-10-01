/** Settings → Risk management (#1185): serverové parametry účtu — sizing,
brzdy, kritérium brány šablon a výchozí rozpočet zkoušky (#1323). Ukládá se jako
nová verze parametrů setupů (append-only, povinný důvod bez náhradního textu)
— engine přepne do sekund; existující setupy si nesou hodnoty vzniku.

Stádia buněk (Stín, Zkouška) se tady nemění — patří do Setupy → Knihovna
(`POST /setups/stage`); globální vypínač brány `template_gate_enabled` zůstal
jen jako nouzová cesta přes API (rozhodnutí 1. 10. 2026, #1323 bod 4B). */
import { useEffect, useState } from 'react'
import { NOTE_MAX, NOTE_MIN } from '../api/setupLibrary'
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
    help: 'Po této realizované ztrátě za seanci (obchodovatelné setupy, všechny symboly) nové setupy jen stínově do konce seance (17:00 CT). 0 = vypnuto.',
  },
  {
    key: 'weekly_brake_r',
    label: 'Týdenní brzda (R)',
    step: 0.5,
    min: 0,
    max: 100,
    help: 'Totéž za obchodní týden (od pondělní seance) — stínově do konce obchodního týdne (neděle 17:00 CT). 0 = vypnuto.',
  },
  {
    key: 'max_template_stops_per_day',
    label: 'Max stopů šablony za den',
    step: 1,
    min: 0,
    max: 100,
    help: 'Po N stopech téže šablony za seanci je šablona do konce seance (17:00 CT) stínová. 0 = vypnuto.',
  },
  {
    key: 'template_gate_min_samples',
    label: 'Brána: minimální vzorek',
    step: 1,
    min: 1,
    max: 10_000,
    help: 'Šablona je na daném symbolu obchodovatelná jen s kladnou dolní mezí očekávání (jednostranný 95% interval Ø R) při n ≥ tomuto počtu setupů téhož symbolu. ES a NQ se hodnotí zvlášť.',
  },
  {
    key: 'template_gate_days',
    label: 'Brána: okno (seancí)',
    step: 5,
    min: 1,
    max: 1000,
    help: 'Kolik posledních seancí track recordu brána hodnotí.',
  },
  {
    key: 'trial_budget_setups',
    label: 'Zkouška: výchozí počet setupů',
    step: 1,
    min: 1,
    max: 20,
    help: 'Předvyplní dialog Zkoušky v Setupy → Knihovna: zkouška skončí po tolika setupech s přebitou bránou (1–20). Běžící zkoušky si nesou rozpočet ze zahájení.',
  },
  {
    key: 'trial_budget_r',
    label: 'Zkouška: výchozí ztráta (R)',
    step: 0.5,
    min: 0.5,
    max: 6,
    help: 'Předvyplní dialog Zkoušky: zkouška skončí, když Σ R jejích uzavřených setupů klesne na −tuto hodnotu (0,5–6 R).',
  },
]

/** Stádia buněk mění jen `POST /setups/stage` — klíče se neposílají, server
 *  převezme platné (starý snímek by jinak vrátil 422). */
const STAGE_KEYS = new Set(['shadow_cells', 'trial_cells'])

function numberOf(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function draftFrom(
  source: Record<string, unknown>,
  defaults: Record<string, unknown>,
): Record<string, number> {
  const values: Record<string, number> = {}
  for (const field of RISK_FIELDS) {
    values[field.key] = numberOf(source[field.key], numberOf(defaults[field.key], 0))
  }
  return values
}

export function RiskSettings() {
  // undefined = načítá se; null = server bez verze (defaulty)
  const [current, setCurrent] = useState<SetupParamsVersion | null | undefined>(undefined)
  const [draft, setDraft] = useState<Record<string, number>>({})
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

  const trimmedNote = note.trim()
  const noteOk = trimmedNote.length >= NOTE_MIN

  const save = async () => {
    // Důvod je povinný (audit, #1323) — náhradní text se nevymýšlí
    if (current === undefined || !noteOk) return
    setSaving(true)
    const base = Object.fromEntries(
      Object.entries(current?.params ?? {}).filter(([key]) => !STAGE_KEYS.has(key)),
    )
    const result = await saveSetupParams({ ...base, ...draft }, trimmedNote)
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
              value={draft[field.key] ?? ''}
              aria-label={field.label}
              onChange={(event) =>
                setDraft((prev) => ({ ...prev, [field.key]: Number(event.target.value) || 0 }))
              }
            />
          </label>
        ))}
      </div>
      <label>
        Důvod změny*
        <input
          value={note}
          maxLength={NOTE_MAX}
          placeholder="proč se parametry mění (audit, povinné)"
          aria-label="Důvod změny risk parametrů"
          aria-invalid={!noteOk}
          onChange={(event) => setNote(event.target.value)}
        />
      </label>
      <button
        type="button"
        className="chip"
        disabled={saving || current === undefined || !noteOk}
        onClick={() => void save()}
      >
        {saving ? 'Ukládám…' : 'Uložit jako novou verzi'}
      </button>
      {!noteOk && (
        <p className="muted" data-testid="risk-note-required">
          Důvod je povinný (aspoň {NOTE_MIN} znaky) — bez něj se verze neuloží.
        </p>
      )}
      {message && <p className="muted">{message}</p>}
      <p className="muted setting-help">
        Setup se stopem nad rozpočtem, po brzdě nebo z šablony bez prokázaného edge vzniká dál jako
        <b> stín</b>: šedý v Setupech, bez pushe, mimo bilanci účtu. Stádium jednotlivé šablony na
        tickeru (Stín, Zkouška s rozpočtem) se nastavuje v <b>Setupy → Knihovna</b>. Ostatní prahy
        šablon se mění skriptem (POST /setups/params).
      </p>
    </section>
  )
}
