import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clientMessage } from './protocol'
import { createSocketTransport, MAX_QUEUED } from './socketTransport'

/** Just enough of a browser WebSocket for the transport. */
class FakeSocket {
  static instances: FakeSocket[] = []
  readonly OPEN = 1
  readyState = 0
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  closed = false
  readonly url: string
  constructor(url: string) {
    this.url = url
    FakeSocket.instances.push(this)
  }
  send(data: string) {
    this.sent.push(data)
  }
  close() {
    this.closed = true
  }
  open() {
    this.readyState = 1
    this.onopen?.()
  }
  drop(code = 1006) {
    this.readyState = 3
    this.onclose?.({ code })
  }
}

const Impl = FakeSocket as unknown as typeof WebSocket
const attach = clientMessage({ type: 'session.attach', sessionId: 's1' })

describe('createSocketTransport', () => {
  beforeEach(() => {
    FakeSocket.instances = []
    vi.useFakeTimers()
  })
  afterEach(() => vi.useRealTimers())

  it('connects to the agent socket on the page origin, queues until open, and hands frames on', () => {
    const t = createSocketTransport({ WebSocketImpl: Impl })
    const frames: unknown[] = []
    const onOpen = vi.fn()
    t.connect({ onFrame: (f) => frames.push(f), onOpen })
    const ws = FakeSocket.instances[0]!
    expect(ws.url).toBe(`ws://${window.location.host}/api/v1/ai/chat`)

    t.send(attach)
    expect(ws.sent).toEqual([])
    ws.open()
    expect(ws.sent).toEqual([JSON.stringify(attach)])
    expect(onOpen).toHaveBeenCalledTimes(1)

    ws.onmessage?.({ data: '{"v":1,"type":"sessions.snapshot","sessions":[]}' })
    ws.onmessage?.({ data: new ArrayBuffer(1) })
    expect(frames).toEqual(['{"v":1,"type":"sessions.snapshot","sessions":[]}'])
  })

  it('reconnects with back-off after a drop, saying so, and not after close()', () => {
    const t = createSocketTransport({ WebSocketImpl: Impl, baseMs: 100, maxMs: 1000 })
    const onClose = vi.fn()
    const onOpen = vi.fn()
    t.connect({ onFrame: () => {}, onClose, onOpen })
    FakeSocket.instances[0]!.open()
    FakeSocket.instances[0]!.drop(1001)
    expect(onClose).toHaveBeenLastCalledWith('The assistant service is restarting; reconnecting…')
    // Sent while down: delivered on the next connection.
    t.send(attach)
    vi.advanceTimersByTime(99)
    expect(FakeSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    const second = FakeSocket.instances[1]!
    second.drop()
    expect(onClose).toHaveBeenLastCalledWith('Lost the connection to the assistant; reconnecting…')
    vi.advanceTimersByTime(200)
    const third = FakeSocket.instances[2]!
    third.open()
    expect(third.sent).toEqual([JSON.stringify(attach)])
    expect(onOpen).toHaveBeenCalledTimes(2)

    t.close()
    expect(third.closed).toBe(true)
    third.drop()
    vi.advanceTimersByTime(10_000)
    expect(FakeSocket.instances).toHaveLength(3)
  })

  it('caps what it holds while disconnected', () => {
    const t = createSocketTransport({ WebSocketImpl: Impl })
    t.connect({ onFrame: () => {} })
    for (let i = 0; i < MAX_QUEUED + 10; i++) t.send(attach)
    FakeSocket.instances[0]!.open()
    expect(FakeSocket.instances[0]!.sent).toHaveLength(MAX_QUEUED)
  })
})
