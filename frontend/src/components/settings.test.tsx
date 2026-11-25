/** Testy Settings: koncept změn a tlačítko Uložit (#445). */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { SettingsView } from './SettingsView'
import type { BackupProgress } from '../api/backup'
import { AppStateProvider } from '../state/AppState'
import { resetBackupRun } from '../state/backupRun'

const puts: { key: string; value: unknown }[] = []
// Serverové hodnoty per test — výchozí stav simuluje načtené settings
let serverValues: Record<string, unknown> = {}
// Chyba, kterou má saveAll vrátit (null = uložení projde)
let saveFailure: Error | null = null
const backupMock = vi.fn<() => Promise<'saved' | 'downloaded'>>()
const fullBackupMock =
  vi.fn<(onProgress: (p: BackupProgress) => void) => Promise<'saved' | 'downloaded'>>()

vi.mock('../api/settings', () => ({
  useServerSettings: () => ({
    values: serverValues,
    put: (key: string, value: unknown) => puts.push({ key, value }),
    saveAll: async (entries: [string, unknown][]) => {
      if (saveFailure) throw saveFailure
      for (const [key, value] of entries) puts.push({ key, value })
    },
  }),
}))

vi.mock('../api/backup', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/backup')>()),
  downloadBackup: () => backupMock(),
  downloadFullBackup: (onProgress: (p: BackupProgress) => void) => fullBackupMock(onProgress),
}))

beforeEach(() => {
  serverValues = { ibkr_port: 7496, retention_days: 90 }
  saveFailure = null
})

afterEach(() => {
  puts.length = 0
  saveFailure = null
  vi.useRealTimers()
  backupMock.mockReset()
  fullBackupMock.mockReset()
  resetBackupRun()
})

function renderSettings() {
  return render(
    <AppStateProvider>
      <SettingsView />
    </AppStateProvider>,
  )
}

test('změna pole se neodešle, dokud se neuloží (#445)', async () => {
  renderSettings()
  const port = await screen.findByLabelText('Port')

  fireEvent.change(port, { target: { value: '7497' } })

  expect(puts).toHaveLength(0) // dokud se neklikne na Uložit, server nic nedostane
  expect(screen.getByRole('status').textContent).toContain('Neuloženo')
})

test('Uložit odešle všechny rozepsané změny najednou', async () => {
  renderSettings()
  fireEvent.change(await screen.findByLabelText('Port'), { target: { value: '7497' } })
  fireEvent.change(screen.getByLabelText('Retence (dny)'), { target: { value: '120' } })

  fireEvent.click(screen.getByRole('button', { name: 'Uložit' }))

  await waitFor(() =>
    expect(puts).toEqual([
      { key: 'ibkr_port', value: 7497 },
      { key: 'retention_days', value: 120 },
    ]),
  )
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Uloženo'))
})

test('potvrzení uložení samo zmizí — jinak splyne s klidovým stavem', async () => {
  renderSettings()
  fireEvent.change(await screen.findByLabelText('Port'), { target: { value: '7497' } })
  fireEvent.click(screen.getByRole('button', { name: 'Uložit' }))

  const status = screen.getByRole('status')
  await waitFor(() => expect(status.textContent).toContain('Uloženo'))

  await waitFor(() => expect(status.textContent).toContain('Žádné neuložené změny'), {
    timeout: 5000,
  })
})

test('odmítnuté uložení se netváří jako úspěch a koncept zůstane (#542)', async () => {
  // Neznámý klíč vrací od #542 HTTP 422 — dřív se chyba spolkla a UI hlásilo „Uloženo"
  saveFailure = new Error('Uložení nastavení retention_days selhalo: HTTP 422')
  renderSettings()
  fireEvent.change(await screen.findByLabelText('Retence (dny)'), { target: { value: '120' } })

  fireEvent.click(screen.getByRole('button', { name: 'Uložit' }))

  const status = screen.getByRole('status')
  await waitFor(() => expect(status.textContent).toContain('HTTP 422'))
  expect(status.textContent).not.toContain('✓ Uloženo')
  // Koncept se nezahodil — Uložit jde zkusit znovu
  expect((screen.getByRole('button', { name: 'Uložit' }) as HTMLButtonElement).disabled).toBe(false)
})

