/** Stažení zálohy PostgreSQL (#439).

Kam se soubor uloží, vybírá uživatel. Kde to prohlížeč umí (File System Access
API, Chrome/Edge), otevře se nativní dialog „Uložit jako" a záloha se streamuje
rovnou na zvolené místo. Jinde spadne na klasické stažení do složky Stažené
soubory — proto se sem hlásí zpět, která z cest se použila.
*/
import { API_BASE } from '../config'
import { tokenHeaders } from './apiToken'

export type BackupResult = 'saved' | 'downloaded'

interface SaveFilePickerOptions {
  suggestedName?: string
  types?: { description: string; accept: Record<string, string[]> }[]
}
interface FileSystemWritable {
  write: (data: Blob) => Promise<void>
  close: () => Promise<void>
}
interface FileSystemFileHandleLike {
  createWritable: () => Promise<FileSystemWritable>
}
type SaveFilePicker = (options?: SaveFilePickerOptions) => Promise<FileSystemFileHandleLike>

/** Jméno z Content-Disposition; bez hlavičky fallback s dnešním datem. */
export function filenameFrom(header: string | null, today: Date): string {
  const match = header?.match(/filename="([^"]+)"/)
  if (match) return match[1]
  const stamp = today.toISOString().slice(0, 10)
  return `gexlens-${stamp}.dump`
}

export async function downloadBackup(): Promise<BackupResult> {
  const response = await fetch(`${API_BASE}/backup/postgres`, { headers: tokenHeaders() })
  if (response.status === 401 || response.status === 503) {
    throw new Error(
      'Záloha vyžaduje API token (#542) — vlož hodnotu GEXLENS_API_TOKEN z .env ' +
        'do pole nad tlačítkem.',
    )
  }
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error(`Záloha selhala: HTTP ${response.status} ${detail}`.trim())
  }
  const blob = await response.blob()
  const name = filenameFrom(response.headers.get('Content-Disposition'), new Date())

  const picker = (window as unknown as { showSaveFilePicker?: SaveFilePicker }).showSaveFilePicker
  if (typeof picker === 'function') {
    const handle = await picker({
      suggestedName: name,
      types: [
        { description: 'PostgreSQL dump', accept: { 'application/octet-stream': ['.dump'] } },
      ],
    })
    const writable = await handle.createWritable()
    await writable.write(blob)
    await writable.close()
    return 'saved'
  }

  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  URL.revokeObjectURL(url)
  return 'downloaded'
}
