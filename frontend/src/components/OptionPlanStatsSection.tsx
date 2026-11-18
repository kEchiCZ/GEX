/** Stats → plán opce QQQ ke scénáři dne NQ (F5b #1749, ADR-0062 bod 7): track record zvlášť
od scénáře NQ. Rozhodnuto 18. 11. v #1738: vlastní sekce vázaná na symbol Stats (Q1 A),
mezera a bez opce jen v řádku stavů, vyřazené P3 jako hypotetický řádek (Q2 A), stíny se
stejnou strukturou jako hlavní plán (Q3 B), před začátkem track recordu jen prázdný stav
(Q4 a A). Čísla počítá engine, sekce je jen zobrazí. */
import { useEffect, useState } from 'react'
import {
  OPTION_PLAN_SCENARIO_SYMBOL,
  OUTCOME_TOOLTIPS,
  ROW_TOOLTIPS,
  SHADOW_LABELS,
  fetchOptionPlanStats,
} from '../api/optionPlans'
import type {
  OptionPlanGroup,
  OptionPlanPnl,
  OptionPlanSection,
  OptionPlanStats,
  OptionPlanStatsResult,
} from '../api/optionPlans'
import { formatPnlUsd } from '../api/setups'

function pnlCell(pnl: OptionPlanPnl): string {
  if (pnl.n === 0 || pnl.mean_usd === null) return '—'
  return `Σ ${formatPnlUsd(pnl.sum_usd)} · ⌀ ${formatPnlUsd(pnl.mean_usd)} · zisk ${pnl.wins}/${pnl.n}`
}

function budgetLabel(budget: string): string {
  return budget === 'nenastaven' ? 'rozpočet nenastaven' : budget
}

interface GroupRowProps {
  label: string
  title: string
  group: OptionPlanGroup
  outcomes: string[]
  className: string
  testId: string
}

function GroupRow({ label, title, group, outcomes, className, testId }: GroupRowProps) {
  return (
    <tr className={className} data-testid={testId}>
      <td title={title}>{label}</td>
      <td>{group.n}</td>
      {outcomes.map((name) => (
        <td key={name}>{group.outcomes[name] ?? 0}</td>
      ))}
      <td>{pnlCell(group.pnl_tp1)}</td>
      <td>{pnlCell(group.pnl_hold)}</td>
      <td>{group.preliminary ? 'předběžné' : 'platné'}</td>
    </tr>
  )
}

/** Řádek skupiny a pod ním jen neprázdné třídy rozpočtu (Q2 A). */
function GroupRows({ label, title, group, outcomes, className, testId }: GroupRowProps) {
  const budgets = Object.entries(group.by_budget ?? {}).filter(([, part]) => part.n > 0)
  return (
    <>
      <GroupRow
        label={label}
        title={title}
        group={group}
        outcomes={outcomes}
        className={className}
        testId={testId}
      />
      {budgets.map(([budget, part]) => (
        <GroupRow
          key={budget}
          label={`↳ ${budgetLabel(budget)}`}
          title={ROW_TOOLTIPS.budget}
          group={part}
          outcomes={outcomes}
          className={`${className} option-plan-budget`}
          testId={`${testId}-budget-${budget}`}
        />
      ))}
    </>
  )
}

interface PartProps {
  partKey: string
  label: string
  section: OptionPlanSection
  outcomes: string[]
  shadow: boolean
}

function PartRows({ partKey, label, section, outcomes, shadow }: PartProps) {
  const status = section.by_status
  const rowClass = shadow ? 'option-plan-shadow' : ''
  const colSpan = outcomes.length + 5
  return (
    <tbody data-testid={`option-plan-part-${partKey}`}>
      <tr className={`option-plan-part ${rowClass}`}>
        <th colSpan={colSpan} title={shadow ? ROW_TOOLTIPS.shadow : undefined}>
          {label} — posouzeno {section.plans} (spuštěno {status['spuštěno'] ?? 0} · bez vstupu{' '}
          {status['bez vstupu'] ?? 0}) · mimo posouzení:{' '}
          <span title={ROW_TOOLTIPS.gap}>mezera {status['mezera'] ?? 0}</span> ·{' '}
          <span title={ROW_TOOLTIPS.noOption}>bez opce {status['bez opce'] ?? 0}</span>
          {section.preliminary ? ' · předběžné' : ' · platné'}
        </th>
      </tr>
      {Object.entries(section.variants).map(([role, variant]) => (
        <VariantRows
          key={role}
          partKey={partKey}
          role={role}
          variant={variant}
          outcomes={outcomes}
          rowClass={rowClass}
          colSpan={colSpan}
          prefix={shadow ? 'stín · ' : ''}
        />
      ))}
    </tbody>
  )
}

interface VariantRowsProps {
  partKey: string
  role: string
  variant: OptionPlanSection['variants'][string]
  outcomes: string[]
  rowClass: string
  colSpan: number
  prefix: string
}

