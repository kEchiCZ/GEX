/** Klient /ws/live (SPEC kap. 6): subscribe protokol, routing kanálů, auto-reconnect. */

export type ChannelData = Record<string, unknown>
export type ChannelHandler = (data: ChannelData) => void

interface WebSocketLike {
  send(payload: string): void
  close(): void
  onopen: (() => void) | null
  onmessage: ((event: { data: string }) => void) | null
  onclose: (() => void) | null
}

export interface LiveSocketOptions {
  /** Testovatelnost: náhrada za window.WebSocket. */
  webSocketFactory?: (url: string) => WebSocketLike
  reconnectDelayMs?: number
}

export class LiveSocket {
  private handlers = new Map<string, Set<ChannelHandler>>()
  private ws: WebSocketLike | null = null
  private closedByUser = false
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null

  constructor(
    private url: string,
    private options: LiveSocketOptions = {},
  ) {}

  connect(): void {
    this.closedByUser = false
    const factory =
      this.options.webSocketFactory ??
      ((url: string) => new WebSocket(url) as unknown as WebSocketLike)
    const ws = factory(this.url)
    this.ws = ws
    ws.onopen = () => {
      const channels = [...this.handlers.keys()]
      if (channels.length > 0) {
        ws.send(JSON.stringify({ action: 'subscribe', channels }))
      }
    }
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data) as { channel?: string; data?: ChannelData }
      if (!message.channel || message.data === undefined) return // ack/error rámce
      for (const handler of this.handlers.get(message.channel) ?? []) {
        handler(message.data)
      }
      // Wildcard subskripce (`levels.*`)
      for (const [pattern, handlers] of this.handlers) {
        if (pattern.endsWith('.*') && message.channel.startsWith(pattern.slice(0, -1))) {
          for (const handler of handlers) handler(message.data)
        }
      }
    }
    ws.onclose = () => {
      if (this.closedByUser) return
      this.reconnectTimer = setTimeout(() => this.connect(), this.options.reconnectDelayMs ?? 2000)
    }
  }

  subscribe(channel: string, handler: ChannelHandler): void {
    const isNew = !this.handlers.has(channel)
    let set = this.handlers.get(channel)
    if (!set) {
      set = new Set()
      this.handlers.set(channel, set)
    }
    set.add(handler)
    if (isNew && this.ws) {
      this.ws.send(JSON.stringify({ action: 'subscribe', channels: [channel] }))
    }
  }

  close(): void {
    this.closedByUser = true
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer)
    this.ws?.close()
  }
}
