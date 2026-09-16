/** Chip paper účtu v hlavičce (#1187 fáze 2): equity, R dne, brzda/kill, otevřená
pozice nebo čekající order s tlačítky Zavřít a KILL. Bez účtu (API nedostupné,
engine ho ještě nezaložil) se nekreslí. */
import { closePaperOrder, killPaper, orderLabel, resumePaper } from '../api/paper'
import type { PaperAccount } from '../api/paper'

export function PaperChip({
  account,
  symbol,
  onChanged,
}: {
  account: PaperAccount | null
  symbol: string
  onChanged: () => void
}) {
  if (account === null) return null
  const mine = [...account.open, ...account.working].filter((order) => order.symbol === symbol)
  const others = account.open.length + account.working.length - mine.length
  const pnl = account.equity - account.equity_start
  const tone = account.halted ? 'halted' : account.brake ? 'braked' : pnl >= 0 ? 'up' : 'down'
  return (
    <span className={`paper-chip paper-${tone}`} data-testid="paper-chip">
      <span
        className="muted"
        title={
          `Paper účet (#1187): equity ${Math.round(account.equity)} $ (start ${Math.round(account.equity_start)} $), ` +
          `rozpočet rizika ${Math.round(account.risk_budget_usd)} $ = ${account.risk_pct} % equity.\n` +
          `Dnes ${account.day_r >= 0 ? '+' : ''}${account.day_r.toFixed(1)} R, týden ${account.week_r >= 0 ? '+' : ''}${account.week_r.toFixed(1)} R ` +
          `(brzdy −${account.daily_brake_r} R / −${account.weekly_brake_r} R).\n` +
          'Fily simuluje engine proti živé ceně; uzavřené obchody jdou do Deníku (tag paper).'
        }
      >
        📒 {Math.round(account.equity)} $ · dnes {account.day_r >= 0 ? '+' : ''}
        {account.day_r.toFixed(1)} R{account.brake ? ` · BRZDA` : ''}
        {account.halted ? ` · KILL` : ''}
      </span>
      {mine.map((order) => (
        <span key={order.id} className="paper-position" data-testid="paper-position">
          {orderLabel(order)}
          {order.close_requested ? ' · zavírá se' : ''}
          {!order.close_requested && (
            <button
              type="button"
              className="chip"
              title="Zruší čekající order / zavře pozici na open dalšího baru"
              onClick={() => void closePaperOrder(order.id).then(() => onChanged())}
            >
              ✕ {order.status === 'open' ? 'Zavřít' : 'Zrušit'}
            </button>
          )}
        </span>
      ))}
      {others > 0 && <span className="muted"> +{others} jinde</span>}
      {account.halted ? (
        <button
          type="button"
          className="chip"
          title={`Kill switch: ${account.halted_reason ?? ''}. Odblokuje nové ordery.`}
          onClick={() => void resumePaper().then(() => onChanged())}
        >
          Odblokovat
        </button>
      ) : (
        <button
          type="button"
          className="chip paper-kill"
          title="KILL SWITCH: zavře všechny paper pozice, zruší čekající ordery a zablokuje nové"
          onClick={() => {
            if (
              !window.confirm('KILL SWITCH: zavřít všechny paper pozice a zablokovat nové ordery?')
            )
              return
            void killPaper('ruční kill switch z UI').then(() => onChanged())
          }}
        >
          KILL
        </button>
      )}
    </span>
  )
}
