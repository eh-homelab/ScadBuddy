import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { BusEvent } from '../src/events/bus.js'
import { CHANGES_PATH, ChangeStream } from '../src/events/changeStream.js'
import { followSessionEvents } from '../src/sessions/busEvents.js'
import { EventLog, type EventRows, type LoggedEvent } from '../src/sessions/eventLog.js'
import type { ServerEvent } from '../src/sessions/protocol.js'

// The agent's client of the API's change stream (#5299 D4, step 11; events/changeStream.ts)
// against a fake API on a real socket: the six channels delivered, a reconnect with
// `Last-Event-ID` and the API's replay, a resync, a refused or silent stream retried, and a
// stream that is down costing only latency, the event log's followers still polling.

type Open = { res: ServerResponse; lastEventId: string | undefined; credential: string | undefined }

/** The API's route: each GET is held open as a stream the test writes frames into. */
class FakeApi {
  readonly server: Server
  readonly opens: Open[] = []
  /** What the next GETs answer before any stream: a status with no CRUD header, or a refusal. */
  refuse: { status: number; crud: boolean }[] = []
  #waiters: (() => void)[] = []

  constructor() {
    this.server = createServer((req: IncomingMessage, res: ServerResponse) => {
      if (req.url !== CHANGES_PATH) {
        res.writeHead(404).end()
        return
      }
      const refusal = this.refuse.shift()
      if (refusal) {
        res.writeHead(refusal.status, refusal.crud ? { 'x-scadbuddy-crud': '1' } : {}).end()
        return
      }
      res.writeHead(200, { 'content-type': 'text/event-stream', 'x-scadbuddy-crud': '1', 'cache-control': 'no-store' })
      res.flushHeaders()
      const header = req.headers['last-event-id']
      this.opens.push({ res, lastEventId: typeof header === 'string' ? header : undefined, credential: req.headers.authorization })
      for (const wake of this.#waiters.splice(0)) wake()
    })
  }

  async listen(): Promise<string> {
    await new Promise<void>((resolve) => this.server.listen(0, '127.0.0.1', resolve))
    return `http://127.0.0.1:${(this.server.address() as AddressInfo).port}`
  }

  /** The `n`th stream opened (1-based), once it is. */
  async open(n: number): Promise<Open> {
    while (this.opens.length < n) await new Promise<void>((resolve) => this.#waiters.push(resolve))
    return this.opens[n - 1]!
  }

  async close(): Promise<void> {
    for (const { res } of this.opens) res.destroy()
    this.server.closeAllConnections()
    await new Promise((resolve) => this.server.close(resolve))
  }
}

function send(res: ServerResponse, event: string, data: unknown, id?: string): void {
  res.write(`${id !== undefined ? `id: ${id}\n` : ''}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
}

function busEvent(fields: Partial<BusEvent> & { kind: string }): BusEvent {
  return { id: crypto.randomUUID().replaceAll('-', ''), at: new Date().toISOString(), ...fields }
}

function change(res: ServerResponse, channel: string, payload: string, id?: string): void {
  send(res, 'change', { c: channel, p: payload }, id)
}

async function until(check: () => boolean, what: string, timeoutMs = 5000): Promise<void> {
  const deadline = performance.now() + timeoutMs
  while (!check()) {
    if (performance.now() > deadline) throw new Error(`timed out waiting for ${what}`)
    await new Promise((r) => setTimeout(r, 5))
  }
}

describe('the change stream client', () => {
  let api: FakeApi
  let base: string
  let stream: ChangeStream | undefined
  const logs: string[] = []

  beforeEach(async () => {
    api = new FakeApi()
    base = await api.listen()
    logs.length = 0
  })
  afterEach(async () => {
    await stream?.close()
    stream = undefined
    await api.close()
  })

  function open(options: { idleMs?: number } = {}): {
    stream: ChangeStream
    heard: BusEvent[]
    resyncs: () => number
    reconnects: () => number
  } {
    const credentialFetch: typeof fetch = (input, init) => fetch(input, { ...init, headers: { ...(init?.headers as Record<string, string>), authorization: 'Bearer sbsvc_test' } })
    stream = new ChangeStream(base, { fetch: credentialFetch, retryMinMs: 10, retryMaxMs: 40, log: (m) => logs.push(m), ...options })
    const heard: BusEvent[] = []
    let resyncs = 0
    let reconnects = 0
    stream.follow({ onEvent: (e) => heard.push(e), onResync: () => resyncs++, onReconnect: () => reconnects++ })
    stream.start()
    return { stream, heard, resyncs: () => resyncs, reconnects: () => reconnects }
  }

  it('delivers every channel, the bus to its followers and the others to their listeners', async () => {
    const { stream, heard } = open()
    const channels = new Map<string, string[]>()
    for (const c of ['scadbuddy_input', 'scadbuddy_pending', 'scadbuddy_mcp', 'scadbuddy_warm_evict', 'scadbuddy_settings']) {
      channels.set(c, [])
      stream.listenAlso(c, (p) => channels.get(c)!.push(p))
    }
    const first = await api.open(1)
    // Opened with the service credential, and no place on its first open.
    expect(first.credential).toBe('Bearer sbsvc_test')
    expect(first.lastEventId).toBeUndefined()
    send(first.res, 'ready', { resync: false, reconnect: false }, '0')
    await stream.ready()
    const job = busEvent({ kind: 'job.done', job_id: 'j1' })
    change(first.res, 'scadbuddy_events', JSON.stringify(job), job.id)
    // A session.* event is NOTIFY-only: no id.
    const session = busEvent({ kind: 'session.status', session_id: 's1' })
    change(first.res, 'scadbuddy_events', JSON.stringify(session))
    for (const c of channels.keys()) change(first.res, c, `${c} with a\nnewline`)
    // The same event again (its live copy after a replay) is delivered once.
    change(first.res, 'scadbuddy_events', JSON.stringify(job), job.id)
    await until(() => [...channels.values()].every((p) => p.length === 1), 'every channel')
    expect(heard.map((e) => e.id)).toEqual([job.id, session.id])
    for (const [c, payloads] of channels) expect(payloads).toEqual([`${c} with a\nnewline`])
  })

  it('reconnects with its Last-Event-ID, takes the replay, then tells followers to re-read', async () => {
    const { stream, heard, resyncs, reconnects } = open()
    const pending: string[] = []
    let reread = 0
    stream.listenAlso('scadbuddy_pending', (p) => pending.push(p))
    stream.onReconnect(() => reread++)
    const first = await api.open(1)
    send(first.res, 'ready', { resync: false, reconnect: false }, 'head0')
    const a = busEvent({ kind: 'job.done', job_id: 'a' })
    change(first.res, 'scadbuddy_events', JSON.stringify(a), a.id)
    // Not a logged event: the place stays at a.
    change(first.res, 'scadbuddy_pending', '')
    await until(() => heard.length === 1 && pending.length === 1, 'the first stream')
    expect(reconnects()).toBe(0)
    first.res.destroy()

    const second = await api.open(2)
    expect(second.lastEventId).toBe(a.id)
    expect(stream.listening).toBe(false)
    const b = busEvent({ kind: 'job.done', job_id: 'b' })
    change(second.res, 'scadbuddy_events', JSON.stringify(b), b.id)
    send(second.res, 'ready', { resync: false, reconnect: true }, b.id)
    await until(() => reread === 1, 'the reconnect callbacks')
    expect(heard.map((e) => e.id)).toEqual([a.id, b.id])
    expect(reconnects()).toBe(1)
    expect(resyncs()).toBe(0)
    expect(stream.listening).toBe(true)

    // The API could not replay (its place pruned): followers resync too.
    second.res.destroy()
    const third = await api.open(3)
    expect(third.lastEventId).toBe(b.id)
    send(third.res, 'ready', { resync: true, reconnect: true }, 'newest')
    await until(() => resyncs() === 1, 'the resync')
    expect(reconnects()).toBe(2)

    // A gap on the API's side (its LISTEN dropped, or this reader fell behind) says the same.
    send(third.res, 'reconnected', { resync: false }, 'newest')
    await until(() => reread === 3, 'the gap re-read')
    expect(resyncs()).toBe(1)
  })

  it('runs the reconnect re-reads with no resync when the API sends reconnected {resync: false} mid-stream', async () => {
    const { stream, heard, resyncs, reconnects } = open()
    let reread = 0
    stream.onReconnect(() => reread++)
    const first = await api.open(1)
    send(first.res, 'ready', { resync: false, reconnect: false }, 'p1')
    await stream.ready()
    // The API's own gap (its LISTEN dropped, or this reader fell behind): it replayed what was
    // logged, then says so. The stream itself never dropped.
    const replayed = busEvent({ kind: 'job.done', job_id: 'r' })
    change(first.res, 'scadbuddy_events', JSON.stringify(replayed), replayed.id)
    send(first.res, 'reconnected', { resync: false }, replayed.id)
    await until(() => reread === 1, 'the reconnect callback')
    expect(reconnects()).toBe(1)
    expect(resyncs()).toBe(0)
    expect(stream.resyncs).toBe(0)
    expect(heard.map((e) => e.id)).toEqual([replayed.id])
    // The same stream: no new open.
    expect(stream.connects).toBe(1)
    expect(api.opens).toHaveLength(1)
    expect(logs.some((m) => m.includes('resyncing'))).toBe(false)
  })

  it("keeps its place on a logged change or the API's place: a NOTIFY-only event or another frame never moves it", async () => {
    const { heard } = open()
    const first = await api.open(1)
    send(first.res, 'ready', { resync: false, reconnect: false }, 'p1')
    const logged = busEvent({ kind: 'job.done', job_id: 'a' })
    change(first.res, 'scadbuddy_events', JSON.stringify(logged), logged.id)
    // The agent's own session.* bus event: NOTIFY only, never in the log, so the API sends no id.
    const unlogged = busEvent({ kind: 'session.waiting', session_id: 's1', seq: 3 })
    change(first.res, 'scadbuddy_events', JSON.stringify(unlogged))
    // An id on a frame that is not a change, `ready` or `reconnected` names no place.
    send(first.res, 'unknown', {}, 'nor-this')
    await until(() => heard.length === 2, 'both bus events')
    first.res.destroy()
    const second = await api.open(2)
    expect(second.lastEventId).toBe(logged.id)
    // A gap the API resynced past moves its place to the newest event: so does the agent's.
    send(second.res, 'ready', { resync: false, reconnect: true }, logged.id)
    send(second.res, 'reconnected', { resync: true }, 'newest')
    await until(() => logs.some((m) => m.includes('resyncing')), 'the resync')
    second.res.destroy()
    const third = await api.open(3)
    expect(third.lastEventId).toBe('newest')
  })

  it('retries a refused or missing route with back-off, and never takes a non-CRUD answer', async () => {
    api.refuse = [
      { status: 404, crud: false },
      { status: 429, crud: true },
      { status: 503, crud: true },
    ]
    const { stream } = open()
    const opened = await api.open(1)
    send(opened.res, 'ready', { resync: false, reconnect: false }, '0')
    await stream.ready()
    expect(logs.filter((m) => m.includes('retrying'))).toHaveLength(3)
    expect(logs[0]).toContain('no CRUD route answered (HTTP 404)')
    expect(logs[1]).toContain('the backend refused it (HTTP 429)')
  })

  it('opens the stream again when it goes silent (a half-open connection)', async () => {
    const { stream } = open({ idleMs: 100 })
    const first = await api.open(1)
    send(first.res, 'ready', { resync: false, reconnect: false }, 'p1')
    await stream.ready()
    // No keepalive: the client gives up on it and reconnects from its place.
    const second = await api.open(2)
    expect(second.lastEventId).toBe('p1')
    expect(logs.some((m) => m.includes('went silent'))).toBe(true)
  })

  it('ignores a malformed frame and keeps the stream', async () => {
    const { stream, heard } = open()
    const first = await api.open(1)
    send(first.res, 'ready', { resync: false, reconnect: false }, '0')
    first.res.write('event: change\ndata: {not json\n\n')
    change(first.res, 'scadbuddy_events', 'not an event')
    const e = busEvent({ kind: 'job.done', job_id: 'x' })
    change(first.res, 'scadbuddy_events', JSON.stringify(e), e.id)
    await until(() => heard.length === 1, 'the good frame')
    expect(stream.connects).toBe(1)
    expect(logs.some((m) => m.includes('malformed change frame'))).toBe(true)
  })
})

/** An event log kept in memory: what another replica appends here wakes nobody on this one. */
class MemoryRows implements Partial<EventRows> {
  readonly rows = new Map<string, LoggedEvent[]>()

  async read(sessionId: string, afterSeq: number, limit: number): Promise<LoggedEvent[]> {
    return (this.rows.get(sessionId) ?? []).filter((r) => r.seq > afterSeq).slice(0, limit)
  }

  /** Another replica's append: in the log, with no local wake. */
  appendElsewhere(sessionId: string, event: ServerEvent): void {
    const log = this.rows.get(sessionId) ?? []
    log.push({ seq: log.length + 1, event })
    this.rows.set(sessionId, log)
  }
}

describe('a dropped change stream costs latency only (#5299 D4)', () => {
  const SESSION = '0b0e3c55-6f1e-4a39-9d0e-8a1c2b3d4e5f'
  const text = (s: string): ServerEvent => ({ type: 'assistant.text.delta', session_id: SESSION, text: s }) as unknown as ServerEvent

  async function followFirst(log: EventLog, signal: AbortSignal, after = 0): Promise<{ seq: number; at: number }> {
    for await (const row of log.follow(SESSION, after, signal)) return { seq: row.seq, at: performance.now() }
    throw new Error('the follow ended')
  }

  it('wakes a follower at once while the stream is up, and polls while it is down', async () => {
    const api = new FakeApi()
    const base = await api.listen()
    const rows = new MemoryRows()
    // A poll long enough that a delivery within it can only be the stream's wake.
    const log = new EventLog(rows as unknown as EventRows, { pollMs: 60_000 })
    const stream = new ChangeStream(base, { retryMinMs: 10, retryMaxMs: 40, log: () => {} })
    const stop = followSessionEvents(stream, log, 'this-replica')
    stream.start()
    const opened = await api.open(1)
    send(opened.res, 'ready', { resync: false, reconnect: false }, '0')
    await stream.ready()
    const controller = new AbortController()
    try {
      const first = followFirst(log, controller.signal)
      await new Promise((r) => setTimeout(r, 50))
      rows.appendElsewhere(SESSION, text('a'))
      const announced = busEvent({ kind: 'session.message', session_id: SESSION, seq: 1, replica: 'another-replica' })
      change(opened.res, 'scadbuddy_events', JSON.stringify(announced))
      const started = performance.now()
      expect((await first).seq).toBe(1)
      expect(performance.now() - started).toBeLessThan(5_000)
    } finally {
      controller.abort()
      stop()
      await stream.close()
      await api.close()
    }

    // The stream is down for good (no API at all): the follower still sees the next event, by its 1 s poll.
    const polled = new EventLog(rows as unknown as EventRows, { pollMs: 1000 })
    const down = new ChangeStream('http://127.0.0.1:9', { retryMinMs: 10, retryMaxMs: 40, log: () => {} })
    const stopDown = followSessionEvents(down, polled, 'this-replica')
    down.start()
    const second = new AbortController()
    try {
      const next = followFirst(polled, second.signal, 1)
      await new Promise((r) => setTimeout(r, 50))
      const appended = performance.now()
      rows.appendElsewhere(SESSION, text('b'))
      const got = await next
      expect(got.seq).toBe(2)
      expect(got.at - appended).toBeLessThan(2_500)
      expect(down.listening).toBe(false)
    } finally {
      second.abort()
      stopDown()
      await down.close()
    }
  })
})
