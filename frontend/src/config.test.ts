import { describe, expect, it } from 'vitest'

import { wsUrlFor } from './config'

describe('wsUrlFor', () => {
  it('relativní základ bere origin ze stránky (produkce za nginx, #542)', () => {
    expect(wsUrlFor('/api', 'http://gexlens.local:8080')).toBe(
      'ws://gexlens.local:8080/api/ws/live',
    )
  })

  it('https origin dá wss', () => {
    expect(wsUrlFor('/api', 'https://gexlens.local')).toBe('wss://gexlens.local/api/ws/live')
  })

  it('absolutní základ origin ignoruje (dev proti API mimo Docker)', () => {
    expect(wsUrlFor('http://127.0.0.1:8000', 'http://127.0.0.1:5173')).toBe(
      'ws://127.0.0.1:8000/ws/live',
    )
  })

  it('koncové lomítko originu se nezdvojí', () => {
    expect(wsUrlFor('/api', 'http://127.0.0.1:8080/')).toBe('ws://127.0.0.1:8080/api/ws/live')
  })
})
