/** Stav připojení datového providera pro tlačítka v Settings (#1108).

Stav se odvozuje z toho, co hlásí engine (`/status`), ne z toho, na co
uživatel klikl: tlačítko „Přepojit" jen vynutí obnovu spojení a obě větve
(IBKR i tastytrade) běží záměrně souběžně. Kliknutí otevře jen okno
„přepojuji", které se zavře, jakmile engine požadavek skutečně vyřídí
(spojení prošlo přepojením a je zpět) — nebo po REQUEST_WINDOW_MS, aby
tlačítko nezůstalo zamčené, kdyby engine mlčel. */

export type ProviderState = 'connected' | 'reconnecting' | 'disconnected' | 'unknown'

/** Jak dlouho po kliknutí se čeká na vyřízení enginem, než se tlačítko odemkne.
    Engine čte požadavek při pollu nastavení (5 cyklů ≈ do minuty) a přepojení
    samo trvá ~1–2 min; IBKR mezitím hlásí `reconnecting`, tasty jen čítač. */
export const REQUEST_WINDOW_MS = 120_000

export interface ProviderStateInput {
  /** true/false podle enginu; null = engine stav této větve nehlásí. */
  connected: boolean | null
  /** Engine sám hlásí, že se připojuje/přepojuje (IBKR `connection`). */
  reconnecting: boolean
  /** Čas posledního požadavku na přepojení (ms epoch); null = nikdy. */
  requestedAtMs: number | null
  /** Engine po požadavku prokazatelně přepojoval (spojení odpadlo nebo
      vzrostl čítač reconnectů) — teprve pak „připojeno" znamená „po přepojení". */
  progressSeen: boolean
  nowMs: number
}

export function providerState(input: ProviderStateInput): ProviderState {
  const { connected, reconnecting, requestedAtMs, progressSeen, nowMs } = input
  if (reconnecting) return 'reconnecting'
  if (requestedAtMs !== null && nowMs - requestedAtMs < REQUEST_WINDOW_MS) {
    // Po kliknutí platí „přepojuji", dokud engine požadavek nevyřídí
    if (!(connected === true && progressSeen)) return 'reconnecting'
  }
  if (connected === true) return 'connected'
  if (connected === false) return 'disconnected'
  return 'unknown'
}

/** Text tlačítka — barvy mluví samy (rozhodnutí uživatele 10. 9.), bez tooltipu. */
export function providerButtonLabel(state: ProviderState, name: string): string {
  switch (state) {
    case 'connected':
      return `Připojeno · ${name}`
    case 'reconnecting':
      return `Přepojuji ${name}…`
    case 'disconnected':
      return `Odpojeno · ${name} — přepojit`
    default:
      return `Přepojit ${name}`
  }
}

/** Co provider právě dodává + jestli je aktivním zdrojem řetězu/spotu. */
export function providerRoleText(
  provider: 'ibkr' | 'tasty',
  chainSource: string | undefined,
  spotSource: string | undefined,
): string {
  const chain = chainSource === provider ? 'řetěz ✔ aktivní' : chainSource ? 'řetěz záloha' : null
  const spot = spotSource === provider ? 'spot ✔ aktivní' : spotSource ? 'spot záloha' : null
  const fixed =
    provider === 'ibkr'
      ? ['bary', 'OI archiv']
      : ['OI fill', 'tisky (Cum Δ)', 'extended expirace', 'ad-hoc pohledy']
  return [chain, spot, ...fixed].filter((part): part is string => part !== null).join(' · ')
}
