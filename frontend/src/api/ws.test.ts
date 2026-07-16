import { beforeEach, expect, test, vi } from 'vitest'
import { FakeWebSocket } from '../test/fakeWs'
import { LiveSocket } from './ws'

function makeSocket(): LiveSocket {
  return new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
    reconnectDelayMs: 10,
  })
}

beforeEach(() => FakeWebSocket.reset())

test('po otevření pošle subscribe se všemi kanály', () => {
  const socket = makeSocket()
  socket.subscribe('status', () => {})
  socket.subscribe('price.ES', () => {})
  socket.connect()

  FakeWebSocket.latest().open()

  expect(JSON.parse(FakeWebSocket.latest().sent[0])).toEqual({
    action: 'subscribe',
    channels: ['status', 'price.ES'],
  })
})

test('routuje zprávy podle kanálu včetně wildcard', () => {
  const socket = makeSocket()
  const statusData: unknown[] = []
  const levelsData: unknown[] = []
  socket.subscribe('status', (data) => statusData.push(data))
  socket.subscribe('levels.*', (data) => levelsData.push(data))
  socket.connect()
  const ws = FakeWebSocket.latest()
  ws.open()

  ws.push('status', { engine: 'online' })
  ws.push('levels.ES.20260716', { flip: 7660 })
  ws.push('flow.ES', { cum: 1 }) // nikdo neposlouchá

  expect(statusData).toEqual([{ engine: 'online' }])
  expect(levelsData).toEqual([{ flip: 7660 }])
})

test('po neplánovaném zavření se znovu připojí', async () => {
  vi.useFakeTimers()
  try {
    const socket = makeSocket()
    socket.subscribe('status', () => {})
    socket.connect()
    expect(FakeWebSocket.instances).toHaveLength(1)

    FakeWebSocket.latest().onclose?.() // výpadek serveru
    vi.advanceTimersByTime(20)

    expect(FakeWebSocket.instances).toHaveLength(2)

    socket.close() // uživatelské zavření už reconnect nespouští
    vi.advanceTimersByTime(50)
    expect(FakeWebSocket.instances).toHaveLength(2)
  } finally {
    vi.useRealTimers()
  }
})
