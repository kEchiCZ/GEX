/** Zdroje zpráv v záložce News (#578 rozšíření, 27. 8. 2026).

Dvě části: audit registru (co má téct vs. co reálně teče, přepínač enabled)
a uživatelské seznamy — Bluesky kurátoři (handle/DID), Reddit subreddity a
vlastní RSS feedy. Seznamy jsou plně editovatelné včetně MAZÁNÍ defaultů:
news-engine je seeduje jen chybí-li klíč, uložená verze uživatele platí.

Propagace změn: Bluesky kurátory si news-engine přenačítá za běhu (~10 min);
subreddity, vlastní RSS a vypnutí zdroje se čtou při startu → po restartu
news-engine. UI to říká u každého ovládacího prvku, žádné tiché dojmy.
*/
import { useEffect, useState } from 'react'
import { fetchNewsSources, patchNewsSource } from '../api/news'
import type { NewsSourceRow } from '../api/news'
import { fetchSettings, putSetting } from '../api/settings'

const SOURCE_LABELS: Record<string, string> = {
  forexfactory: 'ForexFactory kalendář',
  fed_rss: 'Fed RSS',
  rss_news: 'Agenturní RSS (CNBC/MW/Yahoo)',
  ibkr: 'IBKR páska',
  alpaca: 'Alpaca (Benzinga)',
  finnhub: 'Finnhub',
  bluesky: 'Bluesky Jetstream',
  reddit_rss: 'Reddit RSS',
  rss_user: 'Vlastní RSS feedy',
}

const TIER_LABELS: Record<string, string> = {
  core: 'jádro',
  extra: 'doplněk',
  test: 'testovací',
}

/** Položka seznamu (#918): prefix `#` v uložené hodnotě = vypnutá.

Konvence komentáře drží tvar settings (seznam řetězců) i validaci API beze
změny; news-engine položky s `#` přeskakuje v `read_list_setting`.
*/
interface ListItem {
  value: string
  enabled: boolean
}

