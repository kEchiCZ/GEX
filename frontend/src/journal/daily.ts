/** Ranní plán a Daily Report Card (#712) — typy a čisté helpery.

Hodnotí se PROCES, ne P/L (SMB, Breitstein). Proto report card nikde
neukazuje výsledek: známka za dodržení vlastních pravidel je jediné, co
jde druhý den ovlivnit.

Steenbarger: záznamy se musí navzájem odkazovat — cíl na zítřek se propíše
do zítřejšího plánu jako „včerejší cíl", jinak je to jen seznam přání.
*/
export type ReportGrade = 'A' | 'B' | 'C' | 'D' | 'F'

export const REPORT_GRADES: ReportGrade[] = ['A', 'B', 'C', 'D', 'F']

export interface PlanScenario {
  /** „nad flipem", „ztráta okraje zóny"… */
  condition: string
  /** Co udělám — včetně rizika. */
  action: string
}

export interface DailyPlan {
  scenarios: PlanScenario[]
  process_goal: string
  mental_state: number | null
  /** Okamžik zamčení; po něm se plán needituje. */
  locked_ts: string | null
  /** Cíl z včerejšího vyhodnocení, na který dnešek navazuje. */
  prev_goal: string
}

export interface SegmentGrade {
  key: string
  grade: ReportGrade | ''
  note: string
}

export interface DailyReview {
  segments: SegmentGrade[]
  lesson: string
  tomorrow_goal: string
  /** Odkaz na ranní plán téhož dne — kontinuita plán → realita. */
  plan_entry_id: number | null
}

export interface DailyPayload {
  plan?: DailyPlan
  review?: DailyReview
}

export const EMPTY_PLAN: DailyPlan = {
  scenarios: [{ condition: '', action: '' }],
  process_goal: '',
  mental_state: null,
  locked_ts: null,
  prev_goal: '',
}

export function emptyReview(segmentKeys: string[]): DailyReview {
  return {
    segments: segmentKeys.map((key) => ({ key, grade: '', note: '' })),
    lesson: '',
    tomorrow_goal: '',
    plan_entry_id: null,
  }
}

/** Je plán zamčený? Zamčení je nevratné — jinak by „plán" šel dopsat po faktu. */
export function isPlanLocked(plan: DailyPlan | undefined): boolean {
  return Boolean(plan?.locked_ts)
}

/** Scénáře bez obsahu se neukládají — prázdný řádek není plán. */
export function cleanScenarios(scenarios: PlanScenario[]): PlanScenario[] {
  return scenarios.filter(
    (scenario) => scenario.condition.trim() !== '' || scenario.action.trim() !== '',
  )
}

/** Cíl na zítřek z posledního vyhodnocení PŘED daným dnem. */
export function previousGoal(
  entries: Array<{ ts_ref: string; daily?: DailyPayload | null }>,
  dayIso: string,
): string {
  const earlier = entries
    .filter((entry) => entry.ts_ref.slice(0, 10) < dayIso && entry.daily?.review?.tomorrow_goal)
    .sort((a, b) => a.ts_ref.localeCompare(b.ts_ref))
  const last = earlier[earlier.length - 1]
  return last?.daily?.review?.tomorrow_goal ?? ''
}

/** Text plánu do `text` záznamu — deník zůstane čitelný i bez UI. */
export function planToText(plan: DailyPlan): string {
  const lines: string[] = []
  if (plan.prev_goal.trim() !== '') lines.push(`Včerejší cíl: ${plan.prev_goal.trim()}`)
  for (const scenario of cleanScenarios(plan.scenarios)) {
    lines.push(`Když ${scenario.condition.trim()} → ${scenario.action.trim()}`)
  }
  if (plan.process_goal.trim() !== '') lines.push(`Procesní cíl: ${plan.process_goal.trim()}`)
  if (plan.mental_state !== null) lines.push(`Stav: ${plan.mental_state}/5`)
  return lines.join('\n')
}

/** Text vyhodnocení do `text` záznamu. */
export function reviewToText(review: DailyReview, labels: Record<string, string>): string {
  const lines: string[] = []
  for (const segment of review.segments) {
    if (segment.grade === '' && segment.note.trim() === '') continue
    const label = labels[segment.key] ?? segment.key
    const note = segment.note.trim() === '' ? '' : ` — ${segment.note.trim()}`
    lines.push(`${label}: ${segment.grade || '—'}${note}`)
  }
  if (review.lesson.trim() !== '') lines.push(`Lekce: ${review.lesson.trim()}`)
  if (review.tomorrow_goal.trim() !== '') lines.push(`Zítra: ${review.tomorrow_goal.trim()}`)
  return lines.join('\n')
}
