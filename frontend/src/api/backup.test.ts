/** Testy stahování zálohy (#439). */
import { afterEach, expect, test, vi } from 'vitest'
import { downloadBackup, filenameFrom } from './backup'

afterEach(() => {
  vi.unstubAllGlobals()
  delete (window as unknown as Record<string, unknown>).showSaveFilePicker
})

test('jméno souboru se bere z Content-Disposition', () => {
  const header = 'attachment; filename="gexlens-2026-08-03_2205.dump"'
  expect(filenameFrom(header, new Date('2026-08-03'))).toBe('gexlens-2026-08-03_2205.dump')
})

test('bez hlavičky fallback s dnešním datem', () => {
  expect(filenameFrom(null, new Date('2026-08-03T10:00:00Z'))).toBe('gexlens-2026-08-03.dump')
})

test('s File System Access API se zapisuje na zvolené místo', async () => {
  const written: Blob[] = []
  const close = vi.fn()
  const picker = vi.fn().mockResolvedValue({
    createWritable: async () => ({
      write: async (blob: Blob) => void written.push(blob),
      close,
    }),
  })
  ;(window as unknown as Record<string, unknown>).showSaveFilePicker = picker
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      blob: async () => new Blob(['dump']),
      headers: { get: () => 'attachment; filename="gexlens-x.dump"' },
    }),
  )

  await expect(downloadBackup()).resolves.toBe('saved')
  expect(picker).toHaveBeenCalledOnce()
  expect(written).toHaveLength(1)
  expect(close).toHaveBeenCalledOnce()
})

test('chyba serveru se nepolyká — tichá neúspěšná záloha je horší než žádná', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      text: async () => 'pg_dump není v image k dispozici',
      headers: { get: () => null },
    }),
  )
  await expect(downloadBackup()).rejects.toThrow(/503/)
})
