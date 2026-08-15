/** Obrazovka Deník (#673 fáze A, #709 rev. 2): retrospektiva tradera.

Timeline záznamů s filtry + formulář nového záznamu. Denní pár = tlačítka
Ranní plán / Večerní vyhodnocení (typ retro_dne s tagem). Export do Markdownu.

Profil (SMB / Futures) řídí, která pole formulář ukazuje, a ukládá se
U ZÁZNAMU — historické zápisy si drží profil, pod kterým vznikly.
*/
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  JOURNAL_PROFILE_LABELS,
  JOURNAL_TYPE_LABELS,
  createJournalEntry,
  defaultProfile,
  deleteJournalEntry,
  fetchJournal,
  fetchJournalMeta,
  journalToMarkdown,
  mistakeLabel,
  plannedRR,
  realizedR,
  updateJournalEntry,
} from '../api/journal'
import { fetchPlaybook } from '../api/journal'
import type {
  JournalEntry,
  JournalMeta,
  JournalProfile,
  JournalType,
  PlaybookItem,
} from '../api/journal'
import { fetchSetups, templateLabel } from '../api/setups'
import type { SetupRow } from '../api/setups'
import { useAppState } from '../state/AppState'

/** Okno, ve kterém se detekovaný setup ještě považuje za „k této minutě". */
const SETUP_MATCH_MS = 15 * 60 * 1000
import { CONTEXT_LABELS, loadJournalContext } from '../journal/context'
import {
  EMPTY_PLAN,
  cleanScenarios,
  emptyReview,
  isPlanLocked,
  planToText,
  previousGoal,
  reviewToText,
} from '../journal/daily'
import type { DailyPlan, DailyReview } from '../journal/daily'
import { daySegments } from '../journal/segments'
import { EMPTY_TRADE, draftToTrade, tradeToDraft } from '../journal/trade'
import type { TradeDraft } from '../journal/trade'
import { JournalPlanFields, JournalReviewFields } from './JournalDaily'
import { JournalPlaybook } from './JournalPlaybook'
import { JournalTradeFields } from './JournalTradeFields'

const PROFILES: JournalProfile[] = ['smb', 'futures']

