/** Notifikace Telegram (#1175, #1284): přepínače z /push/status, zápis přes saveAll. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, expect, test, vi } from 'vitest'
import { PushSettings } from './PushSettings'

const STATUS = {
  configured: true,
  quiet_hours: '23:00-06:00',
  daily_cap: 200,
  sent_today: 3,
  last_sent_at: null,
  last_error: null,
  enabled: true,
  master: {
    setting: 'push_telegram_enabled',
    label: 'Posílat notifikace na Telegram',
    help: ['Hlavní vypínač všech zpráv na Telegram.', '', '• Výchozí: zapnuto'],
  },
  groups: [
    {
      key: 'market',
      label: 'Setupy a burza',
      topics: [
        {
          key: 'setup',
          setting: 'push_telegram_topic_setup',
          label: 'Nový setup',
          help: ['Vznikl obchodní setup.', '', '• Ve zvonku: setup', '• Výchozí: zapnuto'],
          enabled: true,
        },
        {
          key: 'news_anomaly',
          setting: 'push_telegram_topic_news_anomaly',
          label: 'Reakce trhu na zprávu',
          help: ['Trh zareagoval.', '', '• Ve zvonku: news_anomaly', '• Výchozí: zapnuto'],
          enabled: true,
        },
      ],
    },
    {
      key: 'app',
      label: 'Chování aplikace',
      topics: [
        {
          key: 'disk_low',
          setting: 'push_telegram_topic_disk_low',
          label: 'Málo místa pro Docker',
          help: ['Úklid Dockeru.', '', '• Ve zvonku: disk_low', '• Výchozí: vypnuto'],
          enabled: false,
        },
      ],
    },
  ],
}

/** Hlavní vypínač má jméno z viditelného popisku (WCAG 2.5.3), ne vlastní aria-label */
const MASTER = STATUS.master.label

function stubStatus(payload: unknown, ok = true) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok, json: async () => payload })),
  )
}

function checkbox(label: string): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement
}

/** Jako useServerSettings: lokálně se projeví jen hodnota, kterou server přijal */
function Harness({
  initial,
  save,
}: {
  initial: Record<string, unknown>
  save: (entries: [string, unknown][]) => Promise<void>
}) {
  const [values, setValues] = useState(initial)
  const saveAll = async (entries: [string, unknown][]) => {
    await save(entries)
    setValues((previous) => ({ ...previous, ...Object.fromEntries(entries) }))
  }
  return <PushSettings values={values} saveAll={saveAll} />
}

afterEach(() => {
  vi.unstubAllGlobals()
})

test('vykreslí hlavní vypínač a dvě skupiny přepínačů z /push/status', async () => {
  stubStatus(STATUS)
  render(<PushSettings values={{}} saveAll={async () => undefined} />)
  await waitFor(() =>
    expect(screen.getByTestId('push-status').textContent).toContain('dnes odesláno 3/200'),
  )
  expect(checkbox(MASTER).checked).toBe(true)
  const legends = Array.from(document.querySelectorAll('legend')).map((node) => node.textContent)
  expect(legends).toEqual(['Setupy a burza', 'Chování aplikace'])
  expect(checkbox('Telegram: Nový setup').checked).toBe(true)
  expect(checkbox('Telegram: Málo místa pro Docker').checked).toBe(false) // efektivní stav ze serveru
  expect(screen.getByText(/Zvoneček dostává vždy všechno/)).toBeTruthy()
})

test('uložená hodnota má přednost před efektivním stavem ze serveru', async () => {
  stubStatus(STATUS)
  render(
    <PushSettings
      values={{ push_telegram_topic_news_anomaly: false, push_telegram_topic_disk_low: true }}
      saveAll={async () => undefined}
    />,
  )
  await waitFor(() => expect(checkbox('Telegram: Reakce trhu na zprávu').checked).toBe(false))
  expect(checkbox('Telegram: Málo místa pro Docker').checked).toBe(true)
})

test('vypnutý hlavní vypínač zamkne skupiny a hodnoty přepínačů nechá', async () => {
  stubStatus(STATUS)
  const saveAll = vi.fn(async () => undefined)
  render(
    <PushSettings
      values={{ push_telegram_enabled: false, push_telegram_topic_disk_low: true }}
      saveAll={saveAll}
    />,
  )
  await waitFor(() => expect(checkbox(MASTER).checked).toBe(false))
  for (const label of ['Telegram: Nový setup', 'Telegram: Málo místa pro Docker']) {
    const input = checkbox(label)
    expect(input.closest('fieldset')?.disabled).toBe(true)
    expect(input.matches(':disabled')).toBe(true)
  }
  expect(checkbox('Telegram: Nový setup').checked).toBe(true)
  expect(checkbox('Telegram: Málo místa pro Docker').checked).toBe(true)
  expect(checkbox(MASTER).matches(':disabled')).toBe(false)
})

