/** Pole strukturovaného obchodu v deníku (#709).

Kvalita setupu a exekuce se známkuje ODDĚLENĚ od výsledku (SMB, Steenbarger)
— dobrý obchod a ziskový obchod jsou dvě různé věci, proto formulář u známek
žádné P/L neukazuje. Převody rozepsaných hodnot jsou v `journal/trade.ts`.
*/
import type { JournalGrade, JournalTrade, TradeDirection } from '../api/journal'
import { mistakeLabel, plannedRR } from '../api/journal'
import { draftToTrade } from '../journal/trade'
import type { TradeDraft } from '../journal/trade'

const GRADES: JournalGrade[] = ['A', 'B', 'C']

export function JournalTradeFields({
  draft,
  onChange,
  mistakeTags,
}: {
  draft: TradeDraft
  onChange: (draft: TradeDraft) => void
  mistakeTags: string[]
}) {
  const set = <K extends keyof TradeDraft>(key: K, value: TradeDraft[K]) =>
    onChange({ ...draft, [key]: value })

  const toggleMistake = (tag: string) =>
    set(
      'mistakeTags',
      draft.mistakeTags.includes(tag)
        ? draft.mistakeTags.filter((item) => item !== tag)
        : [...draft.mistakeTags, tag],
    )

  const rr = plannedRR(draftToTrade(draft) as JournalTrade)

  return (
    <fieldset className="journal-trade" aria-label="Obchod">
      <div className="journal-form-row">
        <select
          value={draft.direction}
          onChange={(event) => set('direction', event.target.value as TradeDirection)}
          aria-label="Směr"
        >
          <option value="long">Long</option>
          <option value="short">Short</option>
        </select>
        <input
          type="text"
          inputMode="decimal"
          value={draft.size}
          onChange={(event) => set('size', event.target.value)}
          placeholder="velikost"
          aria-label="Velikost pozice"
        />
      </div>

      <div className="journal-form-row">
        <span className="muted">Plán</span>
        <input
          type="text"
          inputMode="decimal"
          value={draft.plannedEntry}
          onChange={(event) => set('plannedEntry', event.target.value)}
          placeholder="vstup"
          aria-label="Plánovaný vstup"
        />
        <input
          type="text"
          inputMode="decimal"
          value={draft.plannedStop}
          onChange={(event) => set('plannedStop', event.target.value)}
          placeholder="stop"
          aria-label="Plánovaný stop"
        />
        <input
          type="text"
          inputMode="decimal"
          value={draft.plannedTarget}
          onChange={(event) => set('plannedTarget', event.target.value)}
          placeholder="cíl"
          aria-label="Plánovaný cíl"
        />
        {rr !== null && <span className="muted">R:R {rr.toFixed(2)}</span>}
      </div>

      <div className="journal-form-row">
        <span className="muted">Exekuce</span>
        <input
          type="text"
          inputMode="decimal"
          value={draft.actualEntry}
          onChange={(event) => set('actualEntry', event.target.value)}
          placeholder="vstup"
          aria-label="Skutečný vstup"
        />
        <input
          type="text"
          inputMode="decimal"
          value={draft.actualExit}
          onChange={(event) => set('actualExit', event.target.value)}
          placeholder="výstup"
          aria-label="Skutečný výstup"
        />
        <input
          type="text"
          inputMode="decimal"
          value={draft.netPnl}
          onChange={(event) => set('netPnl', event.target.value)}
          placeholder="net P/L"
          aria-label="Net P/L"
        />
      </div>

      <div className="journal-form-row">
        <select
          value={draft.setupGrade}
          onChange={(event) => set('setupGrade', event.target.value as JournalGrade | '')}
          aria-label="Známka setupu"
          title="Kvalita setupu NEZÁVISLE na výsledku — dobrý obchod a ziskový obchod jsou dvě různé věci"
        >
          <option value="">setup —</option>
          {GRADES.map((grade) => (
            <option key={grade} value={grade}>
              setup {grade}
            </option>
          ))}
        </select>
        <select
          value={draft.executionGrade}
          onChange={(event) => set('executionGrade', event.target.value as JournalGrade | '')}
          aria-label="Známka exekuce"
          title="Jak jsem dodržel vlastní plán"
        >
          <option value="">exekuce —</option>
          {GRADES.map((grade) => (
            <option key={grade} value={grade}>
              exekuce {grade}
            </option>
          ))}
        </select>
        <select
          value={draft.emotion}
          onChange={(event) => set('emotion', event.target.value)}
          aria-label="Emoce"
        >
          <option value="">emoce —</option>
          {[1, 2, 3, 4, 5].map((level) => (
            <option key={level} value={String(level)}>
              emoce {level}
            </option>
          ))}
        </select>
      </div>

      {mistakeTags.length > 0 && (
        <div className="journal-form-row journal-mistakes" aria-label="Chyby">
          {mistakeTags.map((tag) => (
            <button
              key={tag}
              type="button"
              className={draft.mistakeTags.includes(tag) ? 'chip active' : 'chip'}
              onClick={() => toggleMistake(tag)}
              aria-pressed={draft.mistakeTags.includes(tag)}
            >
              {mistakeLabel(tag)}
            </button>
          ))}
        </div>
      )}
    </fieldset>
  )
}
