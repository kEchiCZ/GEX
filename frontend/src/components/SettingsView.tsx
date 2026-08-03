/** Settings (SPEC 7.5): konfigurace enginu a UI; změny se ukládají okamžitě.

Hodnoty jdou přes PUT /settings/{key} hned při změně — engine si je čte
průběžně (bez restartu tam, kde SPEC restart nevyžaduje). Téma se aplikuje
živě na kořenový element.
*/
import { useState } from 'react'
import { downloadBackup } from '../api/backup'
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
  // Fallbacky musí odpovídat defaultům enginu, jinak pole ukazuje nepravdu
  // (engine tyhle dvě hodnoty zatím čte z konfigurace, ne z DB — viz #438)
  { key: 'retention_days', label: 'Retence (dny)', fallback: 90 },
  { key: 'disk_limit_gb', label: 'Disk limit (GB)', fallback: 5 },
]

export function SettingsView() {
  const { theme, setTheme } = useAppState()
  const { values, put } = useServerSettings()
  const [backup, setBackup] = useState<'idle' | 'running'>('idle')
  const [backupNote, setBackupNote] = useState<string | null>(null)

  const runBackup = async () => {
    setBackup('running')
    setBackupNote(null)
    try {
      const where = await downloadBackup()
      setBackupNote(
        where === 'saved'
          ? 'Hotovo — záloha uložena na zvolené místo.'
          : 'Hotovo — záloha stažena do složky Stažené soubory (prohlížeč neumí výběr složky).',
      )
    } catch (error) {
      // Chyba musí být vidět: tichá neúspěšná záloha je horší než žádná
      setBackupNote(error instanceof Error ? error.message : 'Záloha selhala.')
    } finally {
      setBackup('idle')
    }
  }

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

      <section aria-label="Záloha">
        <h2>Záloha databáze</h2>
        <button className="chip" onClick={runBackup} disabled={backup === 'running'}>
          {backup === 'running' ? 'Zálohuji…' : 'Zálohovat PostgreSQL'}
        </button>
        {backupNote && (
          <p className="muted" role="status">
            {backupNote}
          </p>
        )}
        <p className="muted">
          Zálohuje <strong>PostgreSQL</strong> — věčný OI archiv, setupy s výsledky, signály,
          tendence a statistiky modelu. Ta data se nedají znovu pořídit a na rozdíl od parquetů
          neleží ve složce projektu, ale v Docker volume, který se smazáním kontejnerů dá ztratit.
        </p>
        <p className="muted">
          <strong>Kam ukládat:</strong> zvol složku <em>mimo adresář projektu</em>, ideálně na jiný
          disk nebo do cloudu. Záloha v repozitáři chrání jen před smazáním volume — ne před
          selháním disku ani ztrátou počítače, protože leží na tomtéž místě. Navíc obsahuje veškerá
          data a do gitu nepatří.
        </p>
      </section>

      <section aria-label="Alerty">
        <h2>Alerty</h2>
        <label>
          <input
            type="checkbox"
            checked={values.subscription_alert_enabled !== false}
            onChange={(event) => put('subscription_alert_enabled', event.target.checked)}
          />
          Hlásit chyby subskripce market dat
        </label>
        <p className="muted">
          Alert, když TWS opakovaně odmítne data konkrétního kontraktu (error 354). S platnými
          subskripcemi ES/NQ by nastat neměl — ojedinělé výpadky farmy se nehlásí.
        </p>
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
