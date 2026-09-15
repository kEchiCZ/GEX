/** Push nastavení (#1175): stav z API, přepínač ukládá serverové nastavení. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { PushSettings } from './PushSettings'

afterEach(() => {
  vi.unstubAllGlobals()
})

test('ukazuje stav bota a přepínač zapisuje push_telegram_<kategorie>', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      json: async () => ({
        configured: true,
        quiet_hours: '23:00-06:00',
        daily_cap: 200,
        sent_today: 3,
        last_sent_at: null,
        last_error: null,
        categories: { setup: true, ops: true, news: true, info: false },
      }),
    })),
  )
  const put = vi.fn()
  render(<PushSettings values={{ push_telegram_news: false }} put={put} />)
  await waitFor(() =>
    expect(screen.getByTestId('push-status').textContent).toContain('dnes odesláno 3/200'),
  )
  const news = screen.getByLabelText('Push: Zprávy') as HTMLInputElement
  expect(news.checked).toBe(false) // uložená hodnota má přednost před stavem z API
  expect((screen.getByLabelText('Push: Ostatní') as HTMLInputElement).checked).toBe(false)
  fireEvent.click(news)
  expect(put).toHaveBeenCalledWith('push_telegram_news', true)
})

test('bez bota řekne, kam patří údaje', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      json: async () => ({
        configured: false,
        quiet_hours: '',
        daily_cap: 0,
        sent_today: 0,
        last_sent_at: null,
        last_error: null,
        categories: {},
      }),
    })),
  )
  render(<PushSettings values={{}} put={() => undefined} />)
  await waitFor(() =>
    expect(screen.getByTestId('push-status').textContent).toContain('není nastaven'),
  )
})
