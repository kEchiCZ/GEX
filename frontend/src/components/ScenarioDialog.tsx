/** Dialog „Uložit scénář" (#1173): cíle z geometrie poslední anotace (upravitelné),
termín (dnes nebo budoucí datum), poznámka; snímek grafu se pořídí při potvrzení.
Jen dopředu: rodič dialog nabízí pouze na živém dni. */
import { useState } from 'react'
import type { ScenarioPathPoint } from '../api/scenarios'

export interface ScenarioDraft {
  targets: number[]
  deadline: string
  note: string
}

export function ScenarioDialog({
  symbol,
  entry,
  path,
  suggestedTargets,
  today,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  symbol: string
  entry: number
  path: ScenarioPathPoint[]
  suggestedTargets: number[]
  today: string
  busy: boolean
  error: string | null
  onConfirm: (draft: ScenarioDraft) => void
  onCancel: () => void
}) {
  const [targetsText, setTargetsText] = useState(suggestedTargets.map((t) => String(t)).join(', '))
  const [deadline, setDeadline] = useState(today)
  const [note, setNote] = useState('')
  const parsedTargets = targetsText
    .split(/[,;\s]+/)
    .map((item) => Number(item.replace(',', '.')))
    .filter((value) => Number.isFinite(value) && value > 0)
    .slice(0, 3)
  const valid = parsedTargets.length > 0 && deadline >= today
  const last = path[path.length - 1]
  return (
    <div className="scenario-dialog" role="dialog" aria-label="Uložit scénář">
      <h3>Scénář dne · {symbol}</h3>
      <p className="muted">
        Vstup {entry.toFixed(2)} · cesta {path.length} bodů
        {last
          ? ` · konec ${new Date(last.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
          : ''}
        . Uloží se snímek grafu s anotací; po termínu engine vyhodnotí zásah cílů v pořadí a
        odchylku od cesty. Scénář nejde založit do minulosti.
      </p>
      <label>
        Cíle v pořadí (max 3)
        <input
          type="text"
          value={targetsText}
          aria-label="Cíle scénáře"
          onChange={(event) => setTargetsText(event.target.value)}
          placeholder="7680, 7600"
        />
      </label>
      <label>
        Termín (settle dne)
        <input
          type="date"
          value={deadline}
          min={today}
          aria-label="Termín scénáře"
          onChange={(event) => setDeadline(event.target.value)}
        />
      </label>
      <label>
        Poznámka
        <input
          type="text"
          value={note}
          maxLength={1000}
          aria-label="Poznámka scénáře"
          onChange={(event) => setNote(event.target.value)}
          placeholder="proč to čekám…"
        />
      </label>
      {error && <p className="journal-error">{error}</p>}
      <div className="journal-form-row">
        <button
          type="button"
          className="chip active"
          disabled={!valid || busy}
          onClick={() => onConfirm({ targets: parsedTargets, deadline, note: note.trim() })}
        >
          {busy ? 'Ukládám…' : 'Uložit scénář'}
        </button>
        <button type="button" className="chip" disabled={busy} onClick={onCancel}>
          Zrušit
        </button>
      </div>
    </div>
  )
}
