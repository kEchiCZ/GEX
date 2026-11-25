/** Běžící záloha z Settings mimo komponentu (#1801).

Úplná záloha trvá minuty a SettingsView se při přepnutí pohledu odmontuje.
Stream sice doběhne, ale výsledek, a hlavně selhání, by bez tohoto stavu nikdo
neviděl a druhý klik po návratu by dostal 409. Proto průběh i poslední hlášku
drží modul a komponenta je jen čte (`useSyncExternalStore`). Dokud záloha běží,
zavření nebo obnovení stránky se ptá — přerušilo by stream do souboru.
*/
import type { BackupProgress, BackupResult } from '../api/backup'
import { downloadBackup, downloadFullBackup, isAbortError } from '../api/backup'

export type BackupKind = 'postgres' | 'full'

export interface BackupRun {
  /** Běží nejvýš jedna záloha naráz — obě čtou celou PG. */
  running: BackupKind | null
  progress: BackupProgress | null
  note: string | null
}

/** Hláška po záloze podle druhu a cesty, kterou se soubor uložil (#439, #1801). */
const DONE: Record<BackupKind, Record<BackupResult, string>> = {
  postgres: {
    saved: 'Hotovo — záloha uložena na zvolené místo.',
    downloaded: 'Hotovo — záloha stažena do složky Stažené soubory (prohlížeč neumí výběr složky).',
  },
  full: {
    saved: 'Hotovo — úplná záloha uložena na zvolené místo.',
    // Nativní stažení běží v prohlížeči; průběh ani výsledek stránka nevidí
    downloaded:
      'Stahování úplné zálohy převzal prohlížeč — průběh a výsledek ukazují jeho stažené soubory.',
  },
}

const IDLE: BackupRun = { running: null, progress: null, note: null }
let current: BackupRun = IDLE
const listeners = new Set<() => void>()

function update(patch: Partial<BackupRun>): void {
  current = { ...current, ...patch }
  for (const listener of listeners) listener()
}

export function subscribeBackupRun(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function backupRunSnapshot(): BackupRun {
  return current
}

/** Jen pro testy: modul přežívá mezi testy, stav ne. */
export function resetBackupRun(): void {
  current = IDLE
  for (const listener of listeners) listener()
}

function guardUnload(event: BeforeUnloadEvent): void {
  event.preventDefault()
}

export async function runBackup(kind: BackupKind): Promise<void> {
  if (current.running !== null) return
  update({ running: kind, progress: null, note: null })
  window.addEventListener('beforeunload', guardUnload)
  try {
    const where =
      kind === 'full'
        ? await downloadFullBackup((progress) => update({ progress }))
        : await downloadBackup()
    update({ note: DONE[kind][where] })
  } catch (error) {
    // Zrušení dialogu „Uložit jako" není chyba zálohy — žádná hláška (#506).
    // Cokoli jiného musí být vidět: tichá neúspěšná záloha je horší než žádná
    if (!isAbortError(error)) {
      update({ note: error instanceof Error ? error.message : 'Záloha selhala.' })
    }
  } finally {
    window.removeEventListener('beforeunload', guardUnload)
    update({ running: null, progress: null })
  }
}
