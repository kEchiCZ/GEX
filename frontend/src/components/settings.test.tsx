/** Testy Settings: koncept změn a tlačítko Uložit (#445). */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { SettingsView } from './SettingsView'
import { AppStateProvider } from '../state/AppState'

const puts: { key: string; value: unknown }[] = []

vi.mock('../api/settings', () => ({
  useServerSettings: () => ({
    values: { ibkr_port: 7496, retention_days: 90 },
    put: (key: string, value: unknown) => puts.push({ key, value }),
  }),
}))

afterEach(() => {
  puts.length = 0
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

  expect(puts).toEqual([
    { key: 'ibkr_port', value: 7497 },
    { key: 'retention_days', value: 120 },
  ])
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Uloženo'))
})

test('Zahodit změny vrátí původní hodnoty a nic neodešle', async () => {
  renderSettings()
  const port = await screen.findByLabelText('Port')
  fireEvent.change(port, { target: { value: '4001' } })

  fireEvent.click(screen.getByRole('button', { name: 'Zahodit změny' }))

  expect(puts).toHaveLength(0)
  expect((screen.getByLabelText('Port') as HTMLInputElement).value).toBe('7496')
})

test('Uložit je nedostupné, dokud není co uložit', async () => {
  renderSettings()
  const save = (await screen.findByRole('button', { name: 'Uložit' })) as HTMLButtonElement

  expect(save.disabled).toBe(true)

  fireEvent.change(screen.getByLabelText('Port'), { target: { value: '7497' } })
  expect(save.disabled).toBe(false)
})
