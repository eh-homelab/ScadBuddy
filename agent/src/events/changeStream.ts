import { setTimeout as sleep } from 'node:timers/promises'
import { transportKind } from '../api/backendUp.js'
import { AGENT_CRUD } from '../api/crudRead.js'
import { type BusEvent, decodeEvent, type EventListener, type EventSource, Followers, PG_CHANNEL } from './bus.js'

// The agent's change stream (#5299 D4, step 11): what the agent heard on a LISTEN connection
// of its own until then, now one server-sent event stream from the CRUD service,
// `GET …/crud/agent/changes` (backend api/crud_agent_changes.py, agent_changes/hub.py), opened
// with the agent's service credential. It carries the six channels: the event bus
// (`scadbuddy_events`, the backend's events and the agent's own `session.*`), and
// `scadbuddy_input`, `_pending`, `_mcp`, `_warm_evict` and `_settings`, in the order Postgres
// delivered them to the API's one LISTEN connection. This process opens no LISTEN connection.
//
// THE GAP. A dropped stream is opened again with back-off (500 ms doubling to 30 s, with
// jitter), sending the last logged event's id as `Last-Event-ID`. The API replays the backend's
// `events` log after it before its `ready` frame, so a backend event sent meanwhile still
// arrives, and says `resync` when it cannot (the place pruned or too far behind): followers
// then re-read what they follow. What has no row in the log (the other channels, the agent's
// own `session.*`) is gone, so after every reconnect followers' `onReconnect` and this class's
// `onReconnect` callbacks run, and re-read. The API sends `reconnected` with the same meaning
// when the gap was on its side (its LISTEN connection dropped, or this reader fell behind).
//
// D4: the 1 s polls stay (sessions/eventLog.ts followers, sessions/turnHost.ts held calls), so a
// stream that is down costs latency only. A stream silent past `idleMs` (the API sends a
// keepalive every 15 s) is taken for a half-open connection and opened again.

export type ChangeStreamOptions = {
  /** The fetch that carries the agent's service credential (api/serviceCredential.ts). */
  fetch?: typeof fetch
  /** Reconnect back-off bounds (default 500 ms → 30 s). */
  retryMinMs?: number
  retryMaxMs?: number
  /** A stream silent this long is opened again (default 45 s: three keepalives). */
  idleMs?: number
  /** Event ids remembered for de-duplication (default 5000). */
  seenMax?: number
  log?: (message: string) => void
}

/** The longest line read before the stream is dropped as broken. */
const LINE_MAX = 1024 * 1024

/** The route, under the agent's CRUD prefix. */
export const CHANGES_PATH = `${AGENT_CRUD}/changes`

type Frame = { id?: string; event: string; data: string }

/** The frames whose `id` is the stream's place. */
const PLACED: ReadonlySet<string> = new Set(['change', 'ready', 'reconnected'])

export class ChangeStream implements EventSource {
  readonly #url: string
  readonly #fetch: typeof fetch
  readonly #followers = new Followers()
  readonly #seen = new Set<string>()
  readonly #channels = new Map<string, (payload: string) => void>()
  readonly #reconnects: (() => void)[] = []
  readonly #retryMin: number
  readonly #retryMax: number
  readonly #idleMs: number
  readonly #seenMax: number
  readonly #log: (message: string) => void
  #controller: AbortController | undefined
  #running: Promise<void> | undefined
  #closed = false
  #listening = false
  #everListened = false
  /** The stream's place: the last logged event's id it sent, or its `ready`'s. */
  #lastId: string | undefined
  #waiters: (() => void)[] = []
  /** Streams opened (tests, metrics). */
  connects = 0
  /** Resyncs the API asked for (tests, metrics). */
  resyncs = 0

  constructor(backendUrl: string, options: ChangeStreamOptions = {}) {
    this.#url = `${backendUrl.replace(/\/+$/, '')}${CHANGES_PATH}`
    this.#fetch = options.fetch ?? globalThis.fetch
    this.#retryMin = options.retryMinMs ?? 500
    this.#retryMax = options.retryMaxMs ?? 30_000
    this.#idleMs = options.idleMs ?? 45_000
    this.#seenMax = options.seenMax ?? 5000
    this.#log = options.log ?? ((m) => console.error(m))
  }

