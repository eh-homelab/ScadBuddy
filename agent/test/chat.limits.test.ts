import { randomUUID } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { ChatConnection, type ChatConnectionOptions } from '../src/routes/chat.js'
import type { LoggedEvent } from '../src/sessions/eventLog.js'
import { SessionError, type SessionManager } from '../src/sessions/manager.js'
import { event, type ServerEvent } from '../src/sessions/protocol.js'
import { frontendClientMessages } from './support/frontendProtocol.js'

// ChatConnection's limits, against a stand-in SessionManager: a client that
// does not read (backpressure, then closing it), repeated attaches, and a
// flood of frames (the queue cap), and the manager's new-session limit.

const settle = (ms = 100) => new Promise((r) => setTimeout(r, ms))

type Fake = {
  manager: SessionManager
  /** Log followers running now. */
  active: () => number
  started: () => number
  /** snapshot() calls so far. */
  snapshots: () => number
  /** Holds every start() (holdStart), and every snapshot() after the first (holdSnapshots), until called. */
  release: () => void
}

/**
 * A manager whose every session log holds `events` deltas, and whose start(), or
 * every session-list read after the first, can be held.
 */
function fakeManager(options: { events?: number; holdStart?: boolean; holdSnapshots?: boolean } = {}): Fake {
  let active = 0
  let started = 0
  let snapshots = 0
  let release = () => {}
  const held =
    options.holdStart || options.holdSnapshots
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
    snapshot: async () => {
      snapshots += 1
      if (options.holdSnapshots && snapshots > 1) await held
      return event({ type: 'sessions.snapshot', sessions: [] })
    },
    get: (id: string) => Promise.resolve({ id }),
    start: async () => {
      started += 1
      if (options.holdStart) await held
      return { session: { id: randomUUID() } }
    },
    send: () => Promise.resolve({}),
    attach: (id: string, _p: unknown, o: { signal?: AbortSignal } = {}) => Promise.resolve(follow(id, o.signal)),
    events: { lastSeq: () => Promise.resolve(0) },
  } as unknown as SessionManager
  return { manager, active: () => active, started: () => started, snapshots: () => snapshots, release: () => release() }
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
      limits: { maxQueued: 8 },
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

  it('refuses a flood of malformed frames past the queue cap with busy, like valid ones', async () => {
    const fake = fakeManager()
    const out: ServerEvent[] = []
    const connection = new ChatConnection(fake.manager, (e) => out.push(e), {
      log: () => {},
      limits: { maxQueued: 8 },
    })
    await connection.open()
    // None of these parse: not JSON, the wrong shape, and an empty message.
    const malformed = ['', 'not json', JSON.stringify({ v: 1, type: 'nope' }), JSON.stringify({ v: 1, type: 'user.message', text: '', context: { route: '/' } })]
    const handled = Array.from({ length: 100 }, (_, i) => connection.receive(malformed[i % malformed.length]!))
    // Eight are answered from the queue; the rest are refused without being parsed.
    expect(errors(out, 'busy')).toBe(92)
    expect(errors(out, 'invalid')).toBe(0)
    await Promise.all(handled)
    expect(errors(out, 'invalid')).toBe(8)
    expect(errors(out, 'busy')).toBe(92)
    // The queue drained, so a malformed frame is answered as such again.
    await connection.receive('still not json')
    expect(errors(out, 'invalid')).toBe(9)
    expect(fake.started()).toBe(0)
    connection.close()
  })

  it('re-reads the session list one at a time, outside the frame cap, when the database is slow', async () => {
    const fake = fakeManager({ holdSnapshots: true })
    const out: ServerEvent[] = []
    const connection = new ChatConnection(fake.manager, (e) => out.push(e), {
      log: () => {},
      limits: { maxQueued: 2 },
      snapshotMs: 5,
    })
    await connection.open()
    await settle(100)
    // The first re-read is still waiting on the database; the ticks since did not add more.
    expect(fake.snapshots()).toBe(2)
    // It holds the queue, but takes no frame slot: two frames still queue, the third is busy.
    const { clientMessage } = await frontendClientMessages()
    const frame = JSON.stringify(clientMessage({ type: 'user.message', text: 'hi', context: { route: '/' } }))
    const handled = [connection.receive(frame), connection.receive(frame), connection.receive(frame)]
    expect(errors(out, 'busy')).toBe(1)
    expect(fake.started()).toBe(0)
    fake.release()
    await Promise.all(handled)
    expect(fake.started()).toBe(2)
    // Once it lands, the next tick reads again.
    await settle(50)
    expect(fake.snapshots()).toBeGreaterThan(2)
    connection.close()
  })

  it("passes the manager's new-session refusal (per owner, not per connection) on as rate_limited", async () => {
    const fake = fakeManager()
    const refusing = {
      ...fake.manager,
      start: () => Promise.reject(new SessionError('rate_limited', 'too many new sessions')),
    } as unknown as SessionManager
    const out: ServerEvent[] = []
    const connection = new ChatConnection(refusing, (e) => out.push(e), { log: () => {} })
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(JSON.stringify(clientMessage({ type: 'user.message', text: 'hi', context: { route: '/' } })))
    expect(out.at(-1)).toMatchObject({ type: 'error', code: 'rate_limited', message: 'too many new sessions' })
    connection.close()
  })
})
