/** Stav tlačítek providerů (#1108): odvozený z enginu, kliknutí otevře jen okno „přepojuji". */
import { describe, expect, test } from 'vitest'
import {
  REQUEST_WINDOW_MS,
  providerButtonLabel,
  providerRoleText,
  providerState,
} from './providerstate'

const T0 = 1_000_000_000

describe('providerState', () => {
  test('bez požadavku kopíruje stav enginu', () => {
    expect(providerState({ connected: true, reconnecting: false, requestedAtMs: null, progressSeen: false, nowMs: T0 })).toBe('connected') // prettier-ignore
    expect(providerState({ connected: false, reconnecting: false, requestedAtMs: null, progressSeen: false, nowMs: T0 })).toBe('disconnected') // prettier-ignore
    expect(providerState({ connected: null, reconnecting: false, requestedAtMs: null, progressSeen: false, nowMs: T0 })).toBe('unknown') // prettier-ignore
    expect(providerState({ connected: true, reconnecting: true, requestedAtMs: null, progressSeen: false, nowMs: T0 })).toBe('reconnecting') // prettier-ignore
  })

  test('po kliknutí platí přepojuji, dokud engine požadavek nevyřídí', () => {
    const clicked = T0
    // Engine ještě nezareagoval (pořád hlásí connected z doby před klikem) → přepojuji
    expect(providerState({ connected: true, reconnecting: false, requestedAtMs: clicked, progressSeen: false, nowMs: clicked + 10_000 })).toBe('reconnecting') // prettier-ignore
    // Spojení odpadlo a je zpět → připojeno (po přepojení)
    expect(providerState({ connected: true, reconnecting: false, requestedAtMs: clicked, progressSeen: true, nowMs: clicked + 20_000 })).toBe('connected') // prettier-ignore
    // Spojení odpadlo a nevrátilo se → v okně dál přepojuji, po okně odpojeno
    expect(providerState({ connected: false, reconnecting: false, requestedAtMs: clicked, progressSeen: true, nowMs: clicked + 20_000 })).toBe('reconnecting') // prettier-ignore
    expect(providerState({ connected: false, reconnecting: false, requestedAtMs: clicked, progressSeen: true, nowMs: clicked + REQUEST_WINDOW_MS + 1 })).toBe('disconnected') // prettier-ignore
    // Engine mlčel celé okno → tlačítko se odemkne podle posledního stavu
    expect(providerState({ connected: true, reconnecting: false, requestedAtMs: clicked, progressSeen: false, nowMs: clicked + REQUEST_WINDOW_MS + 1 })).toBe('connected') // prettier-ignore
  })

  test('popisky a role', () => {
    expect(providerButtonLabel('connected', 'IBKR')).toBe('Připojeno · IBKR')
    expect(providerButtonLabel('reconnecting', 'tastytrade')).toBe('Přepojuji tastytrade…')
    expect(providerButtonLabel('disconnected', 'IBKR')).toBe('Odpojeno · IBKR — přepojit')
    expect(providerButtonLabel('unknown', 'IBKR')).toBe('Přepojit IBKR')
    expect(providerRoleText('ibkr', 'ibkr', 'ibkr')).toBe('řetěz ✔ aktivní · spot ✔ aktivní · bary · OI archiv') // prettier-ignore
    expect(providerRoleText('tasty', 'ibkr', 'tasty')).toBe('řetěz záloha · spot ✔ aktivní · OI fill · tisky (Cum Δ) · extended expirace · ad-hoc pohledy') // prettier-ignore
    expect(providerRoleText('tasty', undefined, undefined)).toBe('OI fill · tisky (Cum Δ) · extended expirace · ad-hoc pohledy') // prettier-ignore
  })
})