  /** Opens the stream, and again whenever it drops, until `close`. Never throws. */
  start(): void {
    if (this.#running || this.#closed) return
    this.#running = this.#loop()
  }

  get listening(): boolean {
    return this.#listening
  }

  follow(listener: EventListener): () => void {
    return this.#followers.follow(listener)
  }

  /**
   * Hears `channel`'s payloads (mcp/sessionRelay.ts, sessions/runtimeSync.ts, sessions/turnHost.ts,
   * gate/changes.ts). Nothing on it is replayed: a payload sent while the stream is down is lost,
   * so its user must not depend on every one. One listener per channel; a second replaces it.
   */
  listenAlso(channel: string, onNotify: (payload: string) => void): void {
    this.#channels.set(channel, onNotify)
  }

  /** Calls `fn` each time the stream is back after a drop: what `listenAlso` lost meanwhile is re-read there. */
  onReconnect(fn: () => void): void {
    this.#reconnects.push(fn)
  }

  async close(): Promise<void> {
    this.#closed = true
    this.#controller?.abort()
    await this.#running
  }

  /** Waits until the stream is open (tests). */
  async ready(timeoutMs = 10_000): Promise<void> {
    if (this.#listening) return
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('the change stream did not open in time')), timeoutMs)
      this.#waiters.push(() => {
        clearTimeout(timer)
        resolve()
      })
    })
  }

  /** Ends the open stream as a network drop would (tests). */
  dropConnectionForTest(): void {
    this.#controller?.abort()
  }

  async #loop(): Promise<void> {
    let delay = this.#retryMin
    while (!this.#closed) {
      const opened = await this.#once()
      if (this.#closed) return
      if (opened) delay = this.#retryMin
      // Jittered, so replicas do not all reconnect in step after an API restart.
      await sleep(Math.round(delay * (0.5 + Math.random() / 2)))
      delay = Math.min(delay * 2, this.#retryMax)
    }
  }

  /** One stream, until it ends; whether it opened (got `ready`). */
  async #once(): Promise<boolean> {
    const controller = new AbortController()
    this.#controller = controller
    let idle: NodeJS.Timeout | undefined
    const touch = () => {
      clearTimeout(idle)
      idle = setTimeout(() => controller.abort(new Error('the change stream went silent')), this.#idleMs)
      idle.unref()
    }
    let opened = false
    try {
      touch()
      const response = await this.#fetch(this.#url, {
        headers: { accept: 'text/event-stream', ...(this.#lastId !== undefined ? { 'last-event-id': this.#lastId } : {}) },
        signal: controller.signal,
      })
      const crud = response.headers.get('x-scadbuddy-crud') === '1'
      const sse = (response.headers.get('content-type') ?? '').startsWith('text/event-stream')
      if (!response.ok || !crud || !sse || !response.body) {
        await response.body?.cancel().catch(() => {})
        this.#log(`change stream: ${crud ? 'the backend refused it' : 'no CRUD route answered'} (HTTP ${response.status}); retrying`)
        return false
      }
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
      let buffer = ''
      let frame: Partial<Frame> & { data?: string } = {}
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        touch()
        buffer += value
        let newline: number
        while ((newline = buffer.indexOf('\n')) >= 0) {
          // The API ends lines with \n alone; a \r before it is dropped.
          const line = buffer.slice(0, newline).replace(/\r$/, '')
          buffer = buffer.slice(newline + 1)
          if (line === '') {
            if (frame.data !== undefined) opened = this.#dispatch({ event: frame.event ?? 'message', data: frame.data, ...(frame.id !== undefined ? { id: frame.id } : {}) }) || opened
            frame = {}
            continue
          }
          if (line.startsWith(':')) continue
          const colon = line.indexOf(':')
          const name = colon < 0 ? line : line.slice(0, colon)
          const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '')
          if (name === 'data') frame.data = frame.data === undefined ? value : `${frame.data}\n${value}`
          else if (name === 'event') frame.event = value
          else if (name === 'id') frame.id = value
        }
        // A frame is a NOTIFY payload (under 8000 bytes) in JSON: a line past this is no frame.
        if (buffer.length > LINE_MAX) throw new Error(`a change stream line passed ${LINE_MAX} characters`)
      }
      if (!this.#closed) this.#log('change stream: ended by the backend; reconnecting')
    } catch (err) {
      if (!this.#closed) {
        const why = controller.signal.aborted ? String((controller.signal.reason as Error | undefined)?.message ?? 'aborted') : transportKind(err)
        this.#log(`change stream: dropped (${why}); reconnecting`)
      }
    } finally {
      clearTimeout(idle)
      this.#listening = false
    }
    return opened
  }

  /** Handles one frame; true for the stream's `ready`. */
  #dispatch(frame: Frame): boolean {
    let data: unknown
    try {
      data = JSON.parse(frame.data)
    } catch {
      this.#log(`change stream: ignored a malformed ${frame.event} frame`)
      return false
    }
    // Only a logged change or the stream's place (`ready`, and `reconnected` after a gap, whose
    // resync moves the API's place to the newest event) moves it: another frame's id names nothing.
    if (frame.id !== undefined && PLACED.has(frame.event)) this.#lastId = frame.id
    switch (frame.event) {
      case 'ready': {
        const resync = (data as { resync?: unknown }).resync === true
        this.connects += 1
        this.#listening = true
        const reconnected = this.#everListened
        this.#everListened = true
        for (const wake of this.#waiters.splice(0)) wake()
        if (reconnected) this.#reconnected(resync)
        return true
      }
      case 'reconnected':
        this.#reconnected((data as { resync?: unknown }).resync === true)
        return false
      case 'change': {
        const { c, p } = data as { c?: unknown; p?: unknown }
        if (typeof c !== 'string' || typeof p !== 'string') return false
        if (c === PG_CHANNEL) {
          const event = decodeEvent(p)
          if (event) this.#deliver(event)
        } else this.#channels.get(c)?.(p)
        return false
      }
      default:
        return false
    }
  }

  /** After a gap: a resync when the API could not replay it, then the NOTIFY-only re-reads. */
  #reconnected(resync: boolean): void {
    if (resync) {
      this.resyncs += 1
      this.#log('change stream: resyncing followers: the event log could not be replayed')
      this.#followers.resync()
    }
    this.#followers.reconnected()
    for (const fn of this.#reconnects) {
      try {
        fn()
      } catch (err) {
        this.#log(`change stream: a reconnect callback failed (${(err as Error).message})`)
      }
    }
  }

  #deliver(event: BusEvent): void {
    if (this.#seen.has(event.id)) return
    this.#seen.add(event.id)
    if (this.#seen.size > this.#seenMax) {
      const oldest = this.#seen.values().next().value
      if (oldest !== undefined) this.#seen.delete(oldest)
    }
    this.#followers.event(event)
  }
}
