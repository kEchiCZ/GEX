/** Konfigurace klienta: základ API a WS URL.

Výchozí `/api` je RELATIVNÍ (#542) — prohlížeč mluví jen s nginx, který požadavky
proxuje na API. Port API tak nemusí být publikovaný a odpadá CORS. Absolutní
hodnotu (např. Vite dev server proti API mimo Docker) lze vynutit přes
VITE_API_BASE. */
export const API_BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api'

/** WS adresa odvozená ze základu API; relativní základ bere origin ze stránky. */
export function wsUrlFor(base: string, origin: string): string {
  const root = /^https?:/.test(base) ? base : origin.replace(/\/+$/, '') + base
  return root.replace(/^http/, 'ws') + '/ws/live'
}

export const WS_URL: string = wsUrlFor(
  API_BASE,
  typeof window === 'undefined' ? 'http://127.0.0.1:8000' : window.location.origin,
)

export const APP_VERSION = '0.1.0'
