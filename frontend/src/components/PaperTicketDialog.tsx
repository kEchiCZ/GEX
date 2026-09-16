/** Order ticket paper účtu (#1187 fáze 2): strana, typ, entry/stop/cíl, kontrakty
ze sizingu, setup z playbooku. Risk vrstva blokuje na serveru — 409 se ukáže
jako důvod; klient jen předpočítá maximum kontraktů z rozpočtu. */
import { useEffect, useState } from 'react'
import { fetchPlaybook } from '../api/journal'
import type { PlaybookItem } from '../api/journal'
import { maxContracts, placePaperOrder, rewardRisk, riskUsd } from '../api/paper'
import type { PaperAccount, PaperOrderType, PaperSide } from '../api/paper'
import { pointValue } from '../instrument/tick'

export function PaperTicketDialog({
  symbol,
  account,
  spot,
  onPlaced,
  onCancel,
}: {
  symbol: string
  account: PaperAccount
  /** Aktuální cena — výchozí entry; null = ticket bez předvyplnění. */
  spot: number | null
  onPlaced: () => void
  onCancel: () => void
}) {
  const pv = pointValue(symbol)
  const tick = symbol.startsWith('NQ') || symbol.startsWith('MNQ') ? 25 : 10
  const [side, setSide] = useState<PaperSide>('long')
  const [orderType, setOrderType] = useState<PaperOrderType>('limit')
  const [entry, setEntry] = useState(spot === null ? '' : String(spot))
  const [stop, setStop] = useState(spot === null ? '' : String(spot - tick))
  const [target, setTarget] = useState(spot === null ? '' : String(spot + 2 * tick))
  const [qtyText, setQtyText] = useState('')
  const [setupKey, setSetupKey] = useState('')
  const [note, setNote] = useState('')
  const [playbook, setPlaybook] = useState<PlaybookItem[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    void fetchPlaybook().then((items) => {
      if (!cancelled) setPlaybook(items)
    })
    return () => {
      cancelled = true
    }
  }, [])

  const num = (text: string): number | null => {
    const value = Number(text.replace(',', '.'))
    return Number.isFinite(value) && value > 0 ? value : null
  }
  const entryValue = num(entry)
  const stopValue = num(stop)
  const targetValue = target.trim() === '' ? null : num(target)
  const allowed =
    entryValue !== null && stopValue !== null
      ? maxContracts(account.risk_budget_usd, entryValue, stopValue, pv)
      : 0
  const qty =
    qtyText.trim() === '' ? Math.max(1, allowed) : Math.max(1, Math.floor(Number(qtyText) || 1))
  const risk =
    entryValue !== null && stopValue !== null ? riskUsd(qty, entryValue, stopValue, pv) : null
  const rrr =
    entryValue !== null && stopValue !== null
      ? rewardRisk(entryValue, stopValue, targetValue)
      : null
  const levelsOk =
    entryValue !== null &&
    stopValue !== null &&
    (side === 'long' ? stopValue < entryValue : stopValue > entryValue) &&
    (targetValue === null ||
      (side === 'long' ? targetValue > entryValue : targetValue < entryValue))
  const overBudget = allowed === 0 || qty > allowed
  const valid = levelsOk && !overBudget && !account.halted && account.brake === null

  const flip = (next: PaperSide) => {
    setSide(next)
    if (entryValue !== null) {
      setStop(String(next === 'long' ? entryValue - tick : entryValue + tick))
      setTarget(String(next === 'long' ? entryValue + 2 * tick : entryValue - 2 * tick))
    }
  }

  return (
    <div className="scenario-dialog paper-ticket" role="dialog" aria-label="Paper order">
      <h3>⚡ Paper order · {symbol}</h3>
      <p className="muted">
        Equity {Math.round(account.equity)} $ · rozpočet rizika{' '}
        {Math.round(account.risk_budget_usd)} $ ({account.risk_pct} %) · dnes{' '}
        {account.day_r >= 0 ? '+' : ''}
        {account.day_r.toFixed(1)} R{account.brake ? ` · BRZDA ${account.brake}` : ''}
        {account.halted ? ' · KILL SWITCH' : ''}. Fily proti živé ceně: market na open dalšího baru
        + 1 tick, limit při protnutí, stop-first ve svíčce, vše denní.
      </p>
      <div className="journal-form-row">
        <button
          type="button"
          className={side === 'long' ? 'chip active' : 'chip'}
          onClick={() => flip('long')}
        >
          LONG
        </button>
        <button
          type="button"
          className={side === 'short' ? 'chip active' : 'chip'}
          onClick={() => flip('short')}
        >
          SHORT
        </button>
        <select
          value={orderType}
          aria-label="Typ orderu"
          onChange={(event) => setOrderType(event.target.value as PaperOrderType)}
        >
          <option value="limit">limit</option>
          <option value="market">market</option>
          <option value="stop">stop (průraz)</option>
        </select>
      </div>
      <label>
        Entry
        <input
          type="text"
          inputMode="decimal"
          value={entry}
          aria-label="Entry"
          onChange={(event) => setEntry(event.target.value)}
        />
      </label>
      <label>
        Stop
        <input
          type="text"
          inputMode="decimal"
          value={stop}
          aria-label="Stop"
          onChange={(event) => setStop(event.target.value)}
        />
      </label>
      <label>
        Cíl (volitelný)
        <input
          type="text"
          inputMode="decimal"
          value={target}
          aria-label="Cíl"
          onChange={(event) => setTarget(event.target.value)}
        />
      </label>
      <label>
        Kontrakty
        <input
          type="number"
          min={1}
          max={100}
          value={qtyText === '' ? qty : qtyText}
          aria-label="Kontrakty"
          onChange={(event) => setQtyText(event.target.value)}
        />
        <span className="muted" data-testid="paper-sizing">
          {risk === null
            ? '—'
            : `riziko ${Math.round(risk)} $ · max ${allowed} ks` +
              (rrr === null ? '' : ` · RRR ${rrr.toFixed(1)}`)}
        </span>
      </label>
      <label>
        Setup (playbook)
        <select
          value={setupKey}
          aria-label="Setup"
          onChange={(event) => setSetupKey(event.target.value)}
        >
          <option value="">—</option>
          {playbook.map((item) => (
            <option key={item.key} value={item.key}>
              {item.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Poznámka
        <input
          type="text"
          value={note}
          maxLength={500}
          aria-label="Poznámka orderu"
          onChange={(event) => setNote(event.target.value)}
          placeholder="proč vstupuji…"
        />
      </label>
      {!levelsOk && entryValue !== null && stopValue !== null && (
        <p className="journal-error">Stop musí ležet proti směru a cíl ve směru obchodu.</p>
      )}
      {levelsOk && overBudget && (
        <p className="journal-error">
          {allowed === 0
            ? `Stop ${Math.abs(entryValue - stopValue).toFixed(2)} b je nad rozpočtem ${Math.round(account.risk_budget_usd)} $ — zkrať stop.`
            : `Rozpočet dovolí max ${allowed} ks.`}
        </p>
      )}
      {error && <p className="journal-error">{error}</p>}
      <div className="journal-form-row">
        <button
          type="button"
          className="chip active"
          disabled={!valid || busy}
          data-testid="paper-submit"
          onClick={() => {
            if (entryValue === null || stopValue === null) return
            setBusy(true)
            setError(null)
            void placePaperOrder({
              symbol,
              side,
              qty,
              order_type: orderType,
              entry_price: entryValue,
              stop_price: stopValue,
              target_price: targetValue,
              setup_key: setupKey || null,
              note: note.trim() || null,
              context: { spot_at_ticket: spot },
            }).then((result) => {
              setBusy(false)
              if (result.ok) onPlaced()
              else setError(result.error)
            })
          }}
        >
          {busy ? 'Podávám…' : `Podat ${side.toUpperCase()} ${qty}×`}
        </button>
        <button type="button" className="chip" disabled={busy} onClick={onCancel}>
          Zrušit
        </button>
      </div>
    </div>
  )
}