test('Zahodit změny vrátí původní hodnoty a nic neodešle', async () => {
  renderSettings()
  const port = await screen.findByLabelText('Port')
  fireEvent.change(port, { target: { value: '4001' } })

  fireEvent.click(screen.getByRole('button', { name: 'Zahodit změny' }))

  expect(puts).toHaveLength(0)
  expect((screen.getByLabelText('Port') as HTMLInputElement).value).toBe('7496')
})

test('textarea Seance ukazuje serverovou hodnotu, ne defaulty z doby fetche (#505)', async () => {
  // První render běží ještě bez serverových settings (fetch v letu)
  serverValues = {}
  const { rerender } = renderSettings()

  // Server odpoví jinou hodnotou než DEFAULT_SESSIONS
  serverValues = {
    ibkr_port: 7496,
    sessions: [{ label: 'Tokio', minuteIdx: 5 }],
  }
  rerender(
    <AppStateProvider>
      <SettingsView />
    </AppStateProvider>,
  )
  const textarea = (await screen.findByLabelText('Seznam seancí (JSON)')) as HTMLTextAreaElement
  expect(textarea.value).toContain('Tokio')
})

test('klik do textarey Seance a ven bez editace nevytvoří „Neuloženo" (#505)', async () => {
  serverValues = {
    ibkr_port: 7496,
    sessions: [{ label: 'Tokio', minuteIdx: 5 }],
  }
  renderSettings()
  const textarea = (await screen.findByLabelText('Seznam seancí (JSON)')) as HTMLTextAreaElement

  fireEvent.focus(textarea)
  fireEvent.blur(textarea)

  expect(screen.getByRole('status').textContent).toContain('Žádné neuložené změny')
  // Uložit by nemělo co poslat — serverová hodnota se nesmí přepsat defaulty
  expect((screen.getByRole('button', { name: 'Uložit' }) as HTMLButtonElement).disabled).toBe(true)
})

test('skutečná editace textarey Seance koncept vytvoří a Uložit ji odešle (#505)', async () => {
  renderSettings()
  const textarea = (await screen.findByLabelText('Seznam seancí (JSON)')) as HTMLTextAreaElement

  fireEvent.change(textarea, { target: { value: '[{"label":"Sydney","minuteIdx":1}]' } })
  fireEvent.blur(textarea)

  expect(screen.getByRole('status').textContent).toContain('Neuloženo')
  fireEvent.click(screen.getByRole('button', { name: 'Uložit' }))
  await waitFor(() =>
    expect(puts).toEqual([{ key: 'sessions', value: [{ label: 'Sydney', minuteIdx: 1 }] }]),
  )
})

test('zrušení dialogu „Uložit jako" u zálohy není chyba — žádná hláška (#506)', async () => {
  backupMock.mockRejectedValueOnce(new DOMException('The user aborted a request.', 'AbortError'))
  renderSettings()
  const button = (await screen.findByRole('button', {
    name: 'Zálohovat PostgreSQL',
  })) as HTMLButtonElement

  fireEvent.click(button)
  await waitFor(() => expect(button.disabled).toBe(false))

  expect(screen.queryByText(/aborted/i)).toBeNull()
  expect(screen.queryByText(/selhala/i)).toBeNull()
})

test('skutečná chyba zálohy zůstává vidět (#506)', async () => {
  backupMock.mockRejectedValueOnce(new Error('Záloha selhala: HTTP 503'))
  renderSettings()
  const button = (await screen.findByRole('button', {
    name: 'Zálohovat PostgreSQL',
  })) as HTMLButtonElement

  fireEvent.click(button)

  expect(await screen.findByText(/HTTP 503/)).toBeDefined()
})

