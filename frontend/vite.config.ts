/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Konfigurace Vite + Vitest (jsdom pro testy komponent)
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true, // testing-library auto-cleanup mezi testy
    setupFiles: ['src/test/setup.ts'],
    // Jen jednotkové testy v src — e2e/*.spec.ts patří Playwrightu (#154)
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
