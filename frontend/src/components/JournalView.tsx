/** Obrazovka Deník (#673, fáze A): manuální retrospektiva tradera.

Timeline záznamů s filtry + formulář nového záznamu. Denní pár = tlačítka
Ranní plán / Večerní vyhodnocení (typ retro_dne s tagem). Export do Markdownu.
Fáze B (import exekucí) přidá typ `obchod` — ten se ručně zakládat nedá.
*/
import { useCallback, useEffect, useState } from 'react'
import {
  JOURNAL_TYPE_LABELS,
  createJournalEntry,
  deleteJournalEntry,
  fetchJournal,
  journalToMarkdown,
  updateJournalEntry,
} from '../api/journal'
import type { JournalEntry, JournalType } from '../api/journal'
import { useAppState } from '../state/AppState'

function toLocalInput(iso: string): string {
  const date = new Date(iso)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function EntryCard({ entry, onChanged }: { entry: JournalEntry; onChanged: () => void }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(entry.text)

  const save = async () => {
    if (await updateJournalEntry(entry.id, { text })) {
      setEditing(false)
      onChanged()
    }
  }
  const remove = async () => {
    if (window.confirm('Smazat záznam deníku?') && (await deleteJournalEntry(entry.id))) onChanged()
  }

  return (
    <article className="journal-entry" aria-label={`Záznam ${entry.id}`}>
      <header className="muted">
        {new Date(entry.ts_ref).toLocaleString()} · <strong>{entry.symbol}</strong> ·{' '}
        {JOURNAL_TYPE_LABELS[entry.entry_type]}
        {entry.tags.map((tag) => (
          <span key={tag} className="chip journal-tag">
            #{tag}
          </span>
        ))}
        <span className="journal-actions">
          <button className="chip" onClick={() => setEditing((value) => !value)}>
            {editing ? 'Zrušit' : 'Upravit'}
          </button>
          <button className="chip" onClick={() => void remove()}>
            Smazat
          </button>
        </span>
      </header>
      {editing ? (
        <>
          <textarea value={text} onChange={(event) => setText(event.target.value)} rows={4} />
          <button className="chip" onClick={() => void save()} disabled={text.trim() === ''}>
            Uložit
          </button>
        </>
      ) : (
        <p className="journal-text">{entry.text}</p>
      )}
    </article>
  )
}

export function JournalView() {
  const { symbol, journalDraft, setJournalDraft } = useAppState()
  const [entries, setEntries] = useState<JournalEntry[]>([])
  const [filterSymbol, setFilterSymbol] = useState<string>('')
  const [filterType, setFilterType] = useState<string>('')
  // Formulář nového záznamu; draft z rychlého vstupu (tlačítko ✎ u Replay)
  // předvyplní okamžik a symbol
  const [formType, setFormType] = useState<Exclude<JournalType, 'obchod'>>('pozorovani')
  const [formTs, setFormTs] = useState(() => toLocalInput(new Date().toISOString()))
  const [formText, setFormText] = useState('')
  const [formTags, setFormTags] = useState('')

  const reload = useCallback(() => {
    void fetchJournal({
      symbol: filterSymbol || undefined,
      entryType: (filterType || undefined) as JournalType | undefined,
    }).then(setEntries)
  }, [filterSymbol, filterType])

  useEffect(reload, [reload])

  useEffect(() => {
    if (journalDraft) {
      setFormTs(toLocalInput(journalDraft.tsRef))
      // Briefing (#674) předvyplní kostru ranního plánu
      if (journalDraft.text !== undefined) {
        setFormText(journalDraft.text)
        setFormType('retro_dne')
        setFormTags('plan')
      }
      setJournalDraft(null)
    }
  }, [journalDraft, setJournalDraft])

  const submit = async (typeOverride?: Exclude<JournalType, 'obchod'>, extraTag?: string) => {
    if (formText.trim() === '') return
    const tags = formTags
      .split(',')
      .map((tag) => tag.trim().replace(/^#/, ''))
      .filter(Boolean)
    if (extraTag && !tags.includes(extraTag)) tags.push(extraTag)
    const created = await createJournalEntry({
      ts_ref: new Date(formTs).toISOString(),
      symbol,
      entry_type: typeOverride ?? formType,
      text: formText.trim(),
      tags,
    })
    if (created) {
      setFormText('')
      setFormTags('')
      reload()
    }
  }

  const exportMd = () => {
    const blob = new Blob([journalToMarkdown(entries)], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `denik-${new Date().toISOString().slice(0, 10)}.md`
    link.click()
    URL.revokeObjectURL(url)
  }

  return (
    <main className="journal-view" aria-label="Deník">
      <section className="journal-form" aria-label="Nový záznam">
        <h3>Nový záznam · {symbol}</h3>
        <div className="journal-form-row">
          <select
            value={formType}
            onChange={(event) => setFormType(event.target.value as Exclude<JournalType, 'obchod'>)}
            aria-label="Typ záznamu"
          >
            <option value="pozorovani">Pozorování</option>
            <option value="hypoteza">Hypotéza</option>
            <option value="retro_dne">Retrospektiva dne</option>
          </select>
          <input
            type="datetime-local"
            value={formTs}
            onChange={(event) => setFormTs(event.target.value)}
            aria-label="Okamžik záznamu"
          />
          <input
            type="text"
            value={formTags}
            onChange={(event) => setFormTags(event.target.value)}
            placeholder="tagy čárkou (flip, fade…)"
            aria-label="Tagy"
          />
        </div>
        <textarea
          value={formText}
          onChange={(event) => setFormText(event.target.value)}
          placeholder="Co vidíš, co čekáš, co ses naučil…"
          rows={3}
          aria-label="Text záznamu"
        />
        <div className="journal-form-row">
          <button className="chip" onClick={() => void submit()} disabled={formText.trim() === ''}>
            Přidat záznam
          </button>
          {/* Denní pár: ranní plán / večerní vyhodnocení (#673) */}
          <button
            className="chip"
            onClick={() => void submit('retro_dne', 'plan')}
            disabled={formText.trim() === ''}
            title="Uloží text jako ranní plán dne (retro_dne #plan)"
          >
            ☀ Ranní plán
          </button>
          <button
            className="chip"
            onClick={() => void submit('retro_dne', 'vyhodnoceni')}
            disabled={formText.trim() === ''}
            title="Uloží text jako večerní vyhodnocení dne (retro_dne #vyhodnoceni)"
          >
            ☾ Vyhodnocení dne
          </button>
        </div>
      </section>

      <section className="journal-list" aria-label="Záznamy">
        <div className="journal-form-row">
          <select
            value={filterSymbol}
            onChange={(event) => setFilterSymbol(event.target.value)}
            aria-label="Filtr symbolu"
          >
            <option value="">Všechny symboly</option>
            <option value={symbol}>{symbol}</option>
          </select>
          <select
            value={filterType}
            onChange={(event) => setFilterType(event.target.value)}
            aria-label="Filtr typu"
          >
            <option value="">Všechny typy</option>
            {Object.entries(JOURNAL_TYPE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <button className="chip" onClick={exportMd} disabled={entries.length === 0}>
            ⬇ Export MD
          </button>
        </div>
        {entries.length === 0 ? (
          <p className="muted">
            Zatím žádné záznamy. Deník je zpětná vazba sám sobě — pozoruj, zapisuj hypotézy a večer
            je vyhodnoť. Rychlý vstup: tlačítko ✎ u Replay.
          </p>
        ) : (
          entries.map((entry) => <EntryCard key={entry.id} entry={entry} onChanged={reload} />)
        )}
      </section>
    </main>
  )
}
