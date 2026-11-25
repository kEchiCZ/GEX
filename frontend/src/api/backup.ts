/** Zálohy z UI: rychlý dump PostgreSQL (#439) a úplná záloha jedním balíčkem (#1801).

Kam se soubor uloží, vybírá uživatel. Kde to prohlížeč umí (File System Access
API, Chrome/Edge), otevře se nativní dialog „Uložit jako" a odpověď teče streamem
rovnou do zvoleného souboru — bez bufferu v paměti tabu, i u balíčku přes 10 GB.
Dialog se musí otevřít ještě v aktivaci kliku (Chrome ji drží ~5 s), proto nikdy
až po čekání na data: dump PG má hlavičky hned, úplná záloha si předem vyžádá
lístek a dialog otevře před minutou přípravy balíčku.

Jinde (mobil) spadne dump PG na stažení přes blob a úplná záloha na nativní
stažení prohlížečem. To hlavičku s tokenem poslat neumí a token do URL nesmí
(historie prohlížeče, access log), proto jde přes jednorázový lístek
`POST /backup/full/ticket` (platí 5 min a jednou). Která cesta se použila, se
hlásí zpět.
*/
import { API_BASE } from '../config'
import { tokenHeaders } from './apiToken'

export type BackupResult = 'saved' | 'downloaded'

export interface BackupProgress {
  /** Příprava balíčku na serveru → stahování → zavření souboru (Chrome ho teprve teď přesune na místo). */
  phase: 'preparing' | 'streaming' | 'finishing'
  receivedBytes: number
  /** Z `Content-Length`; bez hlavičky neznámé. */
  totalBytes: number | null
  remainingS: number | null
}

interface SaveFilePickerOptions {
  suggestedName?: string
  types?: { description: string; accept: Record<string, string[]> }[]
}
interface FileSystemFileHandleLike {
  createWritable: () => Promise<WritableStream<Uint8Array>>
  /** Chrome 110+; jinde chybí — prázdný soubor po selhání pak zůstane a hláška to řekne. */
  remove?: () => Promise<void>
}
type SaveFilePicker = (options?: SaveFilePickerOptions) => Promise<FileSystemFileHandleLike>

const TOKEN_HINT =
  'Záloha vyžaduje API token (#542) — vlož hodnotu GEXLENS_API_TOKEN z .env do pole ' +
  'API token v sekci Záloha.'
const PROGRESS_INTERVAL_MS = 500

/** Jméno z Content-Disposition; bez hlavičky fallback s dnešním datem. */
export function filenameFrom(header: string | null, today: Date): string {
  const match = header?.match(/filename="([^"]+)"/)
  if (match) return match[1]
  const stamp = today.toISOString().slice(0, 10)
  return `gexlens-${stamp}.dump`
}

/** Zbývající sekundy z průměrné rychlosti od prvního bajtu; bez celku nebo dat neznámé. */
export function remainingSeconds(
  receivedBytes: number,
  totalBytes: number | null,
  elapsedMs: number,
): number | null {
  if (totalBytes === null || receivedBytes <= 0 || elapsedMs <= 0) return null
  const bytesPerMs = receivedBytes / elapsedMs
  return Math.max(0, (totalBytes - receivedBytes) / bytesPerMs / 1000)
}

const mb = (bytes: number) => Math.round(bytes / 1e6).toLocaleString('cs-CZ')

function duration(seconds: number): string {
  return seconds < 60 ? `${Math.ceil(seconds)} s` : `${Math.ceil(seconds / 60)} min`
}

/** Řádek průběhu pod tlačítkem. */
export function progressText(progress: BackupProgress): string {
  if (progress.phase === 'preparing') {
    return 'Připravuji balíček — dump databáze a kopie dnešních partic…'
  }
  if (progress.phase === 'finishing') return 'Dokončuji zápis souboru na disk…'
  const total = progress.totalBytes === null ? '' : ` / ${mb(progress.totalBytes)}`
  const left = progress.remainingS === null ? '' : ` · zbývá ~${duration(progress.remainingS)}`
  return `Stahuji ${mb(progress.receivedBytes)}${total} MB${left}`
}

/** Průchozí počítadlo bajtů: hlásí průběh nejvýš jednou za `intervalMs` a ohlídá úplnost.

Bajty jen počítá a posílá dál, takže tlak zpět (backpressure) z disku zůstává
a nic se nehromadí v paměti. Stream kratší než `Content-Length` je chyba ještě
před zavřením souboru — useknutý balíček by jinak vypadal jako hotová záloha.
*/
export function progressCounter(
  totalBytes: number | null,
  onProgress: (progress: BackupProgress) => void,
  now: () => number = () => performance.now(),
  intervalMs = PROGRESS_INTERVAL_MS,
): TransformStream<Uint8Array, Uint8Array> {
  const started = now()
  let received = 0
  let lastReport = -Infinity
  return new TransformStream({
    transform(chunk, controller) {
      received += chunk.byteLength
      const at = now()
      if (at - lastReport >= intervalMs) {
        lastReport = at
        onProgress({
          phase: 'streaming',
          receivedBytes: received,
          totalBytes,
          remainingS: remainingSeconds(received, totalBytes, at - started),
        })
      }
      controller.enqueue(chunk)
    },
    flush() {
      if (totalBytes !== null && received !== totalBytes) {
        throw new Error(
          `Stahování skončilo předčasně: ${mb(received)} z ${mb(totalBytes)} MB — ` +
            'soubor se neuložil, spusť zálohu znovu.',
        )
      }
      onProgress({ phase: 'finishing', receivedBytes: received, totalBytes, remainingS: 0 })
    },
  })
}