test('úplná záloha ukazuje průběh, druhá mezitím nejde spustit, pak hotovo (#1801)', async () => {
  let finish: (result: 'saved') => void = () => {}
  fullBackupMock.mockImplementationOnce((onProgress) => {
    onProgress({
      phase: 'streaming',
      receivedBytes: 5_000_000_000,
      totalBytes: 10_000_000_000,
      remainingS: 120,
    })
    return new Promise((resolve) => (finish = resolve))
  })
  renderSettings()
  const full = await screen.findByRole('button', {
    name: 'Úplná záloha (data + databáze + nastavení)',
  })

  fireEvent.click(full)

  const progress = await screen.findByText(/Stahuji 5.000 \/ 10.000 MB · zbývá ~2 min/)
  expect(progress.getAttribute('role')).toBe('status')
  const bar = screen.getByRole('progressbar', {
    name: 'Průběh úplné zálohy',
  }) as HTMLProgressElement
  expect(bar.value).toBe(5_000_000_000)
  expect(bar.max).toBe(10_000_000_000)
  const postgres = screen.getByRole('button', {
    name: 'Zálohovat PostgreSQL',
  }) as HTMLButtonElement
  expect(postgres.disabled).toBe(true)
  expect((screen.getByRole('button', { name: 'Zálohuji…' }) as HTMLButtonElement).disabled).toBe(
    true,
  )

  finish('saved')

  expect(await screen.findByText('Hotovo — úplná záloha uložena na zvolené místo.')).toBeDefined()
  expect(screen.queryByText(/Stahuji/)).toBeNull()
  expect(postgres.disabled).toBe(false)
})

test('zrušení dialogu u úplné zálohy není chyba — žádná hláška (#506)', async () => {
  fullBackupMock.mockRejectedValueOnce(
    new DOMException('The user aborted a request.', 'AbortError'),
  )
  renderSettings()
  const full = (await screen.findByRole('button', {
    name: 'Úplná záloha (data + databáze + nastavení)',
  })) as HTMLButtonElement

  fireEvent.click(full)
  await waitFor(() => expect(full.disabled).toBe(false))

  expect(screen.queryByText(/aborted/i)).toBeNull()
  expect(screen.queryByText(/selhala/i)).toBeNull()
})

test('selhání zálohy je vidět i po přepnutí pohledu během stahování (#1801)', async () => {
  let fail: (error: Error) => void = () => {}
  fullBackupMock.mockImplementationOnce(() => new Promise((_resolve, reject) => (fail = reject)))
  const first = renderSettings()
  fireEvent.click(
    await screen.findByRole('button', { name: 'Úplná záloha (data + databáze + nastavení)' }),
  )
  await waitFor(() => expect(fullBackupMock).toHaveBeenCalledOnce())
  first.unmount() // uživatel odešel na graf

  fail(new Error('Stahování skončilo předčasně: 100 z 9 000 MB'))
  await Promise.resolve()
  renderSettings()

  expect(await screen.findByText(/skončilo předčasně/)).toBeDefined()
})

test('běžící záloha zůstane po návratu do Settings zamčená a s průběhem (#1801)', async () => {
  fullBackupMock.mockImplementationOnce((onProgress) => {
    onProgress({ phase: 'preparing', receivedBytes: 0, totalBytes: null, remainingS: null })
    return new Promise(() => {})
  })
  const first = renderSettings()
  fireEvent.click(
    await screen.findByRole('button', { name: 'Úplná záloha (data + databáze + nastavení)' }),
  )
  await screen.findByText(/Připravuji balíček/)
  first.unmount()

  renderSettings()

  expect(await screen.findByText(/Připravuji balíček/)).toBeDefined()
  expect((screen.getByRole('button', { name: 'Zálohuji…' }) as HTMLButtonElement).disabled).toBe(
    true,
  )
  expect(
    (screen.getByRole('button', { name: 'Zálohovat PostgreSQL' }) as HTMLButtonElement).disabled,
  ).toBe(true)
})

test('selhání úplné zálohy zůstává vidět', async () => {
  fullBackupMock.mockRejectedValueOnce(
    new Error('Úplná záloha selhala: HTTP 409 Úplná záloha už běží'),
  )
  renderSettings()

  fireEvent.click(
    await screen.findByRole('button', { name: 'Úplná záloha (data + databáze + nastavení)' }),
  )

  expect(await screen.findByText(/HTTP 409 Úplná záloha už běží/)).toBeDefined()
})

test('Uložit je nedostupné, dokud není co uložit', async () => {
  renderSettings()
  const save = (await screen.findByRole('button', { name: 'Uložit' })) as HTMLButtonElement

  expect(save.disabled).toBe(true)

  fireEvent.change(screen.getByLabelText('Port'), { target: { value: '7497' } })
  expect(save.disabled).toBe(false)
})
