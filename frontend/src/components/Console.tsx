/** IBKR Console (SPEC 7.5): log API událostí, správa připojení, repair fronta. */
import { useAppState } from '../state/AppState'
import { useServerSettings } from '../api/settings'

export function Console() {
  const { consoleLog, status } = useAppState()
  const { values, put } = useServerSettings()

  return (
    <main className="console" aria-label="IBKR Console">
      <section className="console-connection" aria-label="Připojení">
        <h2>Připojení</h2>
        <label>
          Host
          <input
            value={String(values.ibkr_host ?? '127.0.0.1')}
            onChange={(event) => put('ibkr_host', event.target.value)}
          />
        </label>
        <label>
          Port
          <input
            type="number"
            value={Number(values.ibkr_port ?? 7496)}
            onChange={(event) => put('ibkr_port', Number(event.target.value))}
          />
        </label>
        <label>
          Client ID
          <input
            type="number"
            value={Number(values.ibkr_client_id ?? 1)}
            onChange={(event) => put('ibkr_client_id', Number(event.target.value))}
          />
        </label>
        <button
          className="chip"
          onClick={() => put('reconnect_requested', new Date().toISOString())}
        >
          Reconnect
        </button>
        <p className="muted">
          Stav: {status.connection ?? status.engine}
          {status.port !== undefined ? ` :${status.port}` : ''}
        </p>
      </section>

      <section aria-label="Subskripce a repair fronta">
        <h2>Subskripce</h2>
        <ul className="muted">
          <li>
            Greeks: {status.greeks_complete ?? '—'}/{status.greeks_total ?? '—'}
          </li>
          <li>Repair fronta: {status.repair_count ?? 0} kontraktů</li>
          <li>
            Lines:{' '}
            {status.lines_utilization !== undefined
              ? `${Math.round(status.lines_utilization * 100)} %`
              : '—'}
          </li>
        </ul>
      </section>

      <section className="console-log" aria-label="Log API událostí">
        <h2>Log</h2>
        <ol>
          {consoleLog.length === 0 && <li className="muted">Zatím žádné události</li>}
          {consoleLog.map((line, index) => (
            <li key={index}>{line}</li>
          ))}
        </ol>
      </section>
    </main>
  )
}