test('klik uloží klíč přepínače a checkbox se přepne až po přijetí serverem', async () => {
  stubStatus(STATUS)
  let resolveSave: () => void = () => undefined
  const save = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        resolveSave = resolve
      }),
  )
  render(<Harness initial={{}} save={save} />)
  await waitFor(() => expect(checkbox('Telegram: Reakce trhu na zprávu').checked).toBe(true))
  fireEvent.click(checkbox('Telegram: Reakce trhu na zprávu'))
  expect(save).toHaveBeenCalledWith([['push_telegram_topic_news_anomaly', false]])
  expect(checkbox('Telegram: Reakce trhu na zprávu').checked).toBe(true) // ještě neuloženo
  await act(async () => resolveSave())
  expect(checkbox('Telegram: Reakce trhu na zprávu').checked).toBe(false)

  fireEvent.click(checkbox(MASTER))
  expect(save).toHaveBeenLastCalledWith([['push_telegram_enabled', false]])
})

test('odmítnutý zápis ukáže chybu a checkbox se nepřepne', async () => {
  stubStatus(STATUS)
  const save = vi.fn(async () => {
    throw new Error('Uložení nastavení push_telegram_topic_setup selhalo: HTTP 422')
  })
  render(<Harness initial={{}} save={save} />)
  await waitFor(() => expect(checkbox('Telegram: Nový setup').checked).toBe(true))
  fireEvent.click(checkbox('Telegram: Nový setup'))
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('HTTP 422'))
  expect(checkbox('Telegram: Nový setup').checked).toBe(true)
})

test('tooltip přepínače je víceřádkový s odrážkami', async () => {
  stubStatus(STATUS)
  render(<PushSettings values={{}} saveAll={async () => undefined} />)
  await waitFor(() => expect(checkbox('Telegram: Nový setup')).toBeTruthy())
  const title = checkbox('Telegram: Nový setup').closest('label')?.getAttribute('title') ?? ''
  expect(title).toContain('\n•')
  expect(title.startsWith('Vznikl obchodní setup.\n\n')).toBe(true)
  expect(checkbox(MASTER).closest('label')?.getAttribute('title')).toContain('\n•')
})

test('přístupné jméno obsahuje viditelný popisek a tooltip je přístupný popis', async () => {
  stubStatus(STATUS)
  render(<PushSettings values={{}} saveAll={async () => undefined} />)
  await waitFor(() => expect(checkbox(MASTER)).toBeTruthy())
  // Jméno = viditelný text popisku; popis = tooltip, aby ho četla i čtečka
  expect(
    screen.getByRole('checkbox', { name: MASTER, description: /^Hlavní vypínač/ }),
  ).toBeTruthy()
  expect(
    screen.getByRole('checkbox', {
      name: 'Telegram: Nový setup',
      description: /^Vznikl obchodní setup\./,
    }),
  ).toBeTruthy()
})

test('bez API ukáže hlášku a žádné přepínače', async () => {
  stubStatus(null, false)
  render(<PushSettings values={{}} saveAll={async () => undefined} />)
  await waitFor(() =>
    expect(screen.getByTestId('push-status').textContent).toContain('nejde načíst'),
  )
  expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
})

test('starší API bez přepínačů = jako nedostupné, ne pád', async () => {
  stubStatus({ ...STATUS, enabled: undefined, groups: undefined, categories: { ops: true } })
  render(<PushSettings values={{}} saveAll={async () => undefined} />)
  await waitFor(() =>
    expect(screen.getByTestId('push-status').textContent).toContain('nejde načíst'),
  )
  expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
})

test('bez bota řekne, kam patří údaje', async () => {
  stubStatus({ ...STATUS, configured: false, quiet_hours: '', daily_cap: 0, sent_today: 0 })
  render(<PushSettings values={{}} saveAll={async () => undefined} />)
  await waitFor(() =>
    expect(screen.getByTestId('push-status').textContent).toContain('není nastaven'),
  )
})
