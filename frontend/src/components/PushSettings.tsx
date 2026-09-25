/** Nastavení notifikací na Telegram (#1175, #1284).

Hlavní vypínač a přepínače per druh upozornění jsou serverová nastavení
(`push_telegram_enabled`, `push_telegram_topic_*`) a ukládají se hned — API
je čte při každém alertu. Seznam přepínačů ve dvou skupinách, popisky,
tooltipy i efektivní hodnoty (po dědění z dřívějších kategorií) přijdou
z `GET /push/status`: jediný zdroj pravdy je tabulka `PUSH_TOPICS` v API,
frontend nedrží kopii klíčů ani výchozích hodnot. Zvonek na přepínačích
nezávisí. Přihlašovací údaje bota jsou jen v `.env`, UI je nikdy nevidí. */
import { useEffect, useState } from 'react'
import { API_BASE } from '../config'

export interface PushTopicStatus {
  key: string
  /** Klíč serverového nastavení (`push_telegram_topic_<key>`) */
  setting: string
  label: string
  /** Tooltip po řádcích (vzor ivRankTooltip) — spojuje se `\n` */
  help: string[]
  /** Efektivní hodnota na serveru: uložená, zděděná z kategorie, nebo výchozí */
  enabled: boolean
}

export interface PushGroupStatus {
  key: string
  label: string
  topics: PushTopicStatus[]
}

export interface PushStatus {
  configured: boolean
  quiet_hours: string
  daily_cap: number
  sent_today: number
  last_sent_at: number | null
  last_error: string | null
  /** Hlavní vypínač Telegramu */
  enabled: boolean
  master: { setting: string; label: string; help: string[] }
  groups: PushGroupStatus[]
}

async function fetchPushStatus(): Promise<PushStatus | null> {
  try {
    const response = await fetch(`${API_BASE}/push/status`)
    if (!response.ok) return null
    const payload = (await response.json()) as Partial<PushStatus> | null
    // Cizí tvar (starší API bez přepínačů, mock) = bez stavu, ne pád komponenty
    if (
      !payload ||
      typeof payload.configured !== 'boolean' ||
      typeof payload.enabled !== 'boolean' ||
      !payload.master ||
      !Array.isArray(payload.groups)
    ) {
      return null
    }
    return payload as PushStatus
  } catch {
    return null
  }
}

export function PushSettings({
  values,
  saveAll,
}: {
  values: Record<string, unknown>
  /** Zápis na server; lokálně se projeví jen přijaté hodnoty, chyba se propustí ven */
  saveAll: (entries: [string, unknown][]) => Promise<void>
}) {
  // undefined = načítá se, null = API nedostupné nebo cizí tvar
  const [status, setStatus] = useState<PushStatus | null | undefined>(undefined)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    void fetchPushStatus().then((result) => {
      if (!cancelled) setStatus(result)
    })
    return () => {
      cancelled = true
    }
  }, [])
  // Uložená hodnota má přednost; bez ní platí efektivní stav ze serveru
  const current = (setting: string, fallback: boolean): boolean => {
    const stored = values[setting]
    return typeof stored === 'boolean' ? stored : fallback
  }
  // Checkbox je řízený hodnotou: přepne se, až když ji server přijme — při
  // odmítnutí (422, API dole) zůstane původní stav a chyba se ukáže
  const toggle = (setting: string, checked: boolean) => {
    setError(null)
    saveAll([[setting, checked]]).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : String(reason))
    })
  }
  const master = status ? current(status.master.setting, status.enabled) : false
  return (
    <section aria-label="Notifikace">
      <h2>Notifikace (Telegram)</h2>
      <p className="muted" data-testid="push-status">
        {status === undefined
          ? 'Stav push notifikací se načítá…'
          : status === null
            ? 'Nastavení notifikací nejde načíst (API nedostupné).'
            : status.configured
              ? `Telegram bot nastaven · dnes odesláno ${status.sent_today}/${status.daily_cap} · tiché hodiny ${status.quiet_hours || '—'}` +
                (status.last_error ? ` · poslední chyba: ${status.last_error}` : '')
              : 'Telegram bot není nastaven — přihlašovací údaje bota a chat id patří do .env (GEXLENS_PUSH_TELEGRAM_*), viz ADMIN manuál.'}
      </p>
      <p className="muted">
        Zvoneček dostává vždy všechno, tyto přepínače řídí jen Telegram. Popis druhu ukáže tooltip.
        Během přihlášení na mobilu k IBKR stojí CumΔ, takže setupy s potvrzením tokem (T1, T4, T8)
        nevznikají; upozornění „IBKR přihlášen jinde“ o tom přijde.
      </p>
      {status && (
        <>
          {/* Jméno dává obalující <label> (= viditelný text, WCAG 2.5.3); title
              i na checkboxu, aby tooltip četla čtečka jako popis */}
          <label className="push-topic push-master" title={status.master.help.join('\n')}>
            <input
              type="checkbox"
              checked={master}
              title={status.master.help.join('\n')}
              onChange={(event) => toggle(status.master.setting, event.target.checked)}
            />
            {status.master.label}
          </label>
          {status.groups.map((group) => (
            // Nativní disabled fieldsetu zamkne a zešedne celou skupinu; uložené
            // hodnoty přepínačů se vypnutím hlavního vypínače nemění
            <fieldset key={group.key} className="push-group" disabled={!master}>
              <legend>{group.label}</legend>
              {group.topics.map((topic) => (
                <label key={topic.key} className="push-topic" title={topic.help.join('\n')}>
                  <input
                    type="checkbox"
                    checked={current(topic.setting, topic.enabled)}
                    aria-label={`Telegram: ${topic.label}`}
                    title={topic.help.join('\n')}
                    onChange={(event) => toggle(topic.setting, event.target.checked)}
                  />
                  {topic.label}
                </label>
              ))}
            </fieldset>
          ))}
          {error && (
            <p className="push-error" role="alert">
              Přepínač se neuložil: {error}
            </p>
          )}
        </>
      )}
    </section>
  )
}
