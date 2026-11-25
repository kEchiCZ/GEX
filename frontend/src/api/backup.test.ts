/** Testy stahování záloh: dump PG (#439) a úplná záloha streamem na disk (#1801). */
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { BackupProgress } from './backup'
import {
  downloadBackup,
  downloadFullBackup,
  filenameFrom,
  progressCounter,
  progressText,
  remainingSeconds,
} from './backup'

type Win = Record<string, unknown>

beforeEach(() => {
  localStorage.setItem('gexlens.apiToken', 'tajne')
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
  delete (window as unknown as Win).showSaveFilePicker
})

/** Odpověď se streamem po kouscích; `declaredLength` jinak než délka těla = useknutý přenos. */
function streamed(chunks: string[], headers: Record<string, string> = {}, status = 200): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(body, { status, headers })
}

function jsonResponse(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Soubor ze „Uložit jako": zapsaný obsah, zavření, zrušení a smazání. */
function fakeFile() {
  const state = { text: '', closed: false, aborted: false, removed: false }
  const decoder = new TextDecoder()
  const handle = {
    createWritable: async () =>
      new WritableStream<Uint8Array>({
        write: (chunk) => void (state.text += decoder.decode(chunk)),
        close: () => void (state.closed = true),
        abort: () => void (state.aborted = true),
      }),
    remove: async () => void (state.removed = true),
  }
  return { state, handle }
}

function installPicker(handle: unknown) {
  const picker = vi.fn().mockResolvedValue(handle)
  ;(window as unknown as Win).showSaveFilePicker = picker
  return picker
}

const TICKET = { ticket: 'L1st3k-jednou', filename: 'gexlens-zaloha-2026-11-25_2210-cd7f1d9.tar' }

// ── Čisté funkce ──────────────────────────────────────────────────────

test('jméno souboru se bere z Content-Disposition', () => {
  const header = 'attachment; filename="gexlens-2026-08-03_2205.dump"'
  expect(filenameFrom(header, new Date('2026-08-03'))).toBe('gexlens-2026-08-03_2205.dump')
})

test('bez hlavičky fallback s dnešním datem', () => {
  expect(filenameFrom(null, new Date('2026-08-03T10:00:00Z'))).toBe('gexlens-2026-08-03.dump')
})

test('odhad zbývajícího času z průměrné rychlosti; bez celku neznámý', () => {
  expect(remainingSeconds(500, 1000, 10_000)).toBe(10)
  expect(remainingSeconds(1000, 1000, 10_000)).toBe(0)
  expect(remainingSeconds(500, null, 10_000)).toBeNull()
  expect(remainingSeconds(0, 1000, 10_000)).toBeNull()
})

test('řádek průběhu: MB / celkem a zbývající čas', () => {
  const at = (p: Partial<BackupProgress>): BackupProgress => ({
    phase: 'streaming',
    receivedBytes: 0,
    totalBytes: null,
    remainingS: null,
    ...p,
  })
  const text = progressText(
    at({ receivedBytes: 1_234_000_000, totalBytes: 13_456_000_000, remainingS: 150 }),
  )
  expect(text.replace(/\s/g, ' ')).toBe('Stahuji 1 234 / 13 456 MB · zbývá ~3 min')
  expect(
    progressText(at({ receivedBytes: 5_000_000, totalBytes: 9_000_000, remainingS: 4.2 })),
  ).toBe('Stahuji 5 / 9 MB · zbývá ~5 s')
  expect(progressText(at({ receivedBytes: 2_000_000 }))).toBe('Stahuji 2 MB')
  expect(progressText(at({ phase: 'preparing' }))).toMatch(/Připravuji balíček/)
  expect(progressText(at({ phase: 'finishing' }))).toMatch(/Dokončuji zápis/)
})

test('počítadlo hlásí průběh nejvýš jednou za interval a na konci fázi dokončení', async () => {
  let clock = 0
  const reports: BackupProgress[] = []
  const counter = progressCounter(
    6,
    (p) => reports.push(p),
    () => clock,
    500,
  )
  const writer = counter.writable.getWriter()
  const drained = new Response(counter.readable).arrayBuffer()
  for (const step of [0, 100, 600]) {
    clock = step
    await writer.write(new Uint8Array(2))
  }
  await writer.close()
  expect((await drained).byteLength).toBe(6) // bajty prošly beze změny
  expect(reports.map((r) => [r.phase, r.receivedBytes])).toEqual([
    ['streaming', 2],
    ['streaming', 6],
    ['finishing', 6],
  ])
  expect(reports[1].remainingS).toBe(0)
})

// ── Dump PostgreSQL ───────────────────────────────────────────────────

test('dump PG: dialog hned po hlavičkách a tělo streamem do souboru', async () => {
  const { state, handle } = fakeFile()
  const picker = installPicker(handle)
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValue(
        streamed(['PGD', 'MP'], { 'Content-Disposition': 'attachment; filename="gexlens-x.dump"' }),
      ),
  )

  await expect(downloadBackup()).resolves.toBe('saved')
  expect(picker).toHaveBeenCalledWith(expect.objectContaining({ suggestedName: 'gexlens-x.dump' }))
  expect(state.text).toBe('PGDMP')
  expect(state.closed).toBe(true)
})

