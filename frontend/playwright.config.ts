/** Playwright smoke nad PRODUKČNÍM bundlem (#154).

Vyžaduje hotový `npm run build` (dist/) — `vite preview` jen servíruje výsledek.
Produkční build je jiné prostředí než dev (bez StrictMode, minifikace, jiné
časování efektů): #146 shazoval produkci na bílou stránku, zatímco dev i jsdom
testy byly zelené. Tenhle smoke tu třídu chyb chytá.
*/
import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: 'e2e',
  use: { baseURL: 'http://127.0.0.1:4173' },
  webServer: {
    command: 'npm run preview -- --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173',
    // Port 4173 patří jen preview (dev server běží na 5173) — reuse je bezpečný
    reuseExistingServer: true,
    timeout: 30_000,
  },
})
