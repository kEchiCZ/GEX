/** Settings (SPEC 7.5): konfigurace enginu a UI.

Změny se drží v konceptu a odesílají až tlačítkem **Uložit** (#445) — dřív se
každý stisk klávesy propsal rovnou na server, takže rozepsaná hodnota (např. „1"
při psaní „150") stihla dojet do enginu. Neuložený koncept se opuštěním
obrazovky nebo refreshem zahodí a platí původní hodnoty.

Výjimka je téma: aplikuje se i ukládá okamžitě, protože jde o čistě vizuální
volbu s okamžitou zpětnou vazbou (AC #167) — čekat u něj na Uložit by mátlo.
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
    help: 'Jak široké pásmo strikes kolem spotu se sbírá. Víc bodů = vidíš vzdálená křídla (pojistky hluboko OTM), ale roste počet kontraktů a tím zátěž subskripcí.',
  },
  {
    key: 'batch_size',
    label: 'Velikost dávky',
    fallback: 80,
    min: 10,
    max: 100,
    restarts: true,
    help: 'Kolik kontraktů se najednou přihlásí k odběru kotací. Nezvyšuj nad 100 — účet má ověřenou kapacitu ~150 souběžných market data lines (ADR-0001) a rezerva patří hot zóně a podkladu.',
  },
  {
    key: 'hot_zone_width',
    label: 'Šířka hot zóny (± strikes)',
    fallback: 15,
    min: 1,
    max: 50,
    restarts: true,
    help: 'Kolik strikes kolem ATM se klasifikuje tick-by-tick (nejpřesnější čtení agresora). Účet zvládne jen 5 souběžných streamů, takže se zóna stejně ořízne od ATM ven — zbytek jede přes midpoint test.',
  },
  {
    key: 'retention_days',
    label: 'Retence (dny)',
    fallback: 90,
    min: 1,
    max: 3650,
    help: 'Po kolika dnech noční úklid maže snapshoty a odvozené řady. Delší okno = starší dny jdou prohlížet a přehrávat, ale roste místo na disku (~17 MB/den pro ES+NQ). OI archiv, setupy a signály se nemažou nikdy.',
  },
  {
    key: 'disk_limit_gb',
    label: 'Disk limit (GB)',
    fallback: 5,
    min: 0.5,
    max: 1000,
    help: 'Nad tímto obsazením složky s daty přijde alert. Není to tvrdý strop — data se dál zapisují, jen upozorní. Drž ho nad očekávaným obsazením při zvolené retenci.',
  },
]

const DEFAULT_SESSIONS = [
  { label: 'London', minuteIdx: 60 },
  { label: 'New York', minuteIdx: 210 },
]

/** Parametr vlevo, nápověda vpravo (#445). */
function SettingRow({ children, help }: { children: React.ReactNode; help: React.ReactNode }) {
  return (
    <div className="setting-row">
      {children}
      <p className="muted setting-help">{help}</p>
    </div>
  )
}

