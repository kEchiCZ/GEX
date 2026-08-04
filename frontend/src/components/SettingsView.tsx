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
  /** K čemu parametr je — uživatel nemá číst kód, aby to zjistil (#438). */
  help: string
  /** Meze, které engine stejně vynutí (`runtime_settings.py`); hodnota mimo ně
      se srovná na nejbližší povolenou, ne zahodí. */
  min: number
  max: number
  /** Změna si vynutí znovupostavení pipeline (na pár sekund vypadnou data). */
  restarts?: boolean
}

// Meze i chování musí odpovídat RUNTIME_SETTINGS v enginu — jinak UI slibuje
// něco jiného, než engine udělá
const ENGINE_FIELDS: NumberField[] = [
  {
    key: 'strike_range_points',
    label: 'Rozsah strikes (± body)',
    fallback: 200,
    min: 50,
    max: 400,
    restarts: true,
    help: 'Jak široké pásmo strikes kolem spotu se sbírá. Víc bodů = vidíš vzdálená křídla (pojistky hluboko OTM), ale roste počet kontraktů a tím i zátěž subskripcí. Strop 400 je polovina denní obálky.',
  },
  {
    key: 'batch_size',
    label: 'Velikost dávky',
    fallback: 80,
    min: 10,
    max: 100,
    restarts: true,
    help: 'Kolik kontraktů se najednou přihlásí k odběru kotací. Nezvyšuj nad 100 — účet má ověřenou kapacitu ~150 souběžných market data lines (ADR-0001) a rezerva patří hot zóně a podkladu. Vyšší hodnota vede na IBKR error 101.',
  },
  {
    key: 'hot_zone_width',
    label: 'Šířka hot zóny (± strikes)',
    fallback: 15,
    min: 1,
    max: 50,
    restarts: true,
    help: 'Kolik strikes kolem ATM se klasifikuje tick-by-tick (nejpřesnější čtení agresora). Účet zvládne jen 5 souběžných tick-by-tick streamů, takže se zóna stejně ořízne od ATM ven — zbytek jede přes midpoint test. Zvyšovat má smysl až po dokoupení Quote Booster packů.',
  },
  {
    key: 'retention_days',
    label: 'Retence (dny)',
    fallback: 90,
    min: 1,
    max: 3650,
    help: 'Po kolika dnech noční úklid maže snapshoty a odvozené řady. Delší okno = můžeš se dívat na starší dny a přehrávat historii, ale roste místo na disku (~17 MB/den pro ES+NQ). OI archiv, setupy a signály se nemažou nikdy bez ohledu na tuto hodnotu.',
  },
  {
    key: 'disk_limit_gb',
    label: 'Disk limit (GB)',
    fallback: 5,
    min: 0.5,
    max: 1000,
    help: 'Nad tímto obsazením složky s daty přijde alert. Není to tvrdý strop — data se dál zapisují, jen upozorní. Drž ho nad očekávaným obsazením při zvolené retenci, jinak bude hlásit planě.',
  },
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
        <p className="muted">
          Parametry spojení na TWS / IB Gateway. Na rozdíl od sekce Engine se{' '}
          <strong>neprojeví za běhu</strong> — spojení se navazuje při startu, takže po změně je
          potřeba restartovat engine (<code>docker compose restart engine</code>).
        </p>
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
        <p className="muted setting-help">
          <em>
            7496 = TWS živý účet, 7497 = TWS paper, 4001 / 4002 = IB Gateway (živý / paper). Musí
            odpovídat portu v TWS → Global Configuration → API → Settings.
          </em>
        </p>
        <label>
          Client ID
          <input
            type="number"
            value={Number(values.ibkr_client_id ?? 1)}
            onChange={(event) => put('ibkr_client_id', Number(event.target.value))}
          />
        </label>
        <p className="muted setting-help">
          <em>
            Odlišuje připojené aplikace. Každá musí mít vlastní číslo (0–999) — se stejným ID by
            engine odpojil jinou aplikaci připojenou na TWS. Měň jen při konfliktu.
          </em>
        </p>
      </section>

      <section aria-label="Engine">
        <h2>Engine</h2>
        <p className="muted">
          Změny si engine přebírá do 5 minut za běhu. Hodnotu mimo povolený rozsah srovná na
          nejbližší povolenou — nerozbiješ tím sběr dat.
        </p>
        {ENGINE_FIELDS.map((field) => (
          <div className="setting-field" key={field.key}>
            <label>
              {field.label}
              <input
                type="number"
                min={field.min}
                max={field.max}
                value={Number(values[field.key] ?? field.fallback)}
                onChange={(event) => put(field.key, Number(event.target.value))}
              />
            </label>
            <p className="muted setting-help">
              {field.help}{' '}
              <em>
                Rozsah {field.min}–{field.max}, výchozí {field.fallback}.
                {field.restarts ? ' Změna nakrátko přeruší sběr dat instrumentu.' : ''}
              </em>
            </p>
          </div>
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
