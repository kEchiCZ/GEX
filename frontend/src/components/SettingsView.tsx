/** Settings (SPEC 7.5): konfigurace enginu a UI; změny se ukládají okamžitě.

Hodnoty jdou přes PUT /settings/{key} hned při změně — engine si je čte
průběžně (bez restartu tam, kde SPEC restart nevyžaduje). Téma se aplikuje
živě na kořenový element.
*/
import { useServerSettings } from '../api/settings'
import { useAppState } from '../state/AppState'
import type { Theme } from '../state/AppState'

interface NumberField {
  key: string
  label: string
  fallback: number
}

const ENGINE_FIELDS: NumberField[] = [
  { key: 'strike_range_points', label: 'Rozsah strikes (± body)', fallback: 200 },
  { key: 'batch_size', label: 'Velikost dávky', fallback: 80 },
  { key: 'hot_zone_width', label: 'Šířka hot zóny (± strikes)', fallback: 15 },
  { key: 'retention_days', label: 'Retence (dny)', fallback: 14 },
  { key: 'disk_limit_gb', label: 'Disk limit (GB)', fallback: 2 },
]

export function SettingsView() {
  const { theme, setTheme } = useAppState()
  const { values, put } = useServerSettings()

  return (
    <main className="settings" aria-label="Settings">
      <section aria-label="IBKR">
        <h2>IBKR</h2>
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
      </section>

      <section aria-label="Engine">
        <h2>Engine</h2>
        {ENGINE_FIELDS.map((field) => (
          <label key={field.key}>
            {field.label}
            <input
              type="number"
              value={Number(values[field.key] ?? field.fallback)}
              onChange={(event) => put(field.key, Number(event.target.value))}
            />
          </label>
        ))}
      </section>

      <section aria-label="Vzhled">
        <h2>Vzhled</h2>
        <label>
          Téma
          <select
            value={theme}
            onChange={(event) => {
              const next = event.target.value as Theme
              setTheme(next) // aplikuje se okamžitě, bez restartu (AC)
              put('theme', next)
            }}
          >
            <option value="dark">Dark</option>
            <option value="light">Light</option>
          </select>
        </label>
        <label>
          Jazyk
          <select
            value={String(values.language ?? 'cs')}
            onChange={(event) => put('language', event.target.value)}
          >
            <option value="cs">Čeština</option>
            <option value="en">English</option>
          </select>
        </label>
      </section>

      <section aria-label="Seance">
        <h2>Seance</h2>
        <label>
          Seznam seancí (JSON: [{'{'}"label", "minuteIdx"{'}'}])
          <textarea
            rows={3}
            defaultValue={JSON.stringify(
              values.sessions ?? [
                { label: 'London', minuteIdx: 60 },
                { label: 'New York', minuteIdx: 210 },
              ],
            )}
            onBlur={(event) => {
              try {
                put('sessions', JSON.parse(event.target.value))
              } catch {
                // Nevalidní JSON se neukládá — pole zůstává k opravě
              }
            }}
          />
        </label>
      </section>
    </main>
  )
}
