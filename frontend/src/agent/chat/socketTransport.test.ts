import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clientMessage } from './protocol'
import { createSocketTransport, MAX_QUEUED, REFUSED_AFTER, REFUSED_MESSAGE } from './socketTransport'

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

    expect(t.send(attach)).toBe('queued')
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

  it('caps what it holds while disconnected, and says so instead of dropping silently', () => {
    const t = createSocketTransport({ WebSocketImpl: Impl })
    t.connect({ onFrame: () => {} })
    const results = Array.from({ length: MAX_QUEUED + 10 }, () => t.send(attach))
    expect(results.filter((r) => r === 'queued')).toHaveLength(MAX_QUEUED)
    expect(results.slice(MAX_QUEUED)).toEqual(Array(10).fill('refused'))
    FakeSocket.instances[0]!.open()
    expect(FakeSocket.instances[0]!.sent).toHaveLength(MAX_QUEUED)
    expect(t.send(attach)).toBe('sent')
  })

  it('never lets a backlog crowd out an approval or an interrupt; they go first on reconnect', () => {
    const t = createSocketTransport({ WebSocketImpl: Impl })
    t.connect({ onFrame: () => {} })
    for (let i = 0; i < MAX_QUEUED; i++) t.send(attach)
    expect(t.send(attach)).toBe('refused')
    const decision = clientMessage({ type: 'approval.decision', sessionId: 's1', id: 'a1', approve: true })
    const stop = clientMessage({ type: 'session.interrupt', sessionId: 's1' })
    expect(t.send(decision)).toBe('queued')
    expect(t.send(stop)).toBe('queued')
    const ws = FakeSocket.instances[0]!
    ws.open()
    expect(ws.sent.slice(0, 2)).toEqual([JSON.stringify(decision), JSON.stringify(stop)])
    expect(ws.sent).toHaveLength(MAX_QUEUED + 2)
  })

  it('lets the re-attach go out before anything queued, so a replay is not doubled', () => {
    const t = createSocketTransport({ WebSocketImpl: Impl, baseMs: 10 })
    const reattach = clientMessage({ type: 'session.attach', sessionId: 's9' })
    let opens = 0
    t.connect({
      onFrame: () => {},
      onOpen: () => {
        opens += 1
        if (opens > 1) expect(t.send(reattach)).toBe('sent')
      },
    })
    FakeSocket.instances[0]!.open()
    FakeSocket.instances[0]!.drop()
    const message = clientMessage({ type: 'user.message', sessionId: 's9', text: 'hi', context: { route: '/' } })
    const decision = clientMessage({ type: 'approval.decision', sessionId: 's9', id: 'a1', approve: false })
    t.send(message)
    t.send(decision)
    vi.advanceTimersByTime(10)
    const ws = FakeSocket.instances[1]!
    ws.open()
    expect(ws.sent).toEqual([JSON.stringify(reattach), JSON.stringify(decision), JSON.stringify(message)])
  })

  it('says the connection is refused after repeated failed handshakes, and asks the status again', () => {
    const onRefused = vi.fn()
    const onClose = vi.fn()
    const t = createSocketTransport({ WebSocketImpl: Impl, baseMs: 10, maxMs: 10, onRefused })
    t.connect({ onFrame: () => {}, onClose })
    for (let i = 0; i < REFUSED_AFTER - 1; i++) {
      FakeSocket.instances.at(-1)!.drop()
      vi.advanceTimersByTime(10)
    }
    expect(onClose).toHaveBeenLastCalledWith('Lost the connection to the assistant; reconnecting…')
    expect(onRefused).not.toHaveBeenCalled()
    FakeSocket.instances.at(-1)!.drop()
    expect(onClose).toHaveBeenLastCalledWith(REFUSED_MESSAGE)
    expect(onRefused).toHaveBeenCalledTimes(1)
    // Still retrying; once a connection opens, the count starts over.
    vi.advanceTimersByTime(10)
    FakeSocket.instances.at(-1)!.open()
    FakeSocket.instances.at(-1)!.drop()
    expect(onClose).toHaveBeenLastCalledWith('Lost the connection to the assistant; reconnecting…')
    expect(onRefused).toHaveBeenCalledTimes(1)
    t.close()
  })
})
