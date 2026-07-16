/** Konfigurace klienta: základ API a WS URL (Vite env, default lokální server). */
export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? 'http://127.0.0.1:8000'

export const WS_URL: string = API_BASE.replace(/^http/, 'ws') + '/ws/live'

export const APP_VERSION = '0.1.0'
