/** Gesta grafu nad PRODUKČNÍM bundlem (#1419, E-2a.6): kolečko a tažení mění
pohled jako dřív a pohled se persistuje jednou na konci gesta — kolečko po klidu,
pan na puštění tlačítka. Skutečné události prohlížeče (rAF, pointer capture),
které jsdom jen napodobí. REST je mockovaný jako ve smoke: graf kreslí demo den. */
import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

// Spec se typuje bez DOM knihovny (tsconfig.node) — tvar globálu prohlížeče ručně
type BrowserGlobal = typeof globalThis & {
  __gexlensWrites?: Record<string, number>
  localStorage: { getItem(key: string): string | null }
  Storage: { prototype: { setItem(key: string, value: string): void } }
}

/** Počty zápisů localStorage per klíč — obal `setItem` před startem aplikace. */
async function countStorageWrites(page: Page) {
  await page.addInitScript(() => {
    const browser = globalThis as BrowserGlobal
    const writes: Record<string, number> = {}
    browser.__gexlensWrites = writes
    const original = browser.Storage.prototype.setItem
    browser.Storage.prototype.setItem = function (key: string, value: string) {
      writes[key] = (writes[key] ?? 0) + 1
      return original.call(this, key, value)
    }
  })
}

const writesOf = (page: Page, key: string) =>
  page.evaluate((name) => (globalThis as BrowserGlobal).__gexlensWrites?.[name] ?? 0, key)
const stored = (page: Page, key: string) =>
  page.evaluate(
    (name) => JSON.parse((globalThis as BrowserGlobal).localStorage.getItem(name) ?? 'null'),
    key,
  )

test('kolečko a pan: pohled se mění, persistence jednou na konci gesta', async ({ page }) => {
  const errors: Error[] = []
  page.on('pageerror', (error) => errors.push(error))
  await countStorageWrites(page)
  await page.route('**/api/**', async (route) => {
    if (route.request().url().endsWith('/instruments/ES/expiries')) {
      await route.fulfill({ json: { expiries: ['20991231'] } })
      return
    }
    await route.fulfill({ status: 404, json: { detail: 'gestures mock' } })
  })
  await page.goto('/')

  const overlay = page.getByRole('img', { name: 'GEX heatmapa' })
  await expect(overlay).toBeVisible()
  const box = await overlay.boundingBox()
  if (!box) throw new Error('plátno heatmapy nemá rozměry')
  const cx = box.x + box.width / 2
  const cy = box.y + box.height / 2
  // Chvíle na fit demo dne a zápisy z mountu (výchozí hodnoty voleb)
  await page.waitForTimeout(500)
  const zoomBefore = await writesOf(page, 'gexlens.chartZoomX')
  const rangeBefore = await writesOf(page, 'gexlens.chartYRange')

  // Kolečko: 6 kroků dovnitř bez pauzy → během gesta žádný zápis
  await page.mouse.move(cx, cy)
  for (let step = 0; step < 6; step += 1) await page.mouse.wheel(0, -100)
  expect(await writesOf(page, 'gexlens.chartZoomX')).toBe(zoomBefore)
  await expect
    .poll(() => writesOf(page, 'gexlens.chartZoomX'), { timeout: 2_000 })
    .toBe(zoomBefore + 1)
  expect(await writesOf(page, 'gexlens.chartYRange')).toBe(rangeBefore + 1)
  const zoomed = (await stored(page, 'gexlens.chartZoomX')) as Record<string, number>
  expect(Object.values(zoomed)[0]).toBeGreaterThan(1.5) // zoom dovnitř se uplatnil
  const rangeAfterWheel = await stored(page, 'gexlens.chartYRange')

  // Pan: tažení přes 20 kroků → pásmo Y jednou na puštění, zoom X beze změny
  await page.mouse.move(cx, cy)
  await page.mouse.down()
  await page.mouse.move(cx + 120, cy + 60, { steps: 20 })
  expect(await writesOf(page, 'gexlens.chartYRange')).toBe(rangeBefore + 1)
  await page.mouse.up()
  await expect
    .poll(() => writesOf(page, 'gexlens.chartYRange'), { timeout: 2_000 })
    .toBe(rangeBefore + 2)
  expect(await writesOf(page, 'gexlens.chartZoomX')).toBe(zoomBefore + 1)
  expect(await stored(page, 'gexlens.chartYRange')).not.toEqual(rangeAfterWheel) // posun dolů

  expect(errors.map((error) => error.stack ?? error.message).join('\n')).toEqual('')
})
