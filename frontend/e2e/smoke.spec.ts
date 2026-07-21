/** Smoke produkčního bundlu (#154): stránka se vyrenderuje a nevyhodí pageerror.

Reprodukuje časování, kterým #146 shazoval PRODUKCI na bílou stránku, zatímco
dev i jsdom testy byly zelené:

- Na portu API běží TCP „tarpit": spojení přijme a mlčí → WebSocket zůstane
  viset ve stavu CONNECTING po celý test (žádné onopen/onclose).
- REST je mockovaný přes page.route, takže expirace dorazí okamžitě → frontend
  volá subscribe kanálů v okně, kdy je socket prokazatelně CONNECTING.
  Chybný send() v tom stavu = InvalidStateError = pageerror + zmizelá aplikace.

Lokálně může na portu běžet skutečné API (EADDRINUSE) — pak se jede bez
tarpitu a test degraduje na kontrolu „bundle se načte a nespadne", což je
pořád víc, než dělal CI do #154.
*/
import net from 'node:net'
import { expect, test } from '@playwright/test'

/** Default z frontend/src/config.ts (VITE_API_BASE se v produkčním buildu CI nenastavuje).
Lokální ladění proti buildu s jiným VITE_API_BASE: SMOKE_API_PORT=18000 npm run smoke */
const API_PORT = Number(process.env.SMOKE_API_PORT ?? 8000)

let tarpit: net.Server | null = null
const tarpitSockets = new Set<net.Socket>()

test.beforeAll(async () => {
  tarpit = await new Promise((resolve) => {
    const server = net.createServer((socket) => {
      // přijmout a mlčet — WS handshake nikdy nedoběhne
      tarpitSockets.add(socket)
      socket.on('close', () => tarpitSockets.delete(socket))
    })
    server.once('error', () => resolve(null)) // port obsazený (lokální API) → bez tarpitu
    server.listen(API_PORT, '127.0.0.1', () => resolve(server))
  })
})

test.afterAll(async () => {
  const server = tarpit
  if (!server) return
  // Visící WS spojení by close() drželo do nekonečna — zabít natvrdo
  for (const socket of tarpitSockets) socket.destroy()
  await new Promise((resolve) => server.close(resolve))
})

test('produkční bundle se načte, přežije subscribe během CONNECTING a nevyhodí pageerror', async ({
  page,
}) => {
  const errors: Error[] = []
  page.on('pageerror', (error) => errors.push(error))

  // REST mock: expirace hned (spouští subscribe WS kanálů), zbytek 404 jako
  // chybějící API — ty cesty musí aplikace přežít (fallback na demo data)
  await page.route(`http://127.0.0.1:${API_PORT}/**`, async (route) => {
    if (route.request().url().endsWith('/instruments/ES/expiries')) {
      await route.fulfill({ json: { expiries: ['20991231'] } })
      return
    }
    await route.fulfill({ status: 404, json: { detail: 'smoke mock' } })
  })

  await page.goto('/')

  // Aplikace se vyrenderovala — bílá stránka (#146) by tu spadla
  await expect(page.getByTestId('data-source')).toBeVisible()

  // Chvíle navíc na opožděné chyby (subscribe po příchodu expirací, efekty po mountu)
  await page.waitForTimeout(1500)
  await expect(page.getByTestId('data-source')).toBeVisible() // nespadla ani dodatečně
  expect(errors.map((error) => error.stack ?? error.message).join('\n')).toEqual('')
})