function parseItems(stored: string[]): ListItem[] {
  return stored
    .map((raw) => raw.trim())
    .filter((raw) => raw !== '')
    .map((raw) =>
      raw.startsWith('#')
        ? { value: raw.replace(/^#+\s*/, ''), enabled: false }
        : { value: raw, enabled: true },
    )
    .filter((item) => item.value !== '')
}

function serializeItems(items: ListItem[]): string[] {
  return items.map((item) => (item.enabled ? item.value : `#${item.value}`))
}

function ListEditor({
  label,
  settingKey,
  placeholder,
  hint,
  stored,
}: {
  label: string
  settingKey: string
  placeholder: string
  hint: string
  stored: string[]
}) {
  const [items, setItems] = useState<ListItem[]>(parseItems(stored))
  const [draft, setDraft] = useState('')
  const [edited, setEdited] = useState(false)
  const [state, setState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  // Serverová data dorazila později (první načtení) — needitovaný obsah se sladí.
  // Po první editaci je autoritativní lokální stav: refetch po uložení může
  // dorazit opožděně a stale odpověď by tiše vrátila starší verzi seznamu.
  useEffect(() => {
    if (!edited) setItems(parseItems(stored))
  }, [stored, edited])

  // Každá změna se ukládá hned — checkbox s odloženým „Uložit" sváděl
  // k zapomenutému stavu jen v prohlížeči
  const save = (next: ListItem[]) => {
    setItems(next)
    setEdited(true)
    setState('saving')
    putSetting(settingKey, serializeItems(next))
      .then(() => setState('saved'))
      .catch(() => setState('error'))
  }

  const add = () => {
    const value = draft.trim()
    if (value === '' || items.some((item) => item.value === value)) return
    setDraft('')
    save([...items, { value, enabled: true }])
  }

  return (
    <div className="news-source-editor">
      <span title={hint} className="news-source-editor-label">
        {label}
      </span>
      <ul className="news-source-items" aria-label={label}>
        {items.map((item) => (
          <li key={item.value} className={item.enabled ? '' : 'news-source-item-off'}>
            <label>
              <input
                type="checkbox"
                checked={item.enabled}
                aria-label={`${label}: ${item.value} aktivní`}
                onChange={(event) =>
                  save(
                    items.map((other) =>
                      other.value === item.value
                        ? { ...other, enabled: event.target.checked }
                        : other,
                    ),
                  )
                }
              />
              <span>{item.value}</span>
            </label>
            <button
              type="button"
              className="news-source-item-remove"
              aria-label={`Smazat ${item.value}`}
              title={
                'Smazat položku.\nSmazané defaulty se už nevracejí — na dočasné odstavení použij checkbox.'
              }
              onClick={() => save(items.filter((other) => other.value !== item.value))}
            >
              ✕
            </button>
          </li>
        ))}
        {items.length === 0 && <li className="muted">žádné položky</li>}
      </ul>
      <div className="news-source-editor-actions">
        <input
          type="text"
          value={draft}
          placeholder={placeholder}
          aria-label={`${label}: nová položka`}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') add()
          }}
        />
        <button type="button" onClick={add} disabled={draft.trim() === ''}>
          Přidat
        </button>
        {state === 'saving' && <span className="muted">ukládám…</span>}
        {state === 'saved' && <span className="muted">uloženo</span>}
        {state === 'error' && <span className="news-source-error">uložení selhalo</span>}
      </div>
    </div>
  )
}

export function NewsSourcesSection() {
  const [sources, setSources] = useState<NewsSourceRow[]>([])
  const [lists, setLists] = useState<Record<string, string[]>>({})

  const load = () => {
    void fetchNewsSources().then(setSources)
    const applySettings = (settings: Record<string, unknown>) => {
      const listOf = (key: string) => {
        const value = settings[key]
        return Array.isArray(value)
          ? value.filter((item): item is string => typeof item === 'string')
          : []
      }
      setLists({
        news_bluesky_authors: listOf('news_bluesky_authors'),
        news_reddit_subreddits: listOf('news_reddit_subreddits'),
        news_rss_extra: listOf('news_rss_extra'),
      })
    }
    // API nedostupné → editory zůstanou prázdné, sekce se nerozbije
    void fetchSettings()
      .then(applySettings)
      .catch(() => {})
  }
  useEffect(load, [])

  const toggle = (source: string, enabled: boolean) => {
    void patchNewsSource(source, enabled).then((ok) => {
      if (ok)
        setSources((rows) => rows.map((row) => (row.source === source ? { ...row, enabled } : row)))
    })
  }

  if (sources.length === 0) return null

  return (
    <div className="news-sources" aria-label="Zdroje zpráv">
      <h3>Zdroje zpráv</h3>
      <table className="news-sources-table">
        <thead>
          <tr>
            <th>Zdroj</th>
            <th>Tier</th>
            <th
              title={
                'Počet událostí za dnešek / denní průměr za 7 dní.\n' +
                'Nula u zapnutého zdroje = zdroj neteče — zkontrolovat.'
              }
            >
              Dnes / Ø den
            </th>
            <th
              title={
                'Podíl událostí s důležitostí ≥ 2 a skóre.\n' +
                'Nízký podíl = zdroj sype hlavně balast.'
              }
            >
              Významné
            </th>
            <th>Poslední</th>
            <th
              title={
                'Vypnutý zdroj se při startu news-engine vůbec nespustí.\n' +
                'Změna se projeví po restartu news-engine.'
              }
            >
              Aktivní
            </th>
          </tr>
        </thead>
        <tbody>
          {sources.map((row) => (
            <tr key={row.source} data-testid={`news-source-${row.source}`}>
              <td title={row.notes ?? undefined}>{SOURCE_LABELS[row.source] ?? row.source}</td>
              <td className="muted">{TIER_LABELS[row.tier] ?? row.tier}</td>
              <td>
                {row.events_today} / {row.daily_avg}
              </td>
              <td>
                {row.significant_share !== null
                  ? `${Math.round(100 * row.significant_share)} %`
                  : '—'}
              </td>
              <td className="muted">
                {row.last_event_ts !== null
                  ? new Date(row.last_event_ts).toLocaleString('cs-CZ', {
                      day: 'numeric',
                      month: 'numeric',
                      hour: '2-digit',
                      minute: '2-digit',
                    })
                  : '—'}
              </td>
              <td>
                <input
                  type="checkbox"
                  checked={row.enabled}
                  aria-label={`Zdroj ${SOURCE_LABELS[row.source] ?? row.source} aktivní`}
                  onChange={(event) => toggle(row.source, event.target.checked)}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="news-sources-editors">
        <ListEditor
          label="Bluesky kurátoři"
          settingKey="news_bluesky_authors"
          placeholder="handle nebo did:plc:…"
          hint={
            'Autoři, jejichž KAŽDÝ post se bere (handle nebo did:…).\n' +
            'Checkbox položku dočasně vypne (vratné); ✕ ji smaže —\n' +
            'smazané defaulty se už nevracejí.\n' +
            'Projeví se za běhu do ~10 minut (bez restartu).'
          }
          stored={lists.news_bluesky_authors ?? []}
        />
        <ListEditor
          label="Reddit subreddity"
          settingKey="news_reddit_subreddits"
          placeholder="subreddit (bez r/)"
          hint={
            'Subreddity pro nativní RSS (bez r/).\n' +
            'Checkbox položku dočasně vypne (vratné); ✕ ji smaže.\n' +
            'Projeví se po restartu news-engine.'
          }
          stored={lists.news_reddit_subreddits ?? []}
        />
        <ListEditor
          label="Vlastní RSS feedy"
          settingKey="news_rss_extra"
          placeholder="https://example.com/feed.xml"
          hint={
            'Libovolné RSS/Atom feedy (plná URL).\n' +
            'Checkbox položku dočasně vypne (vratné); ✕ ji smaže.\n' +
            'V auditu se hlásí jako „Vlastní RSS feedy".\n' +
            'Projeví se po restartu news-engine.'
          }
          stored={lists.news_rss_extra ?? []}
        />
      </div>
      <p className="muted">
        Bluesky kurátoři se načítají za běhu (~10 min); subreddity, vlastní feedy a vypnutí zdroje
        se čtou při startu news-engine.
      </p>
    </div>
  )
}
