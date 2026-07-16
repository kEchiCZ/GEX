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

/** Serverová nastavení s okamžitým zápisem (bez restartu — engine si je čte průběžně). */
export function useServerSettings(): {
  values: ServerSettings
  put: (key: string, value: unknown) => void
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

  return { values, put }
}
