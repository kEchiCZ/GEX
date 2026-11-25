/** Smoke tlačítka Úplná záloha nad PRODUKČNÍM bundlem (#1801).

Obě cesty ukládání se skutečnými streamy prohlížeče, které jsdom jen napodobí:

- Chrome/Edge: `showSaveFilePicker` (nahrazený zapisovačem, který bajty jen
  počítá — nativní dialog headless neotevře) a stream odpovědi do souboru;
- jiný prohlížeč (mobil): nativní stažení s jednorázovým lístkem v URL.

REST je mockovaný; API token z localStorage nesmí skončit v žádné URL.
*/
import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

const TOKEN = 'smoke-token-tajne'
const TICKET = 'smoke-listek-jednou'
const FILENAME = 'gexlens-zaloha-2026-11-25_2210-cd7f1d9.tar'
const BODY = Buffer.alloc(3 * 1024 * 1024, 7) // 3 MB, víc kusů streamu

// Spec se typuje bez DOM knihovny (tsconfig.node) — tvar globálu prohlížeče ručně
type BrowserGlobal = typeof globalThis & {
  __written?: number
  __closed?: boolean
  localStorage: { setItem(key: string, value: string): void }
  showSaveFilePicker?: unknown
}

async function mockApi(page: Page, requests: { url: string; token: string | null }[]) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = request.url()
    requests.push({ url, token: request.headers()['x-gexlens-token'] ?? null })
    if (url.endsWith('/instruments/ES/expiries')) {
      await route.fulfill({ json: { expiries: ['20991231'] } })
    } else if (request.method() === 'POST' && url.endsWith('/backup/full/ticket')) {
      await route.fulfill({ json: { ticket: TICKET, filename: FILENAME } })
    } else if (/\/backup\/full(\?|$)/.test(url)) {
      await route.fulfill({
        body: BODY,
        headers: {
          'Content-Type': 'application/x-tar',
          'Content-Length': String(BODY.length),
          'Content-Disposition': `attachment; filename="${FILENAME}"`,
        },
      })
    } else {
      await route.fulfill({ status: 404, json: { detail: 'backup smoke mock' } })
    }
  })
}

async function openBackup(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Settings', exact: true }).click()
  return page.getByRole('button', { name: 'Úplná záloha (data + databáze + nastavení)' })
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => {
    ;(globalThis as BrowserGlobal).localStorage.setItem('gexlens.apiToken', token)
  }, TOKEN)
})

test('Chrome: dialog „Uložit jako" a stream celé odpovědi do souboru', async ({ page }) => {
  const errors: Error[] = []
  page.on('pageerror', (error) => errors.push(error))
  await page.addInitScript(() => {
    const browser = globalThis as BrowserGlobal
    browser.__written = 0
    browser.showSaveFilePicker = async () => ({
      createWritable: async () =>
        new WritableStream<Uint8Array>({
          write(chunk) {
            browser.__written = (browser.__written ?? 0) + chunk.byteLength
          },
          close() {
            browser.__closed = true
          },
        }),
    })
  })
  const requests: { url: string; token: string | null }[] = []
  await mockApi(page, requests)

  await (await openBackup(page)).click()

  await expect(page.getByText('Hotovo — úplná záloha uložena na zvolené místo.')).toBeVisible()
  const written = await page.evaluate(() => (globalThis as BrowserGlobal).__written)
  expect(written).toBe(BODY.length)
  expect(await page.evaluate(() => (globalThis as BrowserGlobal).__closed)).toBe(true)
  const backup = requests.filter((r) => r.url.includes('/backup/'))
  expect(backup.map((r) => new URL(r.url).pathname)).toEqual([
    '/api/backup/full/ticket',
    '/api/backup/full',
  ])
  expect(backup.every((r) => r.token === TOKEN)).toBe(true)
  expect(requests.some((r) => r.url.includes(TOKEN))).toBe(false)
  expect(errors.map((error) => error.message).join('\n')).toEqual('')
})

test('bez File System Access API: nativní stažení s lístkem, token v URL ne', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(globalThis, 'showSaveFilePicker', { value: undefined })
  })
  const requests: { url: string; token: string | null }[] = []
  await mockApi(page, requests)
  const button = await openBackup(page)

  // Stažení odkazem s `download` jde v Chromiu mimo page.route (v logu chyba proxy
  // vite preview) — tady se ověřuje adresa a jméno, obsah pokrývá pytest endpointu
  const [download] = await Promise.all([page.waitForEvent('download'), button.click()])

  expect(download.suggestedFilename()).toBe(FILENAME)
  const url = new URL(download.url())
  expect(url.pathname).toBe('/api/backup/full')
  expect(url.searchParams.get('ticket')).toBe(TICKET)
  expect(download.url()).not.toContain(TOKEN)
  await expect(page.getByText(/Stahování úplné zálohy převzal prohlížeč/)).toBeVisible()
})
