/** Kalendářový selektor expirací (#513, SPEC 3.2) — náhrada prostého dropdownu.

Popover s měsíční mřížkou: expirační dny zvýrazněné podle druhu (denní/
týdenní/EOM/měsíční/kvartální), dnešek orámovaný, vybraná expirace plná.
Den s více trading classes (MES styl) nabídne druhý krok — série jsou vidět
jmenovitě, výběr ale vede na řetěz data (snapshoty slévají série per den,
per-série řetěz by chtěl zásah do enginu). Klávesy: Enter/mezerník otevře,
šipky posouvají fokus po dnech, PgUp/PgDn měsíce, Esc zavře.
*/
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  dayTitle,
  expiryMonth,
  kindClass,
  monthGrid,
  monthLabel,
  expiryKeyOf,
} from '../instrument/calendar'
import { expiryKind } from '../instrument/expiry'

interface ExpiryCalendarProps {
  expiries: string[]
  /** Trading classes per expirace z OI archivu; prázdné/chybějící = neznámé. */
  expiryClasses: Record<string, string[]>
  selected: string | null
  onSelect: (expiry: string) => void
  /** Expirace dodávané tastytrade (#616) — v seznamu i tooltipu značené. */
  extended: ReadonlySet<string>
  now: Date
}

export function ExpiryCalendar({
  expiries,
  expiryClasses,
  selected,
  onSelect,
  extended,
  now,
}: ExpiryCalendarProps) {
  const [open, setOpen] = useState(false)
  // Druhý krok pro den s více trading classes — drží expiraci, čeká na sérii
  const [tcStep, setTcStep] = useState<string | null>(null)
  const initialMonth = () => {
    const base = selected ? expiryMonth(selected) : null
    return base ?? { year: now.getUTCFullYear(), month: now.getUTCMonth() }
  }
  const [view, setView] = useState(initialMonth)
  const wrapRef = useRef<HTMLDivElement>(null)
  const gridRef = useRef<HTMLDivElement>(null)

  const expirySet = useMemo(() => new Set(expiries), [expiries])
  const todayKey = expiryKeyOf(now)

  // Zavření kliknutím mimo popover — dropdown nesmí zůstat viset přes graf
  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
        setOpen(false)
        setTcStep(null)
      }
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open])

  const shiftMonth = (delta: number) => {
    setView((prev) => {
      const index = prev.year * 12 + prev.month + delta
      return { year: Math.floor(index / 12), month: ((index % 12) + 12) % 12 }
    })
  }

  const pick = (expiry: string) => {
    const classes = expiryClasses[expiry] ?? []
    if (classes.length > 1) {
      setTcStep(expiry)
      return
    }
    onSelect(expiry)
    setOpen(false)
    setTcStep(null)
  }

  const openCalendar = () => {
    setView(initialMonth())
    setTcStep(null)
    setOpen((prev) => !prev)
  }

  // Roving fokus šipkami po dnech s expirací (klávesová dostupnost, AC #513)
  const onGridKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') {
      setOpen(false)
      setTcStep(null)
      return
    }
    if (event.key === 'PageUp' || event.key === 'PageDown') {
      event.preventDefault()
      shiftMonth(event.key === 'PageUp' ? -1 : 1)
      return
    }
    const steps: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 } // prettier-ignore
    const step = steps[event.key]
    if (step === undefined || gridRef.current === null) return
    event.preventDefault()
    const buttons = Array.from(
      gridRef.current.querySelectorAll<HTMLButtonElement>('button.cal-day:not(:disabled)'),
    )
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement)
    const next = buttons[current === -1 ? 0 : current + step]
    next?.focus()
  }

  const weeks = monthGrid(view.year, view.month, expirySet)
  const selectedClasses = selected ? (expiryClasses[selected] ?? []) : []
  const triggerLabel =
    selected === null
      ? '—'
      : selectedClasses.length > 0
        ? `${selected} · ${selectedClasses.join('/')}`
        : extended.has(selected)
          ? `${selected} · tasty`
          : selected

  return (
    <div className="expiry-calendar" ref={wrapRef}>
      <button
        type="button"
        className="cal-trigger"
        data-testid="expiry-trigger"
        disabled={expiries.length === 0}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={openCalendar}
      >
        {triggerLabel} <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div
          className="cal-popover"
          role="dialog"
          aria-label="Výběr expirace"
          data-testid="expiry-calendar"
          onKeyDown={onGridKeyDown}
        >
          <div className="cal-header">
            <button type="button" aria-label="Předchozí měsíc" onClick={() => shiftMonth(-1)}>
              ‹
            </button>
            <span className="cal-title">{monthLabel(view.year, view.month)}</span>
            <button type="button" aria-label="Další měsíc" onClick={() => shiftMonth(1)}>
              ›
            </button>
          </div>
          <div className="cal-grid" ref={gridRef}>
            {['Po', 'Út', 'St', 'Čt', 'Pá', 'So', 'Ne'].map((name) => (
              <span key={name} className="cal-weekday muted">
                {name}
              </span>
            ))}
            {weeks.flat().map((cell, index) => {
              if (cell.expiry === null) {
                return (
                  <span
                    key={index}
                    className={`cal-day cal-plain${cell.inMonth ? '' : ' cal-out'}`}
                  >
                    {cell.day}
                  </span>
                )
              }
              const classes = expiryClasses[cell.expiry] ?? []
              const marks = [
                'cal-day',
                'cal-expiry',
                kindClass(expiryKind(cell.expiry)),
                cell.inMonth ? '' : 'cal-out',
                cell.expiry === selected ? 'cal-selected' : '',
                cell.expiry === todayKey ? 'cal-today' : '',
              ]
              return (
                <button
                  key={index}
                  type="button"
                  className={marks.filter(Boolean).join(' ')}
                  title={dayTitle(cell.expiry, classes, extended.has(cell.expiry))}
                  data-expiry={cell.expiry}
                  onClick={() => pick(cell.expiry as string)}
                >
                  {cell.day}
                  {classes.length > 1 && <span className="cal-multi">{classes.length}</span>}
                </button>
              )
            })}
          </div>
          <div className="cal-legend muted">
            <span className="cal-kind-daily">denní</span>
            <span className="cal-kind-weekly">týdenní</span>
            <span className="cal-kind-eom">EOM</span>
            <span className="cal-kind-monthly">měsíční</span>
            <span className="cal-kind-quarterly">kvartální</span>
          </div>
          {tcStep !== null && (
            <div className="cal-tc-step" data-testid="cal-tc-step">
              <p className="muted">
                {tcStep}: den s více sériemi — heatmapa zobrazuje řetěz celého dne (série sloučené).
              </p>
              {(expiryClasses[tcStep] ?? []).map((tc) => (
                <button
                  key={tc}
                  type="button"
                  className="cal-tc-option"
                  onClick={() => {
                    onSelect(tcStep)
                    setOpen(false)
                    setTcStep(null)
                  }}
                >
                  {tc}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
