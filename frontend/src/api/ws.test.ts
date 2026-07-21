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

test('subscribe během CONNECTING nevyhodí a kanál se pošle po otevření (#146)', () => {
  const socket = makeSocket()
  socket.connect() // socket existuje, ale je teprve CONNECTING
  // Tohle v produkci shazovalo celý React strom InvalidStateError
  expect(() => socket.subscribe('snapshot.ES.20260716', () => {})).not.toThrow()
  expect(FakeWebSocket.latest().sent).toHaveLength(0)

  FakeWebSocket.latest().open()
  expect(JSON.parse(FakeWebSocket.latest().sent[0])).toEqual({
    action: 'subscribe',
    channels: ['snapshot.ES.20260716'],
  })
})

test('subscribe nad otevřeným socketem pošle kanál hned', () => {
  const socket = makeSocket()
  socket.connect()
  FakeWebSocket.latest().open()
  socket.subscribe('spot.ES', () => {})
  expect(JSON.parse(FakeWebSocket.latest().sent.at(-1)!)).toEqual({
    action: 'subscribe',
    channels: ['spot.ES'],
  })
})

test('po výpadku spojení se subscribe nepokouší posílat do zavřeného socketu (#146)', () => {
  const socket = makeSocket()
  socket.connect()
  const ws = FakeWebSocket.latest()
  ws.open()
  ws.onclose?.() // výpadek serveru — socket už není OPEN
  expect(() => socket.subscribe('flow.ES', () => {})).not.toThrow()
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

/** Fake, jehož close() NEvyvolá onclose synchronně — jako reálný prohlížeč (#153). */
class DeferredCloseWebSocket extends FakeWebSocket {
  close(): void {
    this.closed = true
    this.opened = false // onclose doručí až test ručně
  }
}

test('opožděný onclose starého socketu po close()+connect() nespustí reconnect (#153)', () => {
  vi.useFakeTimers()
  try {
    const socket = new LiveSocket('ws://test/ws/live', {
      webSocketFactory: (url) => new DeferredCloseWebSocket(url),
      reconnectDelayMs: 10,
    })
    const received: unknown[] = []
    socket.subscribe('status', (data) => received.push(data))

    socket.connect() // mount (StrictMode)
    socket.close() // cleanup — onclose od prohlížeče dorazí až později
    socket.connect() // remount
    expect(FakeWebSocket.instances).toHaveLength(2)

    const [ws1, ws2] = FakeWebSocket.instances
    ws2.open()
    ws1.onclose?.() // teprve teď dorazí onclose zavřeného ws1

    vi.advanceTimersByTime(50)
    // Bez identity guardu by ws1.onclose naplánoval reconnect → třetí socket
    expect(FakeWebSocket.instances).toHaveLength(2)

    // Zprávy ze starého socketu se nesmí doručovat (duplicitní dispatch)
    ws1.push('status', { stale: true })
    ws2.push('status', { live: true })
    expect(received).toEqual([{ live: true }])
  } finally {
    vi.useRealTimers()
  }
})

test('connect() přes otevřený socket resetuje isOpen — subscribe neposílá do CONNECTING (#153)', () => {
  const socket = makeSocket()
  socket.connect()
  FakeWebSocket.latest().open() // isOpen = true
  socket.connect() // nový socket je CONNECTING; starý stav nesmí přežít
  // FakeWebSocket.send před otevřením vyhazuje jako prohlížeč (#146)
  expect(() => socket.subscribe('spot.ES', () => {})).not.toThrow()
  expect(FakeWebSocket.latest().sent).toHaveLength(0)

  FakeWebSocket.latest().open()
  expect(JSON.parse(FakeWebSocket.latest().sent[0])).toEqual({
    action: 'subscribe',
    channels: ['spot.ES'],
  })
})

test('po reconnectu onopen pošle i kanály přihlášené během výpadku (#153)', () => {
  vi.useFakeTimers()
  try {
    const socket = makeSocket()
    socket.subscribe('status', () => {})
    socket.connect()
    FakeWebSocket.latest().open()

    FakeWebSocket.latest().onclose?.() // výpadek serveru
    socket.subscribe('flow.ES', () => {}) // subskripce během výpadku
    vi.advanceTimersByTime(20) // reconnect

    const ws = FakeWebSocket.latest()
    ws.open()
    expect(JSON.parse(ws.sent[0])).toEqual({
      action: 'subscribe',
      channels: ['status', 'flow.ES'],
    })
  } finally {
    vi.useRealTimers()
  }
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
