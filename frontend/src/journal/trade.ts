/** Rozepsaný obchod deníku a převody na API tvar (#709).

Číselné vstupy drží ROZEPSANOU hodnotu jako text a převádějí se až při
odeslání — stejné poučení jako #445: rozepsané „68" z „6810" nesmí odejít
jako platná cena.
*/
import type { JournalGrade, JournalTrade, TradeDirection } from '../api/journal'

export interface TradeDraft {
  /** Klíč setupu z playbooku — povinný (#710). */
  setupKey: string
  /** Proč teze selhala (#711) — nabízí se jen u ztrátového obchodu. */
  failureMode: string
  direction: TradeDirection
  plannedEntry: string
  plannedStop: string
  plannedTarget: string
  actualEntry: string
  actualExit: string
  size: string
  setupGrade: JournalGrade | ''
  executionGrade: JournalGrade | ''
  mistakeTags: string[]
  emotion: string
  grossPnl: string
  netPnl: string
  fees: string
}

export const EMPTY_TRADE: TradeDraft = {
  setupKey: '',
  failureMode: '',
  direction: 'long',
  plannedEntry: '',
  plannedStop: '',
  plannedTarget: '',
  actualEntry: '',
  actualExit: '',
  size: '',
  setupGrade: '',
  executionGrade: '',
  mistakeTags: [],
  emotion: '',
  grossPnl: '',
  netPnl: '',
  fees: '',
}

/** Prázdný nebo nečíselný vstup → null, ať se nedosazují nuly. */
function num(value: string): number | null {
  const trimmed = value.trim().replace(',', '.')
  if (trimmed === '') return null
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : null
}

export function draftToTrade(draft: TradeDraft): Partial<JournalTrade> & {
  direction: TradeDirection
} {
  return {
    direction: draft.direction,
    setup_key: draft.setupKey === '' ? null : draft.setupKey,
    failure_mode: draft.failureMode === '' ? null : draft.failureMode,
    planned_entry: num(draft.plannedEntry),
    planned_stop: num(draft.plannedStop),
    planned_target: num(draft.plannedTarget),
    actual_entry: num(draft.actualEntry),
    actual_exit: num(draft.actualExit),
    size: num(draft.size),
    setup_grade: draft.setupGrade === '' ? null : draft.setupGrade,
    execution_grade: draft.executionGrade === '' ? null : draft.executionGrade,
    mistake_tags: draft.mistakeTags,
    emotion: num(draft.emotion),
    gross_pnl: num(draft.grossPnl),
    net_pnl: num(draft.netPnl),
    fees: num(draft.fees),
  }
}

export function tradeToDraft(trade: JournalTrade): TradeDraft {
  const str = (value: number | null) => (value === null ? '' : String(value))
  return {
    setupKey: trade.setup_key ?? '',
    failureMode: trade.failure_mode ?? '',
    direction: trade.direction,
    plannedEntry: str(trade.planned_entry),
    plannedStop: str(trade.planned_stop),
    plannedTarget: str(trade.planned_target),
    actualEntry: str(trade.actual_entry),
    actualExit: str(trade.actual_exit),
    size: str(trade.size),
    setupGrade: trade.setup_grade ?? '',
    executionGrade: trade.execution_grade ?? '',
    mistakeTags: trade.mistake_tags,
    emotion: str(trade.emotion),
    grossPnl: str(trade.gross_pnl),
    netPnl: str(trade.net_pnl),
    fees: str(trade.fees),
  }
}
