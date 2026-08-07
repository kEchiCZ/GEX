/** Testy Settings: koncept změn a tlačítko Uložit (#445). */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { SettingsView } from './SettingsView'
import { AppStateProvider } from '../state/AppState'

const puts: { key: string; value: unknown }[] = []
// Serverové hodnoty per test — výchozí stav simuluje načtené settings
let serverValues: Record<string, unknown> = {}
// Chyba, kterou má saveAll vrátit (null = uložení projde)
let saveFailure: Error | null = null
const backupMock = vi.fn<() => Promise<'saved' | 'downloaded'>>()

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

vi.mock('../api/backup', () => ({
  downloadBackup: () => backupMock(),
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

test('Uložit je nedostupné, dokud není co uložit', async () => {
  renderSettings()
  const save = (await screen.findByRole('button', { name: 'Uložit' })) as HTMLButtonElement

  expect(save.disabled).toBe(true)

  fireEvent.change(screen.getByLabelText('Port'), { target: { value: '7497' } })
  expect(save.disabled).toBe(false)
})
