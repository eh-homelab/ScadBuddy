import { randomUUID } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { ChatConnection, type ChatConnectionOptions } from '../src/routes/chat.js'
import type { LoggedEvent } from '../src/sessions/eventLog.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { event, type ServerEvent } from '../src/sessions/protocol.js'
import { frontendClientMessages } from './support/frontendProtocol.js'

// ChatConnection's limits, against a stand-in SessionManager: a client that
// does not read (backpressure, then closing it), repeated attaches, and a
// flood of frames (the queue cap and the new-session rate limit).

const settle = (ms = 100) => new Promise((r) => setTimeout(r, ms))

type Fake = {
  manager: SessionManager
  /** Log followers running now. */
  active: () => number
  started: () => number
  /** Holds every start() until called. */
  release: () => void
}

/** A manager whose every session log holds `events` deltas, and whose start() can be held. */
function fakeManager(options: { events?: number; holdStart?: boolean } = {}): Fake {
  let active = 0
  let started = 0
  let release = () => {}
  const held = options.holdStart
    ? new Promise<void>((resolve) => {
        release = resolve
      })
    : Promise.resolve()
  async function* follow(id: string, signal?: AbortSignal): AsyncGenerator<LoggedEvent> {
    active += 1
    try {
      for (let seq = 1; seq <= (options.events ?? 0); seq++) {
        if (signal?.aborted) return
        yield {
          seq,
          event: event({ type: 'assistant.text.delta', sessionId: id, messageId: 'm', delta: 'x' }),
        } as LoggedEvent
      }
      await new Promise<void>((resolve) => {
        if (signal?.aborted) resolve()
        signal?.addEventListener('abort', () => resolve())
      })
    } finally {
      active -= 1
    }
  }
  const manager = {
    snapshot: () => Promise.resolve(event({ type: 'sessions.snapshot', sessions: [] })),
    get: (id: string) => Promise.resolve({ id }),
    start: async () => {
      started += 1
      await held
      return { session: { id: randomUUID() } }
    },
    send: () => Promise.resolve({}),
    attach: (id: string, _p: unknown, o: { signal?: AbortSignal } = {}) => Promise.resolve(follow(id, o.signal)),
    events: { lastSeq: () => Promise.resolve(0) },
  } as unknown as SessionManager
  return { manager, active: () => active, started: () => started, release: () => release() }
}

/** A socket whose unsent bytes grow by `frameBytes` per event until the test drains it. */
function slowSocket(frameBytes: number) {
  const out: ServerEvent[] = []
  let buffered = 0
  let overflowed = 0
  const options: ChatConnectionOptions = {
    buffered: () => buffered,
    overflow: () => {
      overflowed += 1
    },
    log: () => {},
  }
  return {
    out,
    options,
    send: (e: ServerEvent) => {
      out.push(e)
      buffered += frameBytes
    },
    drain: () => {
      buffered = 0
    },
    overflowed: () => overflowed,
  }
}

const deltas = (out: ServerEvent[]) => out.filter((e) => e.type === 'assistant.text.delta').length
const errors = (out: ServerEvent[], code: string) =>
  out.filter((e) => e.type === 'error' && e.code === code).length

