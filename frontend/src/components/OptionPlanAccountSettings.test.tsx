/** Settings → Plán opce QQQ — účet (F5a #1746, ADR-0062 K2 A): bez výchozí hodnoty, prázdné =
nenastaveno, validace kladného čísla jako na serveru, chyba serveru viditelně. Čísla jsou
syntetická (skutečný účet do repa nepatří). */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { OptionPlanAccountSettings } from './OptionPlanAccountSettings'
import { parseAccountInput } from '../api/optionPlans'

const ACCOUNT = 'option_plan_account_usd'
const MAX_LOSS = 'option_plan_max_loss_usd'
const ACCOUNT_LABEL = 'Velikost účtu ($)'
const MAX_LOSS_LABEL = 'Rozpočet na obchod — max. čerpání P4 ($ na kontrakt)'

function input(label: string): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement
}

function saveButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Uložit účet plánu' }) as HTMLButtonElement
}

test('parseAccountInput: prázdné = nenastaveno, kladné číslo i s čárkou, jinak neplatné', () => {
  expect(parseAccountInput('  ')).toBeNull()
  expect(parseAccountInput('97,5')).toBe(97.5)
  expect(parseAccountInput('12345')).toBe(12345)
  for (const bad of ['0', '-5', 'abc', 'Infinity', '1e999']) {
    expect(parseAccountInput(bad)).toBeUndefined()
  }
})

test('bez hodnot ze serveru: prázdná pole s „nenastaveno“, žádný výchozí údaj', () => {
  render(<OptionPlanAccountSettings values={{}} saveAll={vi.fn()} />)
  expect(input(ACCOUNT_LABEL).value).toBe('')
  expect(input(MAX_LOSS_LABEL).value).toBe('')
  expect(screen.getByTestId(`account-unset-${ACCOUNT}`).textContent).toBe('nenastaveno')
  expect(screen.getByTestId(`account-unset-${MAX_LOSS}`).textContent).toBe('nenastaveno')
  expect(saveButton().disabled).toBe(true) // nic se nezměnilo
})

test('uložené hodnoty ze serveru se ukážou, null zůstane prázdné', () => {
  render(<OptionPlanAccountSettings values={{ [ACCOUNT]: 4321, [MAX_LOSS]: null }} saveAll={vi.fn()} />) // prettier-ignore
  expect(input(ACCOUNT_LABEL).value).toBe('4321')
  expect(input(MAX_LOSS_LABEL).value).toBe('')
})

test('neplatná hodnota: hláška u pole a Uložit nejde', () => {
  const saveAll = vi.fn()
  render(<OptionPlanAccountSettings values={{}} saveAll={saveAll} />)
  for (const bad of ['0', '-5', 'abc']) {
    fireEvent.change(input(MAX_LOSS_LABEL), { target: { value: bad } })
    expect(input(MAX_LOSS_LABEL).getAttribute('aria-invalid')).toBe('true')
    expect(screen.getByTestId(`account-invalid-${MAX_LOSS}`)).toBeTruthy()
    expect(saveButton().disabled).toBe(true)
  }
  expect(saveAll).not.toHaveBeenCalled()
})

test('Uložit pošle obě hodnoty: číslo a prázdné jako null', async () => {
  const saveAll = vi.fn(() => Promise.resolve())
  render(<OptionPlanAccountSettings values={{}} saveAll={saveAll} />)
  fireEvent.change(input(ACCOUNT_LABEL), { target: { value: '2468' } })
  fireEvent.change(input(MAX_LOSS_LABEL), { target: { value: '' } })
  fireEvent.click(saveButton())
  await waitFor(() => expect(saveAll).toHaveBeenCalledTimes(1))
  expect(saveAll).toHaveBeenCalledWith([
    [ACCOUNT, 2468],
    [MAX_LOSS, null],
  ])
  expect(await screen.findByText(/^Uloženo\./)).toBeTruthy()
})

test('chyba serveru (422) je vidět a rozepsaná hodnota zůstane', async () => {
  const saveAll = vi.fn(() => Promise.reject(new Error('Uložení nastavení selhalo: HTTP 422')))
  render(<OptionPlanAccountSettings values={{}} saveAll={saveAll} />)
  fireEvent.change(input(MAX_LOSS_LABEL), { target: { value: '75' } })
  fireEvent.click(saveButton())
  expect(
    await screen.findByText('Uložení selhalo: Uložení nastavení selhalo: HTTP 422'),
  ).toBeTruthy()
  expect(input(MAX_LOSS_LABEL).value).toBe('75')
})
