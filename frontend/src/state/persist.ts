/** Persistence UI voleb do localStorage (ADR-0007, #167).

Poslední nastavení uživatele (timeframe, mód heatmapy, přepínače…) přežije
refresh i restart prohlížeče. Hodnoty se při čtení validují reviverem —
rozbitá nebo zastaralá hodnota tiše spadne na default, nikdy neshodí aplikaci.
URL deep-linky mají přednost (parametr `override`).
*/
import { useEffect, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'

const PREFIX = 'gexlens.'

/** Reviver: ze syrové (nedůvěryhodné) hodnoty vyrobí platnou, jinak vrátí fallback. */
export type Revive<T> = (value: unknown, fallback: T) => T

/** Přečte uloženou hodnotu; chybějící/rozbitá → fallback. */
export function readStored<T>(name: string, fallback: T, revive: Revive<T>): T {
  try {
    const raw = window.localStorage.getItem(PREFIX + name)
    if (raw === null) return fallback
    return revive(JSON.parse(raw), fallback)
  } catch {
    // Zakázané úložiště nebo nevalidní JSON — chovej se jako bez uloženého stavu
    return fallback
  }
}

/** useState zrcadlený do localStorage. `override` (URL deep-link) přebíjí uložený stav. */
export function usePersistentState<T>(
  name: string,
  fallback: T,
  revive: Revive<T>,
  override?: T | null,
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => override ?? readStored(name, fallback, revive))
  useEffect(() => {
    try {
      window.localStorage.setItem(PREFIX + name, JSON.stringify(value))
    } catch {
      // Plné/zakázané úložiště — persistence je best-effort, aplikace běží dál
    }
  }, [name, value])
  return [value, setValue]
}

/** Reviver pro výčtové volby (selecty, chipy): jen hodnota z povolené množiny. */
export function oneOf<T extends string>(allowed: readonly T[]): Revive<T> {
  return (value, fallback) =>
    typeof value === 'string' && (allowed as readonly string[]).includes(value)
      ? (value as T)
      : fallback
}

/** Reviver pro číslo sevřené do intervalu (viditelnost, šířka panelu). */
export function clampedNumber(min: number, max: number): Revive<number> {
  return (value, fallback) =>
    typeof value === 'number' && Number.isFinite(value)
      ? Math.min(max, Math.max(min, value))
      : fallback
}

/** Reviver pro objekt booleanů (přepínače): známé klíče přes defaulty, zbytek zahodit. */
export function mergedBooleans<T extends { [K in keyof T]: boolean }>(): Revive<T> {
  return (value, fallback) => {
    if (typeof value !== 'object' || value === null) return fallback
    const result = { ...fallback }
    for (const key of Object.keys(fallback) as Array<keyof T>) {
      const stored = (value as Record<string, unknown>)[key as string]
      if (typeof stored === 'boolean') result[key] = stored as T[keyof T]
    }
    return result
  }
}

/** Reviver pro krátký identifikátor (symbol tickeru). */
export function shortString(maxLength = 12): Revive<string> {
  return (value, fallback) =>
    typeof value === 'string' && value.length > 0 && value.length <= maxLength ? value : fallback
}