describe('ChatConnection backpressure', () => {
  it('stops reading a session log while the socket is over its high-water mark, and resumes when it drains', async () => {
    const fake = fakeManager({ events: 1000 })
    const socket = slowSocket(60)
    const connection = new ChatConnection(fake.manager, socket.send, {
      ...socket.options,
      limits: { highWater: 100, drainStallMs: 60_000 },
    })
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(JSON.stringify(clientMessage({ type: 'session.attach', sessionId: randomUUID() })))
    await settle()
    // The snapshot and one delta put it past 100 bytes; nothing more is sent until it drains.
    expect(deltas(socket.out)).toBe(1)
    socket.drain()
    await settle()
    expect(deltas(socket.out)).toBe(3)
    expect(socket.overflowed()).toBe(0)
    connection.close()
    await settle()
    expect(fake.active()).toBe(0)
  })

  it('closes a client that never drains, and stops following', async () => {
    const fake = fakeManager({ events: 1000 })
    const socket = slowSocket(60)
    const connection = new ChatConnection(fake.manager, socket.send, {
      ...socket.options,
      limits: { highWater: 100, drainStallMs: 100 },
    })
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(JSON.stringify(clientMessage({ type: 'session.attach', sessionId: randomUUID() })))
    await settle(300)
    expect(socket.overflowed()).toBe(1)
    expect(deltas(socket.out)).toBe(1)
    expect(connection.following()).toEqual([])
    expect(fake.active()).toBe(0)
  })

  it('closes a client once its send buffer passes the cap', async () => {
    const fake = fakeManager({ events: 1000 })
    const socket = slowSocket(600)
    const connection = new ChatConnection(fake.manager, socket.send, {
      ...socket.options,
      limits: { highWater: 10_000, bufferMax: 1000 },
    })
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(JSON.stringify(clientMessage({ type: 'session.attach', sessionId: randomUUID() })))
    await settle()
    expect(socket.overflowed()).toBe(1)
    // The snapshot and one delta reach 1200 bytes; nothing is sent after that.
    expect(deltas(socket.out)).toBe(1)
    await settle()
    expect(fake.active()).toBe(0)
  })

  it('a repeated attach of one session replaces its replay rather than adding one', async () => {
    const fake = fakeManager({ events: 1000 })
    const socket = slowSocket(60)
    const connection = new ChatConnection(fake.manager, socket.send, {
      ...socket.options,
      limits: { highWater: 100, drainStallMs: 60_000 },
    })
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    const id = randomUUID()
    for (let i = 0; i < 10; i++) {
      await connection.receive(JSON.stringify(clientMessage({ type: 'session.attach', sessionId: id })))
    }
    await settle()
    expect(connection.following()).toEqual([id])
    expect(fake.active()).toBe(1)
    connection.close()
    await settle()
    expect(fake.active()).toBe(0)
  })
})

describe('ChatConnection inbound limits', () => {
  it('refuses frames past the queue cap with busy, and handles the ones it took', async () => {
    const fake = fakeManager({ holdStart: true })
    const out: ServerEvent[] = []
    const connection = new ChatConnection(fake.manager, (e) => out.push(e), {
      log: () => {},
      limits: { maxQueued: 8, maxNewSessions: 1000 },
    })
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    const frame = JSON.stringify(clientMessage({ type: 'user.message', text: 'hi', context: { route: '/' } }))
    const handled = Array.from({ length: 100 }, () => connection.receive(frame))
    await settle()
    // The first is waiting on start(); seven more wait behind it.
    expect(fake.started()).toBe(1)
    expect(errors(out, 'busy')).toBe(92)
    fake.release()
    await Promise.all(handled)
    expect(fake.started()).toBe(8)
    // The queue drained, so the connection takes frames again.
    await connection.receive(frame)
    expect(fake.started()).toBe(9)
    connection.close()
  })

  it('rate-limits new sessions per connection with rate_limited', async () => {
    const fake = fakeManager()
    const out: ServerEvent[] = []
    const connection = new ChatConnection(fake.manager, (e) => out.push(e), {
      log: () => {},
      limits: { maxNewSessions: 3, newSessionWindowMs: 200 },
    })
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    const frame = JSON.stringify(clientMessage({ type: 'user.message', text: 'hi', context: { route: '/' } }))
    for (let i = 0; i < 5; i++) await connection.receive(frame)
    expect(fake.started()).toBe(3)
    expect(errors(out, 'rate_limited')).toBe(2)
    // Messages to an existing session are not new sessions.
    await connection.receive(
      JSON.stringify(clientMessage({ type: 'user.message', sessionId: randomUUID(), text: 'hi', context: { route: '/' } })),
    )
    expect(errors(out, 'rate_limited')).toBe(2)
    await settle(250)
    await connection.receive(frame)
    expect(fake.started()).toBe(4)
    connection.close()
  })
})
