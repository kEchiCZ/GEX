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

/** Seznam ↔ textarea: řádek = položka; prázdné řádky a mezery se zahazují. */
function parseLines(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
}

function ListEditor({
  label,
  settingKey,
  placeholder,
  hint,
  stored,
  onSaved,
}: {
  label: string
  settingKey: string
  placeholder: string
  hint: string
  stored: string[]
  onSaved: () => void
}) {
  const [text, setText] = useState(stored.join('\n'))
  const [dirty, setDirty] = useState(false)
  const [state, setState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  // Data ze serveru dorazila později / po uložení jinde — needitovaný obsah se sladí
  useEffect(() => {
    if (!dirty) setText(stored.join('\n'))
  }, [stored, dirty])

  const save = () => {
    setState('saving')
    putSetting(settingKey, parseLines(text))
      .then(() => {
        setState('saved')
        setDirty(false)
        onSaved()
      })
      .catch(() => setState('error'))
  }

  return (
    <div className="news-source-editor">
      <label>
        <span title={hint} className="news-source-editor-label">
          {label}
        </span>
        <textarea
          value={text}
          rows={Math.min(10, Math.max(3, text.split('\n').length + 1))}
          placeholder={placeholder}
          aria-label={label}
          onChange={(event) => {
            setText(event.target.value)
            setDirty(true)
            setState('idle')
          }}
        />
      </label>
      <div className="news-source-editor-actions">
        <button type="button" onClick={save} disabled={state === 'saving' || !dirty}>
          Uložit
        </button>
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
          placeholder={'cnbc.com\ndid:plc:…'}
          hint={
            'Autoři, jejichž KAŽDÝ post se bere (handle nebo did:…), jeden na řádek.\n' +
            'Defaultní účty smíš smazat — už se nevrátí.\n' +
            'Projeví se za běhu do ~10 minut (bez restartu).'
          }
          stored={lists.news_bluesky_authors ?? []}
          onSaved={load}
        />
        <ListEditor
          label="Reddit subreddity"
          settingKey="news_reddit_subreddits"
          placeholder={'wallstreetbets\nstocks'}
          hint={
            'Subreddity pro nativní RSS (bez r/), jeden na řádek.\n' +
            'Projeví se po restartu news-engine.'
          }
          stored={lists.news_reddit_subreddits ?? []}
          onSaved={load}
        />
        <ListEditor
          label="Vlastní RSS feedy"
          settingKey="news_rss_extra"
          placeholder="https://example.com/feed.xml"
          hint={
            'Libovolné RSS/Atom feedy (plná URL), jeden na řádek.\n' +
            'V auditu se hlásí jako „Vlastní RSS feedy".\n' +
            'Projeví se po restartu news-engine.'
          }
          stored={lists.news_rss_extra ?? []}
          onSaved={load}
        />
      </div>
      <p className="muted">
        Bluesky kurátoři se načítají za běhu (~10 min); subreddity, vlastní feedy a vypnutí zdroje
        se čtou při startu news-engine.
      </p>
    </div>
  )
}
