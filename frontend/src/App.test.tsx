import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import App from './App'

test('vykreslí nadpis aplikace', () => {
  render(<App />)
  expect(screen.getByText('GEXLens')).toBeDefined()
})