export function SettingsView() {
  const { theme, setTheme, status } = useAppState()
  const { values, put } = useServerSettings()
  // Rozepsané změny; prázdný objekt = nic k uložení
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [backup, setBackup] = useState<'idle' | 'running'>('idle')
  const [backupNote, setBackupNote] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const dirtyKeys = Object.keys(draft)
  const value = (key: string, fallback: unknown): unknown =>
    key in draft ? draft[key] : (values[key] ?? fallback)
  const edit = (key: string, next: unknown) => {
    setSaved(false)
    setDraft((previous) => ({ ...previous, [key]: next }))
  }
  const save = () => {
    for (const [key, next] of Object.entries(draft)) put(key, next)
    setDraft({})
    setSaved(true)
  }
  const discard = () => {
    setDraft({})
    setSaved(false)
  }

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
      // DOMException nemusí být instanceof Error — AbortError se pozná podle name
      const isAbort =
        typeof error === 'object' &&
        error !== null &&
        (error as { name?: unknown }).name === 'AbortError'
      if (isAbort) {
        // Zrušení dialogu „Uložit jako" není chyba zálohy — žádná hláška (#506)
      } else {
        // Chyba musí být vidět: tichá neúspěšná záloha je horší než žádná
        setBackupNote(error instanceof Error ? error.message : 'Záloha selhala.')
      }
    } finally {
      setBackup('idle')
    }
  }

  return (
    <main className="settings" aria-label="Settings">
      <section aria-label="IBKR">
        <h2>IBKR</h2>
        <SettingRow
          help={
            <>
              Adresa, kde běží TWS / IB Gateway. V Dockeru se na hostitele odkazuje přes{' '}
              <code>host.docker.internal</code>.
            </>
          }
        >
          <label>
            Host
            <input
              value={String(value('ibkr_host', '127.0.0.1'))}
              onChange={(event) => edit('ibkr_host', event.target.value)}
            />
          </label>
        </SettingRow>
        <SettingRow help="7496 = TWS živý účet, 7497 = TWS paper, 4001 / 4002 = IB Gateway (živý / paper). Musí odpovídat portu v TWS → Global Configuration → API → Settings.">
          <label>
            Port
            <input
              type="number"
              value={Number(value('ibkr_port', 7496))}
              onChange={(event) => edit('ibkr_port', Number(event.target.value))}
            />
          </label>
        </SettingRow>
        <SettingRow help="Odlišuje připojené aplikace. Každá musí mít vlastní číslo (0–999) — se stejným ID by engine odpojil jinou aplikaci připojenou na TWS. Měň jen při konfliktu.">
          <label>
            Client ID
            <input
              type="number"
              value={Number(value('ibkr_client_id', 1))}
              onChange={(event) => edit('ibkr_client_id', Number(event.target.value))}
            />
          </label>
        </SettingRow>
        <p className="muted">
          Po uložení se engine sám přepojí — restartovat ho není potřeba. Přepojení na pár sekund
          přeruší sběr dat.
        </p>
        <p className="muted" data-testid="account-info">
          {status.engine === 'online' && status.account ? (
            <>
              Připojeno k účtu <strong>{status.account}</strong>.{' '}
              {status.account_paper === true
                ? 'Paper účet — data jsou reálná, obchody nikoli.'
                : status.account_paper === false
                  ? 'ŽIVÝ účet.'
                  : ''}
            </>
          ) : (
            'Účet neznámý — engine není připojený k TWS.'
          )}
        </p>
      </section>

      <section aria-label="Engine">
        <h2>Engine</h2>
        {ENGINE_FIELDS.map((field) => (
          <SettingRow
            key={field.key}
            help={
              <>
                {field.help}{' '}
                <em>
                  Rozsah {field.min}–{field.max}, výchozí {field.fallback}.
                  {field.restarts ? ' Změna nakrátko přeruší sběr dat instrumentu.' : ''}
                </em>
              </>
            }
          >
            <label>
              {field.label}
              <input
                type="number"
                min={field.min}
                max={field.max}
                value={Number(value(field.key, field.fallback))}
                onChange={(event) => edit(field.key, Number(event.target.value))}
              />
            </label>
          </SettingRow>
        ))}
        <p className="muted">
          Uložené změny si engine přebírá do 5 minut. Hodnotu mimo povolený rozsah srovná na
          nejbližší povolenou — nerozbiješ tím sběr dat.
        </p>
      </section>

      <section aria-label="Záloha">
        <h2>Záloha databáze</h2>
        <SettingRow
          help={
            <>
              Zálohuje <strong>PostgreSQL</strong> — věčný OI archiv, setupy s výsledky, signály,
              tendence a statistiky modelu. Ta data se nedají znovu pořídit a na rozdíl od parquetů
              neleží ve složce projektu, ale v Docker volume, který se smazáním kontejnerů dá
              ztratit. <strong>Kam ukládat:</strong> složku <em>mimo adresář projektu</em>, ideálně
              na jiný disk nebo do cloudu — záloha v repozitáři neleží nikde jinde než originál a do
              gitu nepatří.
            </>
          }
        >
          <button className="chip" onClick={runBackup} disabled={backup === 'running'}>
            {backup === 'running' ? 'Zálohuji…' : 'Zálohovat PostgreSQL'}
          </button>
        </SettingRow>
        {backupNote && (
          <p className="muted" role="status">
            {backupNote}
          </p>
        )}
      </section>

      <section aria-label="Alerty">
        <h2>Alerty</h2>
        <SettingRow help="Alert, když TWS opakovaně odmítne data konkrétního kontraktu (error 354). S platnými subskripcemi ES/NQ by nastat neměl — ojedinělé výpadky farmy se nehlásí.">
          <label>
            <input
              type="checkbox"
              checked={value('subscription_alert_enabled', true) !== false}
              onChange={(event) => edit('subscription_alert_enabled', event.target.checked)}
            />
            Hlásit chyby subskripce market dat
          </label>
        </SettingRow>
      </section>

      <section aria-label="Vzhled">
        <h2>Vzhled</h2>
        <SettingRow help="Aplikuje se i ukládá okamžitě, bez tlačítka Uložit — je to čistě vizuální volba a čekání by u ní mátlo.">
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
        </SettingRow>
        <SettingRow help="Jazyk textů v aplikaci.">
          <label>
            Jazyk
            <select
              value={String(value('language', 'cs'))}
              onChange={(event) => edit('language', event.target.value)}
            >
              <option value="cs">Čeština</option>
              <option value="en">English</option>
            </select>
          </label>
        </SettingRow>
      </section>

      <section aria-label="Seance">
        <h2>Seance</h2>
        <SettingRow help="Historické pole — markery seancí se generují automaticky z časů světových burz, tohle už se nepoužívá. Nevalidní JSON se neuloží.">
          <label>
            Seznam seancí (JSON)
            {/* defaultValue se vyhodnotí při prvním renderu, kdy fetch settings
                ještě běží — klíč remountne textarea, jakmile se platná hodnota
                (server / koncept / zahození konceptu) změní (#505) */}
            <textarea
              rows={3}
              key={JSON.stringify(value('sessions', DEFAULT_SESSIONS))}
              defaultValue={JSON.stringify(value('sessions', DEFAULT_SESSIONS))}
              onBlur={(event) => {
                try {
                  const parsed: unknown = JSON.parse(event.target.value)
                  // Beze změny žádný koncept — klik dovnitř a ven by jinak označil
                  // „Neuloženo" a Uložit by zapsal defaulty přes serverovou hodnotu (#505)
                  if (
                    JSON.stringify(parsed) !== JSON.stringify(value('sessions', DEFAULT_SESSIONS))
                  ) {
                    edit('sessions', parsed)
                  }
                } catch {
                  // Nevalidní JSON se do konceptu nedostane — pole zůstává k opravě
                }
              }}
            />
          </label>
        </SettingRow>
      </section>

      <div className="settings-actions">
        <button className="chip active" onClick={save} disabled={dirtyKeys.length === 0}>
          Uložit
        </button>
        <button className="chip" onClick={discard} disabled={dirtyKeys.length === 0}>
          Zahodit změny
        </button>
        <span className="muted" role="status">
          {dirtyKeys.length > 0 ? (
            <span className="dirty">
              Neuloženo: {dirtyKeys.length}{' '}
              {dirtyKeys.length === 1 ? 'změna' : dirtyKeys.length < 5 ? 'změny' : 'změn'} — bez
              uložení se při odchodu zahodí.
            </span>
          ) : saved ? (
            'Uloženo.'
          ) : (
            'Žádné neuložené změny.'
          )}
        </span>
      </div>
    </main>
  )
}
