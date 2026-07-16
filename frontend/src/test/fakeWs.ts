/** Testovací náhrada WebSocketu pro LiveSocket. */

export class FakeWebSocket {
  static instances: FakeWebSocket[] = []

  sent: string[] = []
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: (() => void) | null = null

  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }

  static reset(): void {
    FakeWebSocket.instances = []
  }

  static latest(): FakeWebSocket {
    const instance = FakeWebSocket.instances.at(-1)
    if (!instance) throw new Error('Žádná FakeWebSocket instance')
    return instance
  }

  send(payload: string): void {
    this.sent.push(payload)
  }

  close(): void {
    this.closed = true
    this.onclose?.()
  }

  open(): void {
    this.onopen?.()
  }

  push(channel: string, data: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify({ channel, data }) })
  }
}
