/** PlayBook setupů (#710) — sekce v Deníku.

Jádro SMB metodiky: archiv pojmenovaných, opakovatelných setupů. Obchoduje se
jen to, co je v playbooku — proto je setup u obchodu povinný.

Vyřazení je `active=false`, NIKDY smazání: historické záznamy na setup
odkazují a musí zůstat čitelné.
*/
import { useState } from 'react'
import { createPlaybookItem, updatePlaybookItem } from '../api/journal'
import type { PlaybookItem } from '../api/journal'

function PlaybookCard({ item, onChanged }: { item: PlaybookItem; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [error, setError] = useState('')

  const toggleActive = async () => {
    if (await updatePlaybookItem(item.id, { active: !item.active })) onChanged()
    else setError('Změna se nepovedla.')
  }

  return (
    <article className={item.active ? 'playbook-card' : 'playbook-card inactive'}>
      <header>
        <button className="chip" onClick={() => setOpen((value) => !value)}>
          {open ? '▾' : '▸'}
        </button>
        <strong>{item.name}</strong>
        <span className="muted">{item.key}</span>
        <span className="chip">{item.profile}</span>
        <button className="chip" onClick={() => void toggleActive()}>
          {item.active ? 'Vyřadit' : 'Vrátit'}
        </button>
      </header>
      {open && (
        <dl className="playbook-detail">
          {item.thesis && (
            <>
              <dt>Proč to funguje</dt>
              <dd>{item.thesis}</dd>
            </>
          )}
          {item.entry_conditions && (
            <>
              <dt>Podmínky vstupu</dt>
              <dd>{item.entry_conditions}</dd>
            </>
          )}
          {item.invalidation && (
            <>
              <dt>Kdy setup neplatí</dt>
              <dd>{item.invalidation}</dd>
            </>
          )}
          {item.management && (
            <>
              <dt>Management</dt>
              <dd>{item.management}</dd>
            </>
          )}
        </dl>
      )}
      {error !== '' && <p className="journal-error">{error}</p>}
    </article>
  )
}

export function JournalPlaybook({
  items,
  onChanged,
}: {
  items: PlaybookItem[]
  onChanged: () => void
}) {
  const [adding, setAdding] = useState(false)
  const [key, setKey] = useState('')
  const [name, setName] = useState('')
  const [thesis, setThesis] = useState('')
  const [error, setError] = useState('')

  const create = async () => {
    const created = await createPlaybookItem({
      key: key.trim(),
      name: name.trim(),
      profile: 'futures',
      thesis: thesis.trim(),
    })
    if (created) {
      setKey('')
      setName('')
      setThesis('')
      setAdding(false)
      setError('')
      onChanged()
    } else {
      setError('Nepovedlo se založit — klíč musí být unikátní, malá písmena a podtržítka.')
    }
  }

  return (
    <section className="journal-playbook" aria-label="PlayBook">
      <div className="journal-form-row">
        <h3>PlayBook</h3>
        <button className="chip" onClick={() => setAdding((value) => !value)}>
          {adding ? 'Zrušit' : '+ Setup'}
        </button>
      </div>
      {adding && (
        <div className="journal-form-row">
          <input
            type="text"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            placeholder="klic_setupu"
            aria-label="Klíč setupu"
          />
          <input
            type="text"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Název"
            aria-label="Název setupu"
          />
          <input
            type="text"
            value={thesis}
            onChange={(event) => setThesis(event.target.value)}
            placeholder="Proč to funguje"
            aria-label="Teze setupu"
          />
          <button
            className="chip"
            onClick={() => void create()}
            disabled={key.trim() === '' || name.trim() === ''}
          >
            Uložit
          </button>
        </div>
      )}
      {error !== '' && <p className="journal-error">{error}</p>}
      {items.length === 0 ? (
        <p className="muted">
          PlayBook je prázdný. Bez pojmenovaného setupu nejde obchody porovnávat — založ první.
        </p>
      ) : (
        items.map((item) => <PlaybookCard key={item.id} item={item} onChanged={onChanged} />)
      )}
    </section>
  )
}