function toLocalInput(iso: string): string {
  const date = new Date(iso)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** Shrnutí obchodu do řádku karty — R se počítá, neukládá. */
function TradeSummary({ entry }: { entry: JournalEntry }) {
  const trade = entry.trade
  if (!trade) return null
  const rr = plannedRR(trade)
  const r = realizedR(trade)
  const parts: string[] = [trade.direction === 'long' ? 'Long' : 'Short']
  if (trade.actual_entry !== null) parts.push(`vstup ${trade.actual_entry}`)
  if (trade.actual_exit !== null) parts.push(`výstup ${trade.actual_exit}`)
  if (rr !== null) parts.push(`plán R:R ${rr.toFixed(2)}`)
  if (r !== null) parts.push(`výsledek ${r >= 0 ? '+' : ''}${r.toFixed(2)}R`)
  if (trade.net_pnl !== null) parts.push(`net ${trade.net_pnl}`)
  return (
    <p className="journal-trade-summary muted">
      {parts.join(' · ')}
      {trade.setup_grade && <span className="chip">setup {trade.setup_grade}</span>}
      {trade.execution_grade && <span className="chip">exekuce {trade.execution_grade}</span>}
      {trade.mistake_tags.map((tag) => (
        <span key={tag} className="chip journal-mistake">
          {mistakeLabel(tag)}
        </span>
      ))}
    </p>
  )
}

/** Snímek kontextu v čitelné podobě — surové JSON uživateli nic neřekne. */
function ContextSummary({ context }: { context: Record<string, unknown> | null }) {
  const [open, setOpen] = useState(false)
  if (context === null) {
    return <p className="journal-context muted">Kontext se nezachytil (starší záznam nebo výpadek dat).</p> // prettier-ignore
  }
  const show = (key: string) => {
    const value = context[key]
    // Chybějící hodnota se ukáže jako „—", nedosazuje se nula ani se
    // pole tiše nevynechá — jinak by výpadek vypadal jako měření.
    const text =
      value === null || value === undefined
        ? '—'
        : typeof value === 'number'
          ? String(Number(value.toFixed(2)))
          : String(value)
    return (
      <span key={key} className="journal-context-item">
        <span className="muted">{CONTEXT_LABELS[key] ?? key}:</span> {text}
      </span>
    )
  }
  const headline = ['regime', 'flip', 'spot', 'dist_to_flip']
  const rest = [
    'call_wall',
    'put_wall',
    'centroid',
    'total_gex',
    'cliff_share',
    'tendency_band',
    // Futures vrstva (#713) — u profilu smb tato pole zůstanou prázdná
    'session_segment',
    'vol_bucket',
    'macro_event',
    'contract',
    'roll_week',
  ]
  return (
    <div className="journal-context">
      <button className="chip" onClick={() => setOpen((value) => !value)} aria-label="Kontext">
        {open ? '▾ Kontext' : '▸ Kontext'}
      </button>
      {headline.map(show)}
      {open && <div className="journal-context-rest">{rest.map(show)}</div>}
    </div>
  )
}

function EntryCard({
  entry,
  onChanged,
  mistakeTags,
  playbook,
}: {
  entry: JournalEntry
  onChanged: () => void
  mistakeTags: string[]
  playbook: PlaybookItem[]
}) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(entry.text)
  const [trade, setTrade] = useState<TradeDraft | null>(
    entry.trade ? tradeToDraft(entry.trade) : null,
  )
  const [error, setError] = useState('')

  const save = async () => {
    const ok = await updateJournalEntry(entry.id, {
      text,
      ...(trade ? { trade: draftToTrade(trade) } : {}),
    })
    if (ok) {
      setEditing(false)
      setError('')
      onChanged()
    } else {
      // Tiché selhání by znamenalo ztracený zápis bez vysvětlení
      setError('Uložení se nepovedlo — zkontroluj hodnoty a spojení.')
    }
  }
  const remove = async () => {
    if (!window.confirm('Smazat záznam deníku?')) return
    if (await deleteJournalEntry(entry.id)) onChanged()
    else setError('Smazání se nepovedlo.')
  }

  return (
    <article className="journal-entry" aria-label={`Záznam ${entry.id}`}>
      <header className="muted">
        {new Date(entry.ts_ref).toLocaleString()} · <strong>{entry.symbol}</strong> ·{' '}
        {JOURNAL_TYPE_LABELS[entry.entry_type]}
        <span className="chip journal-profile">{JOURNAL_PROFILE_LABELS[entry.profile]}</span>
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
          {trade && (
            <JournalTradeFields
              draft={trade}
              onChange={setTrade}
              mistakeTags={mistakeTags}
              playbook={playbook}
            />
          )}
          <button className="chip" onClick={() => void save()} disabled={text.trim() === ''}>
            Uložit
          </button>
        </>
      ) : (
        <>
          <p className="journal-text">{entry.text}</p>
          <TradeSummary entry={entry} />
          <ContextSummary context={entry.context} />
        </>
      )}
      {error !== '' && <p className="journal-error">{error}</p>}
    </article>
  )
}

