/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Zrcadlo produkčního nginx (#542): klient volá relativní /api, proxy ho pošle
// na API. Platí pro dev server (`npm run dev` proti `make run-api`) i pro
// `vite preview`, nad kterým jede Playwright smoke — ten potřebuje, aby WS
// končil na portu API (tam si staví TCP tarpit, #154).
const apiProxy = {
  '/api': {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
    ws: true,
    rewrite: (path: string) => path.replace(/^\/api/, ''),
  },
}

// Konfigurace Vite + Vitest (jsdom pro testy komponent)
export default defineConfig({
  plugins: [react()],
  server: { proxy: apiProxy },
  preview: { proxy: apiProxy },
  test: {
    environment: 'jsdom',
    globals: true, // testing-library auto-cleanup mezi testy
    setupFiles: ['src/test/setup.ts'],
    // Jen jednotkové testy v src — e2e/*.spec.ts patří Playwrightu (#154)
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
