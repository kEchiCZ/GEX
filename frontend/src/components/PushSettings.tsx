/** Nastavení push notifikací na Telegram (#1175, #1126 bod 3b).

Přepínače kategorií jsou serverová nastavení (`push_telegram_*`), ukládají se
okamžitě — čte je API při každém alertu. Stav (nakonfigurováno, odesláno
dnes, poslední chyba) přijde z `GET /push/status`; přihlašovací údaje bota
jsou jen v `.env`, UI je nikdy nevidí ani nezobrazuje. */
import { useEffect, useState } from 'react'
import { API_BASE } from '../config'

export interface PushStatus {
  configured: boolean
  quiet_hours: string
  daily_cap: number
  sent_today: number
  last_sent_at: number | null
  last_error: string | null
  categories: Record<string, boolean>
}

export const PUSH_CATEGORIES: readonly { key: string; label: string; help: string }[] = [
  {
    key: 'setup',
    label: 'Setupy',
    help: 'Vznik setupu (T1–T9 z registru), práh confidence z .env (GEXLENS_PUSH_SETUP_MIN_CONFIDENCE).',
  },
  {
    key: 'ops',
    label: 'Provoz',
    help: 'Výpadek IBKR, degradovaný start, přetažení mobilem, disk, zaseknuté greeks — jdou i v tichých hodinách.',
  },
  {
    key: 'news',
    label: 'Zprávy',
    help: 'Anomálie zpráv a koncentrace opčního objemu před eventem.',
  },
  {
    key: 'info',
    label: 'Ostatní',
    help: 'FA validace a kalibrace, drift, retro pass, blízkost úrovně — default vypnuto, je toho hodně.',
  },
]

export async function fetchPushStatus(): Promise<PushStatus | null> {
  try {
    const response = await fetch(`${API_BASE}/push/status`)
    if (!response.ok) return null
    const payload = (await response.json()) as Partial<PushStatus> | null
    // Cizí tvar (starší API, mock) = bez stavu, ne pád komponenty
    if (!payload || typeof payload.configured !== 'boolean') return null
    return { ...payload, categories: payload.categories ?? {} } as PushStatus
  } catch {
    return null
  }
}

export function PushSettings({
  values,
  put,
}: {
  values: Record<string, unknown>
  put: (key: string, value: unknown) => void
}) {
  const [status, setStatus] = useState<PushStatus | null>(null)
  useEffect(() => {
    let cancelled = false
    void fetchPushStatus().then((result) => {
      if (!cancelled) setStatus(result)
    })
    return () => {
      cancelled = true
    }
  }, [])
  const enabled = (key: string): boolean => {
    const stored = values[`push_telegram_${key}`]
    if (typeof stored === 'boolean') return stored
    return status?.categories?.[key] ?? key !== 'info'
  }
  return (
    <section aria-label="Notifikace">
      <h2>Notifikace (Telegram)</h2>
      <p className="muted" data-testid="push-status">
        {status === null
          ? 'Stav push notifikací se načítá…'
          : status.configured
            ? `Telegram bot nastaven · dnes odesláno ${status.sent_today}/${status.daily_cap} · tiché hodiny ${status.quiet_hours || '—'}` +
              (status.last_error ? ` · poslední chyba: ${status.last_error}` : '')
            : 'Telegram bot není nastaven — přihlašovací údaje bota a chat id patří do .env (GEXLENS_PUSH_TELEGRAM_*), viz ADMIN manuál.'}
      </p>
      <p className="muted">
        Posílá se totéž, co zvoní ve zvonku — jedna fronta, dva výstupy. Během přihlášení na mobilu
        k IBKR stojí CumΔ, takže setupy s potvrzením tokem (T1, T4, T8) nevznikají; provozní alert o
        tom přijde.
      </p>
      {PUSH_CATEGORIES.map((category) => (
        <label key={category.key} className="push-category" title={category.help}>
          <input
            type="checkbox"
            checked={enabled(category.key)}
            aria-label={`Push: ${category.label}`}
            onChange={(event) => put(`push_telegram_${category.key}`, event.target.checked)}
          />
          {category.label}
        </label>
      ))}
    </section>
  )
}