export function JournalView() {
  const { symbol, selectedExpiry, journalDraft, setJournalDraft } = useAppState()
  const [entries, setEntries] = useState<JournalEntry[]>([])
  const [meta, setMeta] = useState<JournalMeta | null>(null)
  const [filterSymbol, setFilterSymbol] = useState<string>('')
  const [filterType, setFilterType] = useState<string>('')
  const [filterProfile, setFilterProfile] = useState<string>('')
  const [filterDate, setFilterDate] = useState<string>('')
  // Formulář nového záznamu; draft z rychlého vstupu (tlačítko ✎ u Replay)
  // předvyplní okamžik a symbol
  const [formType, setFormType] = useState<JournalType>('pozorovani')
  const [formTs, setFormTs] = useState(() => toLocalInput(new Date().toISOString()))
  const [formText, setFormText] = useState('')
  const [formTags, setFormTags] = useState('')
  const [formError, setFormError] = useState('')
  const [trade, setTrade] = useState<TradeDraft>(EMPTY_TRADE)
  const [playbook, setPlaybook] = useState<PlaybookItem[]>([])
  const [setups, setSetups] = useState<SetupRow[]>([])
  const [showPlaybook, setShowPlaybook] = useState(false)
  const [plan, setPlan] = useState<DailyPlan>(EMPTY_PLAN)
  const [review, setReview] = useState<DailyReview | null>(null)
  // null = uživatel profil neměnil, drží se odvození ze symbolu
  const [profileOverride, setProfileOverride] = useState<JournalProfile | null>(null)

  const profile = profileOverride ?? defaultProfile(symbol)
  const mistakeTags = useMemo(() => meta?.mistake_tags ?? [], [meta])
  // Setupy nabízené playbookem: profil filtruje, `both` platí vždy
  const profilePlaybook = useMemo(
    () => playbook.filter((item) => item.profile === 'both' || item.profile === profile),
    [playbook, profile],
  )
  // Detektor v okolí zapisované minuty (#710) — nabídne předvyplnění plánu
  const nearbySetup = useMemo(() => {
    const target = new Date(formTs).getTime()
    if (!Number.isFinite(target)) return null
    let best: SetupRow | null = null
    let bestDelta = SETUP_MATCH_MS
    for (const row of setups) {
      const delta = Math.abs(new Date(row.created_ts).getTime() - target)
      if (delta <= bestDelta) {
        best = row
        bestDelta = delta
      }
    }
    return best
  }, [setups, formTs])
  const symbolOptions = useMemo(() => {
    const known = new Set(meta?.symbols ?? [])
    known.add(symbol)
    return [...known].sort()
  }, [meta, symbol])

  const reload = useCallback(() => {
    void fetchJournal({
      symbol: filterSymbol || undefined,
      date: filterDate || undefined,
      entryType: (filterType || undefined) as JournalType | undefined,
      profile: (filterProfile || undefined) as JournalProfile | undefined,
    }).then(setEntries)
  }, [filterSymbol, filterDate, filterType, filterProfile])

  useEffect(reload, [reload])

  // Číselníky drží server, ať se výčty nerozejdou s validací
  useEffect(() => {
    void fetchJournalMeta().then(setMeta)
  }, [entries.length])

  useEffect(() => {
    void fetchPlaybook().then(setPlaybook)
  }, [])

  useEffect(() => {
    void fetchSetups(symbol).then(setSetups)
  }, [symbol])

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

  const submit = async (
    typeOverride?: JournalType,
    extraTag?: string,
    daily?: Record<string, unknown>,
    textOverride?: string,
  ) => {
    const bodyText = (textOverride ?? formText).trim()
    if (bodyText === '') return
    const entryType = typeOverride ?? formType
    if (entryType === 'obchod' && trade.setupKey === '') {
      setFormError('Vyber setup z playbooku — bez něj nejde obchody porovnávat.')
      return
    }
    const tags = formTags
      .split(',')
      .map((tag) => tag.trim().replace(/^#/, ''))
      .filter(Boolean)
    if (extraTag && !tags.includes(extraTag)) tags.push(extraTag)
    const tsIso = new Date(formTs).toISOString()
    // Kontext se skládá při zápisu — poziční mapa se během dne mění a zpětně
    // by ji nešlo rekonstruovat (retence 90 dní, verzovaná mechanika).
    const context = await loadJournalContext({
      symbol,
      expiry: selectedExpiry,
      tsRef: tsIso,
      profile,
    })
    const created = await createJournalEntry({
      ts_ref: tsIso,
      symbol,
      entry_type: entryType,
      text: bodyText,
      tags,
      profile,
      ...(entryType === 'obchod' ? { trade: draftToTrade(trade) } : {}),
      // Vazba na detekovaný setup — sloupec existoval od fáze A a nikdy se
      // neplnil; bez něj nejde odpovědět „vzal jsem ho, nebo přeskočil?" (#627)
      ...(nearbySetup ? { setup_id: nearbySetup.id } : {}),
      context: context as unknown as Record<string, unknown>,
      ...(daily ? { daily } : {}),
    })
    if (created) {
      setFormText('')
      setFormTags('')
      setTrade(EMPTY_TRADE)
      setFormError('')
      reload()
    } else {
      setFormError('Záznam se nepodařilo uložit — zkontroluj hodnoty a spojení se serverem.')
    }
  }

  // ── Denní rituál (#712) ──────────────────────────────────────
  const formDayIso = formTs.slice(0, 10)
  const segmentLabels = useMemo(() => {
    const labels: Record<string, string> = {}
    for (const segment of daySegments(profile, formDayIso)) labels[segment.key] = segment.label
    return labels
  }, [profile, formDayIso])

  // Cíl z posledního vyhodnocení PŘED dneškem — bez navázání je to jen
  // seznam přání (Steenbarger)
  useEffect(() => {
    setPlan((current) =>
      isPlanLocked(current)
        ? current
        : { ...current, prev_goal: previousGoal(entries, formDayIso) },
    )
  }, [entries, formDayIso])

  // Report card se zakládá až na vyžádání, ať prázdný formulář nekřičí
  useEffect(() => {
    if (formType !== 'retro_dne') return
    setReview((current) =>
      current === null ? emptyReview(daySegments(profile, formDayIso).map((s) => s.key)) : current,
    )
  }, [formType, profile, formDayIso])

  // Struktura je nabídka, ne povinnost — rychlý textový plán musí dál fungovat,
  // jinak se deník přestane používat (Jigsaw: over-dokumentace ho zabije).
  const planReady =
    formText.trim() !== '' ||
    cleanScenarios(plan.scenarios).length > 0 ||
    plan.process_goal.trim() !== ''
  const reviewReady =
    formText.trim() !== '' ||
    (review !== null &&
      (review.segments.some((segment) => segment.grade !== '' || segment.note.trim() !== '') ||
        review.lesson.trim() !== '' ||
        review.tomorrow_goal.trim() !== ''))

  const submitPlan = async () => {
    const locked: DailyPlan = {
      ...plan,
      scenarios: cleanScenarios(plan.scenarios),
      // Zamčení je nevratné — jinak by „plán" šel dopsat po faktu
      locked_ts: new Date().toISOString(),
    }
    const text = formText.trim() !== '' ? formText.trim() : planToText(locked)
    await submit('retro_dne', 'plan', { plan: locked }, text)
    setPlan({ ...EMPTY_PLAN, prev_goal: locked.prev_goal })
  }

  const submitReview = async () => {
    if (review === null) return
    const text = formText.trim() !== '' ? formText.trim() : reviewToText(review, segmentLabels)
    await submit('retro_dne', 'vyhodnoceni', { review }, text)
    setReview(emptyReview(daySegments(profile, formDayIso).map((segment) => segment.key)))
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
        <div className="journal-form-row" role="group" aria-label="Profil deníku">
          {PROFILES.map((value) => (
            <button
              key={value}
              type="button"
              className={profile === value ? 'chip active' : 'chip'}
              onClick={() => setProfileOverride(value)}
              aria-pressed={profile === value}
              title={
                value === 'futures'
                  ? 'Futures: seance, R v bodech, kontrakt — bez polí pro výběr akcie'
                  : 'SMB: obecný profil (katalyzátor, výběr instrumentu)'
              }
            >
              {JOURNAL_PROFILE_LABELS[value]}
            </button>
          ))}
        </div>
        <div className="journal-form-row">
          <select
            value={formType}
            onChange={(event) => setFormType(event.target.value as JournalType)}
            aria-label="Typ záznamu"
          >
            <option value="pozorovani">Pozorování</option>
            <option value="hypoteza">Hypotéza</option>
            <option value="retro_dne">Retrospektiva dne</option>
            <option value="obchod">Obchod</option>
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
        {nearbySetup && (
          <p className="journal-nearby muted">
            Detektor tu nabídl <strong>{templateLabel(nearbySetup.template)}</strong> (
            {nearbySetup.direction === 'long' ? 'long' : 'short'}, vstup {nearbySetup.entry}).
            {formType === 'obchod' && (
              <button
                type="button"
                className="chip"
                onClick={() =>
                  setTrade((current) => ({
                    ...current,
                    direction: nearbySetup.direction,
                    plannedEntry: String(nearbySetup.entry),
                    plannedStop: String(nearbySetup.stop),
                    plannedTarget: String(nearbySetup.target),
                  }))
                }
              >
                Převzít plán
              </button>
            )}
          </p>
        )}
        {formType === 'retro_dne' && (
          <>
            <JournalPlanFields plan={plan} onChange={setPlan} />
            {review !== null && (
              <JournalReviewFields
                review={review}
                onChange={setReview}
                profile={profile}
                dateIso={formDayIso}
              />
            )}
          </>
        )}
        {formType === 'obchod' && (
          <JournalTradeFields
            draft={trade}
            onChange={setTrade}
            mistakeTags={mistakeTags}
            playbook={profilePlaybook}
          />
        )}
        {formError !== '' && <p className="journal-error">{formError}</p>}
        <div className="journal-form-row">
          <button className="chip" onClick={() => void submit()} disabled={formText.trim() === ''}>
            Přidat záznam
          </button>
          {/* Denní pár: ranní plán / večerní vyhodnocení (#673, struktura #712) */}
          <button
            className="chip"
            onClick={() => void submitPlan()}
            disabled={!planReady}
            title="Uloží a ZAMKNE ranní plán — po zamčení se needituje"
          >
            ☀ Ranní plán
          </button>
          <button
            className="chip"
            onClick={() => void submitReview()}
            disabled={!reviewReady}
            title="Uloží Daily Report Card — známka za proces, ne za zisk"
          >
            ☾ Vyhodnocení dne
          </button>
        </div>
      </section>

      {showPlaybook && (
        <JournalPlaybook
          items={playbook}
          onChanged={() => void fetchPlaybook(true).then(setPlaybook)}
        />
      )}

      <section className="journal-list" aria-label="Záznamy">
        <div className="journal-form-row">
          <select
            value={filterSymbol}
            onChange={(event) => setFilterSymbol(event.target.value)}
            aria-label="Filtr symbolu"
          >
            <option value="">Všechny symboly</option>
            {symbolOptions.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
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
          <select
            value={filterProfile}
            onChange={(event) => setFilterProfile(event.target.value)}
            aria-label="Filtr profilu"
          >
            <option value="">Oba profily</option>
            {PROFILES.map((value) => (
              <option key={value} value={value}>
                {JOURNAL_PROFILE_LABELS[value]}
              </option>
            ))}
          </select>
          <input
            type="date"
            value={filterDate}
            onChange={(event) => setFilterDate(event.target.value)}
            aria-label="Filtr dne"
          />
          <button className="chip" onClick={exportMd} disabled={entries.length === 0}>
            ⬇ Export MD
          </button>
          <button
            className={showPlaybook ? 'chip active' : 'chip'}
            onClick={() => {
              // Při otevření dotáhnout i vyřazené, ať jdou vrátit zpět
              if (!showPlaybook) void fetchPlaybook(true).then(setPlaybook)
              else void fetchPlaybook().then(setPlaybook)
              setShowPlaybook((value) => !value)
            }}
            aria-pressed={showPlaybook}
          >
            PlayBook
          </button>
        </div>
        {entries.length === 0 ? (
          <p className="muted">
            Zatím žádné záznamy. Deník je zpětná vazba sám sobě — pozoruj, zapisuj hypotézy a večer
            je vyhodnoť. Rychlý vstup: tlačítko ✎ u Replay.
          </p>
        ) : (
          entries.map((entry) => (
            <EntryCard
              key={entry.id}
              entry={entry}
              onChanged={reload}
              mistakeTags={mistakeTags}
              playbook={playbook}
            />
          ))
        )}
      </section>
    </main>
  )
}