function savePicker(): SaveFilePicker | null {
  const picker = (window as unknown as { showSaveFilePicker?: SaveFilePicker }).showSaveFilePicker
  return typeof picker === 'function' ? picker.bind(window) : null
}

function contentLength(response: Response): number | null {
  const value = Number(response.headers.get('Content-Length') ?? NaN)
  return Number.isFinite(value) && value >= 0 ? value : null
}

/** Chyba odpovědi: 401 vysvětlí token, jinak stav a `detail` ze serveru. */
async function failure(response: Response, what: string): Promise<Error> {
  // Jen 401. Pod 503 se schovává i „pg_dump není v image" — vlastní hláška by
  // tu příčinu přepsala, a detail ze serveru ji vysvětlí líp (#542)
  if (response.status === 401) return new Error(TOKEN_HINT)
  const text = await response.text().catch(() => '')
  let detail = text
  try {
    const parsed = JSON.parse(text) as { detail?: unknown }
    if (typeof parsed.detail === 'string') detail = parsed.detail
  } catch {
    // Tělo není JSON — ukáže se, jak přišlo
  }
  return new Error(`${what} selhala: HTTP ${response.status} ${detail}`.trim())
}

function clickDownload(href: string, name: string): void {
  const link = document.createElement('a')
  link.href = href
  link.download = name
  link.click()
}

async function streamToFile(
  body: ReadableStream<Uint8Array>,
  handle: FileSystemFileHandleLike,
): Promise<void> {
  const writable = await handle.createWritable()
  // pipeTo soubor po úspěchu zavře, po chybě zápis zruší (rozpracovaná data se zahodí)
  await body.pipeTo(writable)
}

/** Zrušení „Uložit jako" (#506) chodí jako DOMException, ne vždy `instanceof Error`. */
export function isAbortError(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as { name?: unknown }).name === 'AbortError'
  )
}

/** Selhání po dialogu: prázdný soubor z dialogu nesmí zůstat jako „záloha".

Vrací vždy obyčejnou `Error`. AbortError po dialogu (přerušený zápis, Chrome
odmítne `close()`) není zrušení uživatelem a nesmí se jako ono spolknout.
*/
async function withoutLeftover(handle: FileSystemFileHandleLike, error: unknown): Promise<Error> {
  const removed = await handle.remove?.().then(
    () => true,
    () => false,
  )
  // DOMException nemusí být instanceof Error (jsdom), zpráva se bere z pole
  const field = (error as { message?: unknown } | null)?.message
  const message = typeof field === 'string' ? field : String(error)
  const reason = isAbortError(error) ? `Zápis zálohy se přerušil: ${message}` : message
  if (removed) return error instanceof Error && !isAbortError(error) ? error : new Error(reason)
  return new Error(`${reason} Prázdný nebo neúplný soubor na zvoleném místě smaž.`)
}

export async function downloadBackup(): Promise<BackupResult> {
  const response = await fetch(`${API_BASE}/backup/postgres`, { headers: tokenHeaders() })
  if (!response.ok) throw await failure(response, 'Záloha')
  const name = filenameFrom(response.headers.get('Content-Disposition'), new Date())

  const picker = savePicker()
  if (picker !== null && response.body !== null) {
    // Hlavičky dumpu přijdou hned, dialog se tak otevře ještě v aktivaci kliku;
    // dřív čekal na celé tělo (`blob()`) a Chrome ho u velkého dumpu odmítl (#1801)
    let handle: FileSystemFileHandleLike
    try {
      handle = await picker({
        suggestedName: name,
        types: [
          { description: 'PostgreSQL dump', accept: { 'application/octet-stream': ['.dump'] } },
        ],
      })
    } catch (error) {
      await response.body.cancel().catch(() => undefined) // ukončí pg_dump na serveru
      throw error
    }
    try {
      await streamToFile(response.body, handle)
    } catch (error) {
      throw await withoutLeftover(handle, error)
    }
    return 'saved'
  }

  const url = URL.createObjectURL(await response.blob())
  clickDownload(url, name)
  URL.revokeObjectURL(url)
  return 'downloaded'
}

/** Úplná záloha (celá PG + data/ + konfigurace bez tajemství), F2 #1799 a F4 #1801. */
export async function downloadFullBackup(
  onProgress: (progress: BackupProgress) => void,
): Promise<BackupResult> {
  // Lístek napřed: chybný token se ohlásí dřív, než dialog založí soubor,
  // a jméno balíčku (čas a revize image) dá server
  const issued = await fetch(`${API_BASE}/backup/full/ticket`, {
    method: 'POST',
    headers: tokenHeaders(),
  })
  if (!issued.ok) throw await failure(issued, 'Úplná záloha')
  const { ticket, filename } = (await issued.json()) as { ticket: string; filename: string }

  const picker = savePicker()
  if (picker === null) {
    clickDownload(`${API_BASE}/backup/full?ticket=${encodeURIComponent(ticket)}`, filename)
    return 'downloaded'
  }

  const handle = await picker({
    suggestedName: filename,
    types: [{ description: 'Úplná záloha GEXLens', accept: { 'application/x-tar': ['.tar'] } }],
  })
  onProgress({ phase: 'preparing', receivedBytes: 0, totalBytes: null, remainingS: null })
  try {
    // Hlavička, ne lístek: dialog mohl zůstat otevřený přes 5 min platnosti lístku
    const response = await fetch(`${API_BASE}/backup/full`, { headers: tokenHeaders() })
    if (!response.ok) throw await failure(response, 'Úplná záloha')
    if (response.body === null) throw new Error('Úplná záloha selhala: odpověď bez těla')
    const counted = response.body.pipeThrough(progressCounter(contentLength(response), onProgress))
    await streamToFile(counted, handle)
  } catch (error) {
    throw await withoutLeftover(handle, error)
  }
  return 'saved'
}