test('dump PG: zrušení dialogu propadne jako AbortError a stahování se ukončí', async () => {
  const response = streamed(['PGDMP'])
  const cancel = vi.spyOn(response.body as ReadableStream, 'cancel')
  ;(window as unknown as Win).showSaveFilePicker = vi
    .fn()
    .mockRejectedValue(new DOMException('The user aborted a request.', 'AbortError'))
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))

  await expect(downloadBackup()).rejects.toMatchObject({ name: 'AbortError' })
  expect(cancel).toHaveBeenCalledOnce()
})

test('chyba serveru se nepolyká — tichá neúspěšná záloha je horší než žádná', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(jsonResponse(503, { detail: 'pg_dump není v image k dispozici' })),
  )
  await expect(downloadBackup()).rejects.toThrow(/503 pg_dump není v image/)
})

test('401 vysvětlí chybějící API token, ne holý stavový kód (#542)', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'Chybí nebo nesedí hlavička' })),
  )
  await expect(downloadBackup()).rejects.toThrow(/API token/)
})

// ── Úplná záloha ──────────────────────────────────────────────────────

test('úplná záloha: lístek, dialog se jménem ze serveru, stream na disk s průběhem', async () => {
  const { state, handle } = fakeFile()
  const picker = installPicker(handle)
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(jsonResponse(200, TICKET))
    .mockResolvedValueOnce(streamed(['tar-', 'obsah'], { 'Content-Length': '9' }))
  vi.stubGlobal('fetch', fetchMock)
  const reports: BackupProgress[] = []

  await expect(downloadFullBackup((p) => reports.push(p))).resolves.toBe('saved')

  const [ticketUrl, ticketInit] = fetchMock.mock.calls[0] as [string, RequestInit]
  expect(ticketUrl).toBe('/api/backup/full/ticket')
  expect(ticketInit).toMatchObject({ method: 'POST', headers: { 'X-GEXLens-Token': 'tajne' } })
  expect(picker).toHaveBeenCalledWith(expect.objectContaining({ suggestedName: TICKET.filename }))
  // Stream jde s hlavičkou: dialog mohl být otevřený déle než platí lístek
  const [streamUrl, streamInit] = fetchMock.mock.calls[1] as [string, RequestInit]
  expect(streamUrl).toBe('/api/backup/full')
  expect(streamInit).toMatchObject({ headers: { 'X-GEXLens-Token': 'tajne' } })
  expect(state.text).toBe('tar-obsah')
  expect(state.closed).toBe(true)
  expect(reports[0].phase).toBe('preparing')
  expect(reports.at(-1)).toMatchObject({ phase: 'finishing', receivedBytes: 9, totalBytes: 9 })
})

test('úplná záloha: zrušení dialogu nespustí stahování a propadne jako AbortError', async () => {
  ;(window as unknown as Win).showSaveFilePicker = vi
    .fn()
    .mockRejectedValue(new DOMException('The user aborted a request.', 'AbortError'))
  const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(200, TICKET))
  vi.stubGlobal('fetch', fetchMock)

  await expect(downloadFullBackup(() => {})).rejects.toMatchObject({ name: 'AbortError' })
  expect(fetchMock).toHaveBeenCalledOnce() // jen lístek
})

test('úplná záloha: 401 na lístku vysvětlí token dřív, než dialog založí soubor', async () => {
  const picker = installPicker(fakeFile().handle)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'x' })))

  await expect(downloadFullBackup(() => {})).rejects.toThrow(/API token/)
  expect(picker).not.toHaveBeenCalled()
})

test('úplná záloha bez File System Access API: nativní stažení s lístkem, token v URL ne', async () => {
  const hrefs: string[] = []
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    hrefs.push(this.href)
    expect(this.download).toBe(TICKET.filename)
  })
  const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(200, TICKET))
  vi.stubGlobal('fetch', fetchMock)

  await expect(downloadFullBackup(() => {})).resolves.toBe('downloaded')
  expect(fetchMock).toHaveBeenCalledOnce()
  expect(hrefs).toHaveLength(1)
  expect(hrefs[0]).toMatch(/\/api\/backup\/full\?ticket=L1st3k-jednou$/)
  expect(hrefs[0]).not.toContain('tajne')
})

test('úplná záloha: chyba serveru po dialogu je vidět a prázdný soubor se smaže', async () => {
  const { state, handle } = fakeFile()
  installPicker(handle)
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, TICKET))
      .mockResolvedValueOnce(jsonResponse(409, { detail: 'Úplná záloha už běží' })),
  )

  await expect(downloadFullBackup(() => {})).rejects.toThrow(
    'Úplná záloha selhala: HTTP 409 Úplná záloha už běží',
  )
  expect(state.removed).toBe(true)
})

test('úplná záloha: useknutý stream se neuloží jako hotová záloha', async () => {
  const { state, handle } = fakeFile()
  installPicker(handle)
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, TICKET))
      .mockResolvedValueOnce(streamed(['tar-'], { 'Content-Length': '9000000' })),
  )

  await expect(downloadFullBackup(() => {})).rejects.toThrow(/předčasně/)
  expect(state.closed).toBe(false)
  expect(state.aborted).toBe(true)
  expect(state.removed).toBe(true)
})

test('úplná záloha: bez možnosti smazat soubor hláška řekne, ať ho uživatel smaže', async () => {
  const { handle } = fakeFile()
  installPicker({ createWritable: handle.createWritable }) // bez remove (starší Chrome)
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, TICKET))
      .mockResolvedValueOnce(jsonResponse(500, { detail: 'Příprava úplné zálohy selhala' })),
  )

  await expect(downloadFullBackup(() => {})).rejects.toThrow(
    /HTTP 500 .*soubor na zvoleném místě smaž/,
  )
})
