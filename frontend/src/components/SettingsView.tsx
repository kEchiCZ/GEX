/** Settings (SPEC 7.5): konfigurace enginu a UI.

Změny se drží v konceptu a odesílají až tlačítkem **Uložit** (#445) — dřív se
každý stisk klávesy propsal rovnou na server, takže rozepsaná hodnota (např. „1"
při psaní „150") stihla dojet do enginu. Neuložený koncept se opuštěním
obrazovky nebo refreshem zahodí a platí původní hodnoty.

Výjimka je téma: aplikuje se i ukládá okamžitě, protože jde o čistě vizuální
volbu s okamžitou zpětnou vazbou (AC #167) — čekat u něj na Uložit by mátlo.
*/
import { useEffect, useState } from 'react'
import { loadApiToken, saveApiToken } from '../api/apiToken'
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
  const {
    theme,
    setTheme,
    status,
    consoleLog,
    tradersMode,
    setTradersMode,
    riskAccountUsd,
    setRiskAccountUsd,
    riskPct,
    setRiskPct,
  } = useAppState()
  const { values, put, saveAll } = useServerSettings()
  // Rozepsané změny; prázdný objekt = nic k uložení
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [backup, setBackup] = useState<'idle' | 'running'>('idle')
  const [backupNote, setBackupNote] = useState<string | null>(null)
  // Token se neukládá na server (je to sdílené tajemství z .env), jen do prohlížeče
  const [apiToken, setApiToken] = useState(() => loadApiToken())
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [saveError, setSaveError] = useState<string | null>(null)

  const dirtyKeys = Object.keys(draft)
  const value = (key: string, fallback: unknown): unknown =>
    key in draft ? draft[key] : (values[key] ?? fallback)
  const edit = (key: string, next: unknown) => {
    setSaveState('idle')
    setDraft((previous) => ({ ...previous, [key]: next }))
  }
  const save = () => {
    const entries = Object.entries(draft)
    setSaveState('saving')
    setSaveError(null)
    void saveAll(entries)
      .then(() => {
        setDraft({})
        setSaveState('saved')
      })
      .catch((error: unknown) => {
        // Koncept se NEmaže — uživatel musí mít co zkusit znovu
        setSaveError(error instanceof Error ? error.message : 'Uložení selhalo.')
        setSaveState('error')
      })
  }
  const discard = () => {
    setDraft({})
    setSaveState('idle')
  }

  // Potvrzení samo zmizí. Trvalé „Uloženo" splyne s klidovým stavem, takže se
  // po druhém uložení nedá poznat, jestli se něco stalo.
  useEffect(() => {
    if (saveState !== 'saved') return
    const timer = setTimeout(() => setSaveState('idle'), 3000)
    return () => clearTimeout(timer)
  }, [saveState])

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

      {/* Náhrada IBKR Console (#705): read-only stav + události v prohlížeči.
          Editace host/port/client ID je výš — S konceptem a Uložit (#445),
          Console je obcházela a posílala enginu rozepsané hodnoty. */}
      <section aria-label="Stav enginu">
        <h2>Stav enginu</h2>
        <SettingRow help="Živý stav pipeline — totéž, co stavová lišta dole, pohromadě u nastavení. Jen ke čtení.">
          <table className="settings-status" data-testid="engine-status">
            <tbody>
              <tr>
                <td>Spojení</td>
                <td>
                  {status.engine === 'online' ? 'online' : (status.engine ?? '—')}
                  {status.connection ? ` · ${status.connection}` : ''}
                  {status.port ? ` · port ${status.port}` : ''}
                </td>
              </tr>
              <tr>
                <td>Účet</td>
                <td>
                  {status.account ?? '—'}
                  {status.account_paper ? ' (paper)' : ''}
                </td>
              </tr>
              <tr>
                <td>Greeks</td>
                <td>
                  {status.greeks_complete ?? '—'}/{status.greeks_total ?? '—'} · repair fronta{' '}
                  {status.repair_count ?? '—'}
                </td>
              </tr>
              <tr>
                <td>OI řetězu</td>
                <td>
                  {status.oi_present != null && status.oi_filled != null
                    ? `${status.oi_present + status.oi_filled}/${
                        status.oi_present + status.oi_filled + (status.oi_missing ?? 0)
                      }${status.oi_filled > 0 ? ` · ${status.oi_filled} z tastytrade` : ''}`
                    : '—'}
                </td>
              </tr>
              <tr>
                <td>Market data lines</td>
                <td>
                  {status.lines_utilization != null
                    ? `${Math.round(status.lines_utilization * 100)} %`
                    : '—'}
                </td>
              </tr>
            </tbody>
          </table>
        </SettingRow>
        <details className="settings-events">
          <summary className="muted">
            Poslední události (jen tento prohlížeč — po obnovení stránky se maže; serverové logy
            jsou v kontejnerech)
          </summary>
          <pre aria-label="Log API událostí">{consoleLog.join('\n') || '—'}</pre>
        </details>
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
        <SettingRow
          help={
            <>
              Sdílené tajemství <code>GEXLENS_API_TOKEN</code> z lokálního <code>.env</code> (#542).
              Bez něj API zálohu odmítne — dump nese celý archiv, takže nesmí být ke stažení bez
              ověření. Zůstává jen v tomhle prohlížeči, na server se neukládá.
            </>
          }
        >
          <label>
            API token
            <input
              type="password"
              value={apiToken}
              autoComplete="off"
              onChange={(event) => {
                setApiToken(event.target.value)
                saveApiToken(event.target.value)
              }}
            />
          </label>
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

      <section aria-label="Trading">
        <h2>Trading</h2>
        <SettingRow help="Traders mode (#627): zapíná vrstvy pro aktivní obchodování — teď značky deníku ✎ na časové ose grafu, postupně přibudou referenční úrovně. Ukládá se okamžitě, jen v tomto prohlížeči.">
          <label>
            <input
              type="checkbox"
              checked={tradersMode}
              onChange={(event) => setTradersMode(event.target.checked)}
            />
            Traders mode — trading vrstvy v grafu
          </label>
        </SettingRow>
        <SettingRow help="Kalkulačka velikosti pozice (#679) u karty setupu: riziko = účet × %, kontrakty = riziko / (stop v bodech × hodnota bodu), vč. micro variant (MES/MNQ). Ukládá se jen v prohlížeči, na server nikdy neodchází.">
          <label>
            Velikost účtu (USD)
            <input
              type="number"
              min={0}
              step={100}
              value={riskAccountUsd}
              onChange={(event) => setRiskAccountUsd(Number(event.target.value) || 0)}
              aria-label="Velikost účtu v USD"
            />
          </label>
          <label>
            Riziko na obchod (%)
            <input
              type="number"
              min={0}
              max={100}
              step={0.25}
              value={riskPct}
              onChange={(event) => setRiskPct(Number(event.target.value) || 0)}
              aria-label="Riziko na obchod v procentech"
            />
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
        <button
          className="chip active"
          onClick={save}
          disabled={dirtyKeys.length === 0 || saveState === 'saving'}
        >
          Uložit
        </button>
        <button
          className="chip"
          onClick={discard}
          disabled={dirtyKeys.length === 0 || saveState === 'saving'}
        >
          Zahodit změny
        </button>
        {/* Pořadí větví: chyba a průběh mají přednost před „Neuloženo" — po
        neúspěchu koncept zůstává, takže by ho jinak přebilo. */}
        <span className="muted" role="status">
          {saveState === 'error' ? (
            <span className="save-error">
              Uložení selhalo: {saveError} Změny zůstávají rozepsané.
            </span>
          ) : saveState === 'saving' ? (
            'Ukládám…'
          ) : dirtyKeys.length > 0 ? (
            <span className="dirty">
              Neuloženo: {dirtyKeys.length}{' '}
              {dirtyKeys.length === 1 ? 'změna' : dirtyKeys.length < 5 ? 'změny' : 'změn'} — bez
              uložení se při odchodu zahodí.
            </span>
          ) : saveState === 'saved' ? (
            <span className="save-ok">✓ Uloženo</span>
          ) : (
            'Žádné neuložené změny.'
          )}
        </span>
      </div>
    </main>
  )
}
