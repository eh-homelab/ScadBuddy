import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RealtimeClient, type RealtimeSignal } from './realtime'

/** A WebSocket the test drives: it opens, closes and delivers frames on command. */
class FakeSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: FakeSocket[] = []

  readyState = FakeSocket.CONNECTING
  sent: unknown[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  readonly url: string

  constructor(url: string) {
    this.url = url
    FakeSocket.instances.push(this)
  }

  send(data: string) {
    this.sent.push(JSON.parse(data))
  }

  close() {
    if (this.readyState === FakeSocket.CLOSED) return
    this.readyState = FakeSocket.CLOSED
    this.onclose?.()
  }

  open() {
    this.readyState = FakeSocket.OPEN
    this.onopen?.()
  }

  deliver(frame: object) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }

  /** Answers every subscribe the client has sent so far. */
  confirm() {
    for (const frame of this.sent as { type: string; topics: string[] }[]) {
      if (frame.type === 'subscribe') this.deliver({ type: 'subscribed', topics: frame.topics })
    }
  }
}

function last(): FakeSocket {
  const socket = FakeSocket.instances.at(-1)
  if (!socket) throw new Error('no socket was opened')
  return socket
}

function client(options: ConstructorParameters<typeof RealtimeClient>[0] = {}) {
  return new RealtimeClient({
    url: 'ws://test/api/v1/ws',
    WebSocketImpl: FakeSocket as unknown as typeof WebSocket,
    random: () => 1,
    ...options,
  })
}

const event = (kind: string, topics: string[]) => ({
  type: 'event',
  id: `e-${kind}`,
  kind,
  topics,
  data: {},
})

beforeEach(() => {
  FakeSocket.instances = []
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('RealtimeClient', () => {
  it('connects on the first subscription and subscribes once open', () => {
    const realtime = client()
    expect(FakeSocket.instances).toHaveLength(0)
    realtime.subscribe('models', () => {})
    expect(FakeSocket.instances).toHaveLength(1)
    last().open()
    expect(last().sent).toEqual([{ type: 'subscribe', topics: ['models'] }])
    expect(realtime.status).toBe('live')
  })

  it('signals resync on confirmation, then delivers events for the topic only', () => {
    const realtime = client()
    const seen: RealtimeSignal[] = []
    realtime.subscribe('model:demo', (signal) => seen.push(signal))
    last().open()
    last().confirm()
    last().deliver(event('model.updated', ['model:demo']))
    last().deliver(event('font.installed', ['fonts']))
    expect(seen).toEqual(['resync', expect.objectContaining({ kind: 'model.updated' })])
  })

  it('sends one subscribe per topic however many follow it, and unsubscribes after the last', async () => {
    const realtime = client()
    realtime.subscribe('fonts', () => {})
    last().open()
    last().confirm()
    const second: RealtimeSignal[] = []
    const stopSecond = realtime.subscribe('fonts', (signal) => second.push(signal))
    // A late follower of a live topic still gets its first read.
    await vi.runAllTicks()
    await Promise.resolve()
    expect(second).toEqual(['resync'])
    const stopFirst = realtime.subscribe('models', () => {})
    stopSecond()
    stopFirst()
    expect(last().sent).toEqual([
      { type: 'subscribe', topics: ['fonts'] },
      { type: 'subscribe', topics: ['models'] },
      { type: 'unsubscribe', topics: ['models'] },
    ])
  })

  it('reconnects with growing, capped back-off and resubscribes everything', () => {
    const realtime = client({ baseMs: 100, maxMs: 400 })
    const seen: RealtimeSignal[] = []
    realtime.subscribe('models', (signal) => seen.push(signal))
    realtime.subscribe('settings', () => {})
    last().open()
    last().close()
    // random() = 1, so each delay is the full ceiling: 100, 200, 400, 400.
    for (const delay of [100, 200, 400, 400]) {
      const before = FakeSocket.instances.length
      vi.advanceTimersByTime(delay - 1)
      expect(FakeSocket.instances).toHaveLength(before)
      vi.advanceTimersByTime(1)
      expect(FakeSocket.instances).toHaveLength(before + 1)
      last().close()
    }
    vi.advanceTimersByTime(400)
    last().open()
    expect(last().sent).toEqual([{ type: 'subscribe', topics: ['models', 'settings'] }])
    last().confirm()
    expect(seen).toEqual(['resync'])
  })

  it('resets the back-off once a connection opens', () => {
    const realtime = client({ baseMs: 100, maxMs: 1000 })
    realtime.subscribe('models', () => {})
    last().close()
    vi.advanceTimersByTime(100)
    last().close()
    vi.advanceTimersByTime(200)
    last().open()
    last().close()
    const before = FakeSocket.instances.length
    vi.advanceTimersByTime(100)
    expect(FakeSocket.instances).toHaveLength(before + 1)
  })

  it('jitters each delay between half and all of its ceiling', () => {
    const realtime = client({ baseMs: 1000, random: () => 0 })
    realtime.subscribe('models', () => {})
    last().close()
    vi.advanceTimersByTime(499)
    expect(FakeSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('is unavailable when no connection opens within the fallback window, and live again once one does', () => {
    const realtime = client({ fallbackMs: 1000, baseMs: 100 })
    const changes: string[] = []
    realtime.onStatus(() => changes.push(realtime.status))
    realtime.subscribe('models', () => {})
    vi.advanceTimersByTime(999)
    expect(realtime.status).toBe('connecting')
    vi.advanceTimersByTime(1)
    expect(realtime.status).toBe('unavailable')
    last().close()
    vi.advanceTimersByTime(100)
    last().open()
    expect(realtime.status).toBe('live')
    expect(changes.filter((status, i) => status !== changes[i - 1])).toEqual([
      'connecting',
      'unavailable',
      'live',
    ])
  })

  it('tells every follower to resync when the server lost events', () => {
    const realtime = client()
    const a: RealtimeSignal[] = []
    const b: RealtimeSignal[] = []
    realtime.subscribe('models', (signal) => a.push(signal))
    realtime.subscribe('outputs', (signal) => b.push(signal))
    last().open()
    last().deliver({ type: 'resync' })
    expect(a).toEqual(['resync'])
    expect(b).toEqual(['resync'])
  })

  it('treats a silent socket as dead and reconnects', () => {
    const realtime = client({ idleMs: 1000, baseMs: 100 })
    realtime.subscribe('models', () => {})
    last().open()
    vi.advanceTimersByTime(900)
    last().deliver({ type: 'ping' })
    vi.advanceTimersByTime(900)
    expect(FakeSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(100)
    vi.advanceTimersByTime(100)
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('stops for good when closed', () => {
    const realtime = client({ baseMs: 100 })
    realtime.subscribe('models', () => {})
    last().open()
    realtime.close()
    vi.advanceTimersByTime(10_000)
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it('ignores frames that are not JSON and keeps going', () => {
    const realtime = client()
    const seen: RealtimeSignal[] = []
    realtime.subscribe('models', (signal) => seen.push(signal))
    last().open()
    last().onmessage?.({ data: '{nope' })
    last().deliver(event('model.created', ['models']))
    expect(seen).toHaveLength(1)
  })
})
