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
import { requestReconnect, useServerSettings } from '../api/settings'
import type { ReconnectTarget } from '../api/settings'
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

// Stavy křížové kontroly IBKR × tasty (#517 A). Text říká, co dělat, ne jen
// jak se stav jmenuje — „quiet" sám o sobě uživateli nic neřekne.
const CROSSCHECK_LABELS: Record<string, string> = {
  ok: 'oba zdroje dodávají data',
  ibkr_suspect: '⚠ IBKR mlčí, tastytrade data má — problém je na straně IBKR',
  tasty_suspect: 'tastytrade zaostává (sekundární zdroj)',
  quiet: 'oba zdroje ticho — tichý trh, ne porucha',
  insufficient: 'málo kontraktů na výrok',
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
    help: 'Po kolika dnech noční úklid maže dopočitatelné řady (ticks/). Snapshoty a odvozené řady se od ADR-0029 nemažou nikdy — jsou to učicí data samoučící smyčky (#794); OI archiv, setupy a signály jakbysmet.',
  },
  {
    key: 'disk_limit_gb',
    label: 'Disk limit (GB)',
    // Default zrcadlí engine (ADR-0029: 5 → 20 GB, keep-forever režim)
    fallback: 20,
    min: 0.5,
    max: 1000,
    help: 'Nad tímto obsazením složky s daty přijde alert. Není to tvrdý strop — data se dál zapisují, jen upozorní. Keep-forever režim (ADR-0029) roste ~6 GB/rok; alert je signál k revizi (komprese starých partic / větší disk), volné místo na disku hlídá zvlášť #773.',
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
  const [reconnect, setReconnect] = useState<{ target: ReconnectTarget; note: string } | null>(null)

  // Ruční přepojení (#950). Potvrzení je nutné: přepojení je ~1–2 min díra
  // ve sběru, takže se nesmí spustit omylem během seance.
  const askReconnect = (target: ReconnectTarget, label: string) => {
    if (
      !window.confirm(`Přepojit ${label}?

Sběr dat se na ~1–2 minuty přeruší.`)
    )
      return
    setReconnect({ target, note: 'vyžádáno…' })
    void requestReconnect(target)
      .then(() =>
        setReconnect({
          target,
          note: `vyžádáno v ${new Date().toLocaleTimeString('cs-CZ')} — engine se přepojí do minuty`,
        }),
      )
      .catch((error: unknown) =>
        setReconnect({
          target,
          note: error instanceof Error ? error.message : 'přepojení selhalo',
        }),
      )
  }

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
                  {/* Délka výpadku IBKR (#770) — pole chybí, když spojení drží */}
                  {status.connection_offline_for_s != null
                    ? ` · bez spojení ${Math.round(status.connection_offline_for_s / 60)} min`
                    : ''}
                </td>
              </tr>
              <tr>
                <td>Účet</td>
                <td>
                  {status.account ?? '—'}
                  {status.account_paper ? ' (paper)' : ''}
                </td>
              </tr>
              {/* Který zdroj právě platí (#950) — u tlačítka Přepojit je to ta
                  informace, podle které se uživatel rozhoduje. Dosud byla jen
                  v chipu v hlavičce a v /status. */}
              <tr>
                <td>Zdroj dat</td>
                <td>
                  řetěz {status.chain_source ?? '—'} · spot {status.spot_source ?? '—'}
                  {status.spot_source === 'tasty' || status.chain_source === 'tasty'
                    ? ' — běží fallback na tastytrade (#614)'
                    : ''}
                </td>
              </tr>
              <tr>
                <td>Přepojení</td>
                <td>
                  <button
                    type="button"
                    className="secondary"
                    data-testid="reconnect-ibkr"
                    onClick={() => askReconnect('ibkr', 'IBKR')}
                  >
                    Přepojit IBKR
                  </button>
                  {reconnect?.target === 'ibkr' && (
                    <span className="muted"> · {reconnect.note}</span>
                  )}
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
              {/* Chyby subskripce (#772): okno dává měřítko, kumulativ trend;
                  „přechod seance" jsou očekávané nárazy resubskripce o půlnoci */}
              <tr>
                <td>Chyby subskripce</td>
                <td>
                  {status.subscription_errors != null
                    ? `${status.subscription_errors_60m ?? 0} za hodinu · ${
                        status.subscription_errors
                      } od startu${
                        (status.subscription_errors_excused ?? 0) > 0
                          ? ` (z toho ${status.subscription_errors_excused} přechod seance)`
                          : ''
                      }`
                    : '—'}
                  {status.subscription_error_recent?.length ? (
                    <details className="subscription-errors">
                      <summary className="muted">
                        poslední záznamy ({status.subscription_error_recent.length})
                      </summary>
                      <pre aria-label="Poslední chyby subskripce">
                        {status.subscription_error_recent
                          .slice()
                          .reverse()
                          .map(
                            (rec) =>
                              `${new Date(rec.ts).toLocaleTimeString('cs-CZ')} ${rec.contract}`,
                          )
                          .join('\n')}
                      </pre>
                    </details>
                  ) : null}
                </td>
              </tr>
              {/* Křížová kontrola feedů (#517 A): chybějící pole = neměří se
                  (shadow neběží) — to je jiný stav než „měří se a je ticho" */}
              <tr>
                <td>Křížová kontrola feedů</td>
                <td title={status.feed_crosscheck_detail ?? undefined}>
                  {status.feed_crosscheck != null
                    ? (CROSSCHECK_LABELS[status.feed_crosscheck] ?? status.feed_crosscheck)
                    : 'neměří se (shadow neběží)'}
                </td>
              </tr>
            </tbody>
          </table>
        </SettingRow>
        {/* Tastytrade větev (#706): protějšek IBKR stavu — od #614 fáze 2 je
            tasty skutečný zdroj (spot/chain fallback, OI fill), ne jen měření.
            Chybějící pole = větev neběží (bez tajemství / vypnutá). */}
        {status.tasty_connected != null && (
          <SettingRow help="Stav tastytrade větve (#706): DXLink spojení, subskripce dxFeed a pokrytí polí. Zdroj řetězu a spotu při fallbacku ukazuje řádek výš (#614); záznam printů plní učicí data (#795). Jen ke čtení.">
            <table className="settings-status" data-testid="tasty-status">
              <tbody>
                <tr>
                  <td>Tastytrade</td>
                  <td>
                    {status.tasty_connected ? 'připojeno' : 'ODPOJENO'}
                    {(status.tasty_reconnects ?? 0) > 0
                      ? ` · ${status.tasty_reconnects} reconnectů`
                      : ''}
                    {status.tasty_last_event_ts
                      ? ` · poslední event ${new Date(status.tasty_last_event_ts).toLocaleTimeString('cs-CZ')}`
                      : ''}
                  </td>
                </tr>
                <tr>
                  <td>Subskripce</td>
                  <td>
                    {status.tasty_symbols ?? '—'} symbolů · quotes {status.tasty_quotes ?? '—'} ·
                    greeks {status.tasty_greeks ?? '—'} · OI {status.tasty_oi ?? '—'}
                  </td>
                </tr>
                <tr>
                  <td>Přepojení</td>
                  <td>
                    <button
                      type="button"
                      className="secondary"
                      data-testid="reconnect-tasty"
                      onClick={() => askReconnect('tasty', 'tastytrade (DXLink)')}
                    >
                      Přepojit tastytrade
                    </button>
                    {reconnect?.target === 'tasty' && (
                      <span className="muted"> · {reconnect.note}</span>
                    )}
                  </td>
                </tr>
                <tr>
                  <td>Trade printy</td>
                  <td>
                    {status.tasty_trades ?? '—'} přijato
                    {status.tasty_trades_recorded != null
                      ? ` · ${status.tasty_trades_recorded} zaznamenáno (#795)`
                      : ''}
                  </td>
                </tr>
                {/* Greeks validátor (#614): podíl párů nad měřeným prahem 2× p95;
                    za normálu ~1–2 %, alert greeks_suspect od >20 % po 3 min */}
                {status.tasty_greeks_mismatch &&
                Object.keys(status.tasty_greeks_mismatch).length > 0 ? (
                  <tr>
                    <td>Greeks validátor</td>
                    <td>
                      {Object.entries(status.tasty_greeks_mismatch)
                        .map(([symbol, share]) => `${symbol} ${(share * 100).toFixed(1)} %`)
                        .join(' · ')}{' '}
                      <span className="muted">nad prahem (norma ~1–2 %)</span>
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </SettingRow>
        )}
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