function VariantRows({
  partKey,
  role,
  variant,
  outcomes,
  rowClass,
  colSpan,
  prefix,
}: VariantRowsProps) {
  const testId = `option-plan-${partKey}-${role}`
  return (
    <>
      <GroupRows
        label={`${prefix}${role} · obchody`}
        title={ROW_TOOLTIPS.trades}
        group={variant.trades}
        outcomes={outcomes}
        className={rowClass}
        testId={`${testId}-trades`}
      />
      <GroupRows
        label={`${prefix}${role} · vyřazeno P3 — hypoteticky`}
        title={ROW_TOOLTIPS.rejected}
        group={variant.rejected_p3}
        outcomes={outcomes}
        className={`${rowClass} option-plan-rejected`}
        testId={`${testId}-rejected`}
      />
      {variant.no_quote > 0 && (
        <tr className={rowClass} data-testid={`${testId}-no-quote`}>
          <td title={ROW_TOOLTIPS.noQuote}>
            {prefix}
            {role} · bez kotace
          </td>
          <td>{variant.no_quote}</td>
          <td colSpan={colSpan - 2} className="muted">
            mezera v kotaci při spoušti — mimo obchody i vyřazené P3
          </td>
        </tr>
      )}
    </>
  )
}

/** Názvy průběhů v pořadí enginu z první skupiny odpovědi (všechny skupiny je mají stejné). */
function outcomeNames(stats: OptionPlanStats): string[] {
  const first = Object.values(stats.main.variants)[0]
  return first ? Object.keys(first.trades.outcomes) : []
}

function StatsTable({ stats }: { stats: OptionPlanStats }) {
  const outcomes = outcomeNames(stats)
  return (
    <div className="option-plan-stats-wrap">
      <table className="stats-table option-plan-stats" data-testid="option-plan-stats">
        <thead>
          <tr>
            <th>řádek</th>
            <th>n</th>
            {outcomes.map((name) => (
              <th key={name} title={OUTCOME_TOOLTIPS[name]}>
                {name}
              </th>
            ))}
            <th title={ROW_TOOLTIPS.pnlTp1}>$ výstup v TP1</th>
            <th title={ROW_TOOLTIPS.pnlHold}>$ držení do TP2</th>
            <th>stav</th>
          </tr>
        </thead>
        <PartRows
          partKey="main"
          label="Hlavní plán"
          section={stats.main}
          outcomes={outcomes}
          shadow={false}
        />
        {Object.entries(stats.shadows).map(([key, section]) => (
          <PartRows
            key={key}
            partKey={key}
            label={`Stín · ${SHADOW_LABELS[key] ?? key}`}
            section={section}
            outcomes={outcomes}
            shadow
          />
        ))}
      </table>
    </div>
  )
}

function Body({ result }: { result: OptionPlanStatsResult | null }) {
  if (result === null) return <p className="muted">Načítám track record plánu…</p>
  if (!result.ok) {
    return (
      <p className="option-plan-error" role="alert" data-testid="option-plan-stats-error">
        Track record plánu se nepodařilo načíst ({result.error}).
      </p>
    )
  }
  const { stats } = result
  if (stats.since === null) {
    return (
      <p className="muted" data-testid="option-plan-stats-empty">
        Track record zatím nezačal: počítá se od první seance s nasazenými opravami vstupů plánu
        (#1774, #1653 + #1599 a #1610; Rozhodnuto 10. 11.). Vyhodnocené plány mimo track record:{' '}
        {stats.outside}. Výsledek každého plánu přichází po close do zvonku.
      </p>
    )
  }
  return (
    <>
      <p className="muted" data-testid="option-plan-stats-since">
        Od seance {stats.since} · pravidla plánu v{stats.rules_version} · vyhodnocené plány před
        začátkem (mimo track record): {stats.outside}
      </p>
      <StatsTable stats={stats} />
    </>
  )
}

export function OptionPlanStatsSection({ symbol }: { symbol: string }) {
  const planned = symbol === OPTION_PLAN_SCENARIO_SYMBOL
  const [result, setResult] = useState<OptionPlanStatsResult | null>(null)
  useEffect(() => {
    if (!planned) return
    let cancelled = false
    void fetchOptionPlanStats().then((next) => {
      if (!cancelled) setResult(next)
    })
    return () => {
      cancelled = true
    }
  }, [planned])
  return (
    <section className="stats-section" aria-label="Plán opce — track record">
      <h2>Plán opce — track record (#1749)</h2>
      {planned ? (
        <>
          <p className="muted">
            Plán opce QQQ ke scénáři dne NQ (ADR-0062), vedený zvlášť od track recordu scénáře.
            Engine ho po close vyhodnotí z naměřených barů QQQ a kotací opce, $ na 1 kontrakt; jen
            aktuální verze pravidel. Pod 30 případů je každé číslo předběžné. Stíny (vstup B, test
            knotem) se do hlavního plánu nepočítají.
          </p>
          <Body result={result} />
        </>
      ) : (
        <p className="muted" data-testid="option-plan-stats-other-symbol">
          Plán opce vzniká zatím jen ze scénáře dne {OPTION_PLAN_SCENARIO_SYMBOL} (opce QQQ). Ze
          scénáře {symbol} plán nevzniká, výběr instrumentů je zadání #1790.
        </p>
      )}
    </section>
  )
}
