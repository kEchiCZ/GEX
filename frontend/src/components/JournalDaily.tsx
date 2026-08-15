/** Ranní plán a Daily Report Card (#712).

Hodnotí se PROCES, ne P/L — report card proto nikde neukazuje výsledek.
Známka za dodržení vlastních pravidel je jediné, co jde druhý den ovlivnit.
*/
import type { JournalProfile } from '../api/journal'
import type { DailyPlan, DailyReview, ReportGrade } from '../journal/daily'
import { REPORT_GRADES, isPlanLocked } from '../journal/daily'
import { daySegments } from '../journal/segments'

export function JournalPlanFields({
  plan,
  onChange,
}: {
  plan: DailyPlan
  onChange: (plan: DailyPlan) => void
}) {
  const locked = isPlanLocked(plan)
  const set = <K extends keyof DailyPlan>(key: K, value: DailyPlan[K]) =>
    onChange({ ...plan, [key]: value })

  const setScenario = (index: number, patch: Partial<{ condition: string; action: string }>) =>
    set(
      'scenarios',
      plan.scenarios.map((scenario, idx) => (idx === index ? { ...scenario, ...patch } : scenario)),
    )

  return (
    <fieldset className="journal-daily" aria-label="Ranní plán" disabled={locked}>
      {plan.prev_goal.trim() !== '' && (
        <p className="muted">
          Včerejší cíl: <strong>{plan.prev_goal}</strong>
        </p>
      )}
      {plan.scenarios.map((scenario, index) => (
        <div className="journal-form-row" key={index}>
          <span className="muted">Když</span>
          <input
            type="text"
            value={scenario.condition}
            onChange={(event) => setScenario(index, { condition: event.target.value })}
            placeholder="nad flipem 6805"
            aria-label={`Podmínka ${index + 1}`}
          />
          <span className="muted">→</span>
          <input
            type="text"
            value={scenario.action}
            onChange={(event) => setScenario(index, { action: event.target.value })}
            placeholder="long k call wall, risk 3 body"
            aria-label={`Akce ${index + 1}`}
          />
        </div>
      ))}
      <div className="journal-form-row">
        <button
          type="button"
          className="chip"
          onClick={() => set('scenarios', [...plan.scenarios, { condition: '', action: '' }])}
        >
          + Scénář
        </button>
        <input
          type="text"
          value={plan.process_goal}
          onChange={(event) => set('process_goal', event.target.value)}
          placeholder="procesní cíl dne (max 3 obchody…)"
          aria-label="Procesní cíl"
        />
        <select
          value={plan.mental_state === null ? '' : String(plan.mental_state)}
          onChange={(event) =>
            set('mental_state', event.target.value === '' ? null : Number(event.target.value))
          }
          aria-label="Mentální stav"
        >
          <option value="">stav —</option>
          {[1, 2, 3, 4, 5].map((level) => (
            <option key={level} value={String(level)}>
              stav {level}
            </option>
          ))}
        </select>
      </div>
    </fieldset>
  )
}

export function JournalReviewFields({
  review,
  onChange,
  profile,
  dateIso,
}: {
  review: DailyReview
  onChange: (review: DailyReview) => void
  profile: JournalProfile
  dateIso: string
}) {
  const segments = daySegments(profile, dateIso)
  const labels = new Map(segments.map((segment) => [segment.key, segment.label]))

  const setSegment = (key: string, patch: Partial<{ grade: ReportGrade | ''; note: string }>) =>
    onChange({
      ...review,
      segments: review.segments.map((segment) =>
        segment.key === key ? { ...segment, ...patch } : segment,
      ),
    })

  return (
    <fieldset className="journal-daily" aria-label="Vyhodnocení dne">
      {review.segments.map((segment) => (
        <div className="journal-form-row" key={segment.key}>
          <span className="journal-segment-label muted">
            {labels.get(segment.key) ?? segment.key}
          </span>
          <select
            value={segment.grade}
            onChange={(event) =>
              setSegment(segment.key, { grade: event.target.value as ReportGrade | '' })
            }
            aria-label={`Známka ${labels.get(segment.key) ?? segment.key}`}
            title="Známka za PROCES, ne za zisk"
          >
            <option value="">—</option>
            {REPORT_GRADES.map((grade) => (
              <option key={grade} value={grade}>
                {grade}
              </option>
            ))}
          </select>
          <input
            type="text"
            value={segment.note}
            onChange={(event) => setSegment(segment.key, { note: event.target.value })}
            placeholder="poznámka"
            aria-label={`Poznámka ${labels.get(segment.key) ?? segment.key}`}
          />
        </div>
      ))}
      <div className="journal-form-row">
        <input
          type="text"
          value={review.lesson}
          onChange={(event) => onChange({ ...review, lesson: event.target.value })}
          placeholder="lekce dne"
          aria-label="Lekce dne"
        />
        <input
          type="text"
          value={review.tomorrow_goal}
          onChange={(event) => onChange({ ...review, tomorrow_goal: event.target.value })}
          placeholder="cíl na zítřek"
          aria-label="Cíl na zítřek"
        />
      </div>
    </fieldset>
  )
}
