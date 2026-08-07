/** REST klient /settings (SPEC kap. 6): čtení všech hodnot, upsert per klíč. */
import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '../config'

export type ServerSettings = Record<string, unknown>

export async function fetchSettings(): Promise<ServerSettings> {
  const response = await fetch(`${API_BASE}/settings`)
  if (!response.ok) {
    throw new Error(`Načtení nastavení selhalo: HTTP ${response.status}`)
  }
  const payload = (await response.json()) as { settings: ServerSettings }
  return payload.settings
}

export async function putSetting(key: string, value: unknown): Promise<void> {
  const response = await fetch(`${API_BASE}/settings/${key}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  })
  if (!response.ok) {
    throw new Error(`Uložení nastavení ${key} selhalo: HTTP ${response.status}`)
  }
}

/** Serverová nastavení s okamžitým zápisem (bez restartu — engine si je čte průběžně).

`put` je pro volby, které platí hned (téma, pole v Console) — chybu jen spolkne,
protože hlásit ji po každém stisku klávesy by rušilo. `saveAll` je pro tlačítko
Uložit: chybu propustí ven, ať se nedá tvrdit „Uloženo", když server odmítl
(od #542 vrací neznámý klíč 422). */
export function useServerSettings(): {
  values: ServerSettings
  put: (key: string, value: unknown) => void
  saveAll: (entries: [string, unknown][]) => Promise<void>
} {
  const [values, setValues] = useState<ServerSettings>({})

  useEffect(() => {
    let cancelled = false
    fetchSettings()
      .then((loaded) => {
        if (!cancelled) setValues(loaded)
      })
      .catch(() => {
        // API neběží — formuláře jedou nad prázdnými hodnotami
      })
    return () => {
      cancelled = true
    }
  }, [])

  const put = useCallback((key: string, value: unknown) => {
    setValues((previous) => ({ ...previous, [key]: value })) // optimisticky
    void putSetting(key, value).catch(() => {
      // Server nedostupný — hodnota zůstává aspoň lokálně do reloadu
    })
  }, [])

  const saveAll = useCallback(async (entries: [string, unknown][]) => {
    const results = await Promise.allSettled(entries.map(([key, value]) => putSetting(key, value)))
    // Lokálně se projeví jen to, co server přijal — jinak by formulář ukazoval
    // hodnotu, kterou nikdo neuložil
    const accepted = entries.filter((_, index) => results[index].status === 'fulfilled')
    if (accepted.length > 0) {
      setValues((previous) => ({ ...previous, ...Object.fromEntries(accepted) }))
    }
    const rejected = results.filter((result) => result.status === 'rejected')
    if (rejected.length > 0) {
      const reason = rejected[0].reason
      const detail = reason instanceof Error ? reason.message : String(reason)
      const rest = rejected.length > 1 ? ` (a dalších ${rejected.length - 1})` : ''
      throw new Error(`${detail}${rest}`)
    }
  }, [])

  return { values, put, saveAll }
}
