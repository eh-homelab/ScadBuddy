import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react'
import { API_BASE } from '../api/client'
import { socketUrl } from './lsp'
import { inReadScope, ReadScope } from './readCursor'

/**
 * #266 — the UI's realtime channel, `WS /api/v1/ws` (backend `api/realtime.py`).
 *
 * One socket per tab. A caller follows a *topic* (`job:<id>`, `model:<slug>`,
 * `print:<output id>`, `models`, `outputs`, `libraries`, `fonts`, `settings`) and is
 * told when something under it changed; it then re-reads through the REST call it
 * already makes. Events carry ids, never content, so a lost or repeated one costs
 * one fetch and never shows stale data.
 *
 * A listener is also signalled `'resync'` — "read now" — when the server confirms a
 * subscription (after a connect or reconnect, so nothing changed between the read
 * and the first event goes unseen), and when the server says events were lost.
 *
 * #1039 — a confirmation's `'resync'` comes with `Confirmation`: the last event the
 * server had heard by then. A listener whose read already saw it (`lib/readCursor.ts`)
 * has missed nothing; one that saw something earlier can `check` whether anything
 * since touched its topic, and read only if so.
 *
 * When the socket cannot connect within `fallbackMs`, `status` becomes
 * `'unavailable'`; callers that must stay current (#267, #268) poll while it is.
 */

export type RealtimeStatus = 'connecting' | 'live' | 'unavailable'

export interface RealtimeEvent {
  id: string
  kind: string
  topics: string[]
  data: Record<string, unknown>
}

export type RealtimeSignal = RealtimeEvent | 'resync'
/** Comes with the `'resync'` that confirms a subscription (#1039). */
export interface Confirmation {
  /**
   * The last event the server had heard once the topic was followed. Absent when it
   * does not know, or when the topic was already live and this listener only joined.
   */
  last?: string
}
export type RealtimeListener = (signal: RealtimeSignal, confirmation?: Confirmation) => void

type ServerFrame =
  | { type: 'subscribed'; topics: string[]; last?: string }
  | { type: 'changed'; id: number; topics: string[] }
  | ({ type: 'event' } & RealtimeEvent)
  | { type: 'resync' }
  | { type: 'ping' }
  | { type: 'error'; message: string }

export interface RealtimeOptions {
  url?: string
  WebSocketImpl?: typeof WebSocket
  /** How long without a connection before `status` is `'unavailable'`. */
  fallbackMs?: number
  /** Reconnect back-off: `baseMs · 2^attempt`, capped at `maxMs`, with jitter. */
  baseMs?: number
  maxMs?: number
  /** Silence for this long (the server pings every 25 s) means the socket is dead. */
  idleMs?: number
  random?: () => number
}

export const FALLBACK_MS = 5_000
const BASE_MS = 500
const MAX_MS = 30_000
const IDLE_MS = 60_000
/** How long a `check` waits for its answer before it counts every topic as changed. */
export const CHECK_MS = 3_000
/** The server's error messages for a check it would not run (backend `api/realtime.py`). */
const CHECK_REFUSALS = ['a check needs', 'expected subscribe or unsubscribe']

export class RealtimeClient {
  private readonly url: string
  private readonly WebSocketImpl: typeof WebSocket
  private readonly fallbackMs: number
  private readonly baseMs: number
  private readonly maxMs: number
  private readonly idleMs: number
  private readonly random: () => number

  private socket: WebSocket | undefined
  private readonly listeners = new Map<string, Set<RealtimeListener>>()
  /** Topics the server has confirmed on the current socket. */
  private readonly confirmed = new Set<string>()
  private readonly statusListeners = new Set<() => void>()
  private currentStatus: RealtimeStatus = 'connecting'
  private attempt = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined
  private fallbackTimer: ReturnType<typeof setTimeout> | undefined
  private idleTimer: ReturnType<typeof setTimeout> | undefined
  private closed = false
  private checks = 0
  private readonly pending = new Map<number, (changed: string[] | undefined) => void>()

  constructor(options: RealtimeOptions = {}) {
    this.url = options.url ?? socketUrl(`${API_BASE}/ws`)
    this.WebSocketImpl = options.WebSocketImpl ?? WebSocket
    this.fallbackMs = options.fallbackMs ?? FALLBACK_MS
    this.baseMs = options.baseMs ?? BASE_MS
    this.maxMs = options.maxMs ?? MAX_MS
    this.idleMs = options.idleMs ?? IDLE_MS
    this.random = options.random ?? Math.random
  }

  get status(): RealtimeStatus {
    return this.currentStatus
  }

  /**
   * #1039 — true when a `subscribe` made now is sure to be followed by a `'resync'`:
   * the socket is open, so the server's `subscribed` (or, for a topic already live,
   * the catch-up signal) comes within a round trip. A caller about to read and
   * subscribe can then leave the read to that signal rather than read twice.
   */
  get willResync(): boolean {
    return !this.closed && this.socket?.readyState === this.WebSocketImpl.OPEN
  }

  /** True while anything is followed: the "unavailable" hint only matters then. */
  get following(): boolean {
    return this.listeners.size > 0
  }

  onStatus(listener: () => void): () => void {
    this.statusListeners.add(listener)
    return () => this.statusListeners.delete(listener)
  }

  /** Follow `topic`. The returned function stops following it. */
  subscribe(topic: string, listener: RealtimeListener): () => void {
    let set = this.listeners.get(topic)
    const first = set === undefined
    if (!set) {
      set = new Set()
      this.listeners.set(topic, set)
    }
    set.add(listener)
    if (first) {
      this.send({ type: 'subscribe', topics: [topic] })
    } else if (this.confirmed.has(topic)) {
      // Already live: this listener still needs its first read, unless a read it made
      // already holds everything since (#1039: no `last`, so it asks).
      queueMicrotask(() => {
        if (set.has(listener)) listener('resync', {})
      })
    }
    this.ensureConnected()
    this.notifyStatus()
    return () => {
      const current = this.listeners.get(topic)
      if (!current?.delete(listener) || current.size > 0) return
      this.listeners.delete(topic)
      this.confirmed.delete(topic)
      this.send({ type: 'unsubscribe', topics: [topic] })
      this.notifyStatus()
    }
  }

  /**
   * #1039 — which of `topics` an event since the read that saw `since` touched. Every
   * one, when the server cannot tell, the socket is not open, or no answer comes in
   * `CHECK_MS`: the caller then reads, as it would have without asking.
   */
  check(since: string[], topics: string[]): Promise<string[]> {
    const id = ++this.checks
    if (!this.send({ type: 'check', id, since, topics })) return Promise.resolve(topics)
    return new Promise((resolve) => {
      const timer = setTimeout(() => settle(undefined), CHECK_MS)
      const settle = (changed: string[] | undefined) => {
        clearTimeout(timer)
        this.pending.delete(id)
        resolve(changed ?? topics)
      }
      this.pending.set(id, settle)
    })
  }

  close(): void {
    this.closed = true
    this.failChecks()
    clearTimeout(this.reconnectTimer)
    clearTimeout(this.fallbackTimer)
    clearTimeout(this.idleTimer)
    const socket = this.socket
    this.socket = undefined
    socket?.close()
  }

  private ensureConnected(): void {
    if (this.closed || this.socket || this.reconnectTimer !== undefined) return
    this.connect()
  }

  private connect(): void {
    this.reconnectTimer = undefined
    this.armFallback()
    let socket: WebSocket
    try {
      socket = new this.WebSocketImpl(this.url)
    } catch {
      this.scheduleReconnect()
      return
    }
    this.socket = socket
    socket.onopen = () => {
      if (this.socket !== socket) return
      this.attempt = 0
      clearTimeout(this.fallbackTimer)
      this.fallbackTimer = undefined
      this.setStatus('live')
      this.touch()
      const topics = [...this.listeners.keys()]
      if (topics.length > 0) this.send({ type: 'subscribe', topics })
    }
    socket.onmessage = (message: MessageEvent) => {
      if (this.socket !== socket) return
      this.touch()
      let frame: ServerFrame
      try {
        frame = JSON.parse(String(message.data)) as ServerFrame
      } catch {
        return
      }
      this.handle(frame)
    }
    socket.onclose = () => {
      if (this.socket !== socket) return
      this.socket = undefined
      this.confirmed.clear()
      this.failChecks()
      clearTimeout(this.idleTimer)
      this.scheduleReconnect()
    }
    // An error is always followed by a close, which is where it is handled.
    socket.onerror = () => {}
  }

  private handle(frame: ServerFrame): void {
    switch (frame.type) {
      case 'subscribed':
        for (const topic of frame.topics) {
          if (!this.listeners.has(topic)) continue
          this.confirmed.add(topic)
          this.emit(topic, 'resync', { last: typeof frame.last === 'string' ? frame.last : undefined })
        }
        return
      case 'changed':
        this.pending.get(frame.id)?.(Array.isArray(frame.topics) ? frame.topics : undefined)
        return
      case 'event':
        for (const topic of frame.topics) {
          this.emit(topic, { id: frame.id, kind: frame.kind, topics: frame.topics, data: frame.data })
        }
        return
      case 'resync':
        for (const topic of this.listeners.keys()) this.emit(topic, 'resync')
        return
      case 'ping':
        return
      case 'error':
        console.warn('realtime:', frame.message)
        // An error frame names no request. A refused check (or a server without them,
        // which refuses every frame but a subscribe) fails the checks waiting; any other
        // error (a refused topic, say) leaves them be.
        if (CHECK_REFUSALS.some((refusal) => frame.message.startsWith(refusal))) this.failChecks()
        return
    }
  }

  private emit(topic: string, signal: RealtimeSignal, confirmation?: Confirmation): void {
    for (const listener of [...(this.listeners.get(topic) ?? [])]) {
      try {
        listener(signal, confirmation)
      } catch (error) {
        console.error('a realtime listener failed', error)
      }
    }
  }

  private send(frame: object): boolean {
    if (this.socket?.readyState !== this.WebSocketImpl.OPEN) return false
    this.socket.send(JSON.stringify(frame))
    return true
  }

  private failChecks(): void {
    for (const settle of [...this.pending.values()]) settle(undefined)
  }

  private scheduleReconnect(): void {
    if (this.closed) return
    this.armFallback()
    const ceiling = Math.min(this.maxMs, this.baseMs * 2 ** this.attempt)
    this.attempt += 1
    // "Equal jitter": at least half the ceiling, so a flapping server is not hammered.
    const delay = ceiling / 2 + (ceiling / 2) * this.random()
    this.reconnectTimer = setTimeout(() => this.connect(), delay)
  }

  private armFallback(): void {
    if (this.fallbackTimer !== undefined || this.currentStatus === 'unavailable') return
    if (this.currentStatus === 'live') this.setStatus('connecting')
    this.fallbackTimer = setTimeout(() => {
      this.fallbackTimer = undefined
      if (!this.socket || this.socket.readyState !== this.WebSocketImpl.OPEN) {
        this.setStatus('unavailable')
      }
    }, this.fallbackMs)
  }

  private touch(): void {
    clearTimeout(this.idleTimer)
    this.idleTimer = setTimeout(() => {
      // No ping in twice the server's interval: the connection is gone without a close.
      this.socket?.close()
    }, this.idleMs)
  }

  private setStatus(status: RealtimeStatus): void {
    if (this.currentStatus === status) return
    this.currentStatus = status
    this.notifyStatus()
  }

  private notifyStatus(): void {
    for (const listener of [...this.statusListeners]) listener()
  }
}

/** #1039 — a read, as `readAgain` judges it: what it saw, and whether its answer is in use. */
export interface ReadRecord {
  scope: ReadScope
  shown: Promise<boolean>
}

/**
 * Runs `load` as one read, recording what it saw for `readAgain`. `applied` says whether
 * its answer is the one in use; a read whose answer was dropped vouches for nothing.
 */
export function recordRead<T>(
  load: () => Promise<T>,
  applied: (value: T) => boolean = () => true,
): { record: ReadRecord; result: Promise<T> } {
  const scope = new ReadScope()
  const result = inReadScope(scope, load)
  return { record: { scope, shown: result.then(applied, () => false) }, result }
}

/**
 * #1039 — whether a subscription's confirmation means `read` must be made again: unless
 * it saw the server's last event, or the server says nothing since touched `topic`.
 * True whenever that cannot be told: no read, a failed one, one still unsettled after
 * `CHECK_MS`, or one without cursors.
 */
export async function readAgain(
  read: ReadRecord | undefined,
  topic: string,
  confirmation: Confirmation,
): Promise<boolean> {
  if (!read || !(await settledIn(read.shown, CHECK_MS))) return true
  const since = read.scope.cursors()
  if (!since) return true
  if (since.length === 1 && since[0] === confirmation.last) return false
  return (await getRealtime().check(since, [topic])).includes(topic)
}

/** `shown`, or false once `ms` pass without it: a hung read must not hold up its re-read. */
function settledIn(shown: Promise<boolean>, ms: number): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const late = new Promise<boolean>((resolve) => {
    timer = setTimeout(() => resolve(false), ms)
  })
  return Promise.race([shown, late]).finally(() => clearTimeout(timer))
}

let shared: RealtimeClient | undefined

/** The tab's one client, created on first use. */
export function getRealtime(): RealtimeClient {
  shared ??= new RealtimeClient()
  return shared
}

/** Tests: drop the shared client so the next test starts from a fresh socket. */
export function resetRealtime(): void {
  shared?.close()
  shared = undefined
}

/** Calls `onSignal` for every change under `topic`; nothing while `topic` is undefined. */
export function useSubscription(topic: string | undefined, onSignal: RealtimeListener): void {
  const latest = useRef(onSignal)
  useEffect(() => {
    latest.current = onSignal
  })
  useEffect(() => {
    if (topic === undefined) return
    return getRealtime().subscribe(topic, (signal, confirmation) => latest.current(signal, confirmation))
  }, [topic])
}

export function useRealtimeStatus(): { status: RealtimeStatus; following: boolean } {
  const client = getRealtime()
  const subscribe = useCallback((listener: () => void) => client.onStatus(listener), [client])
  const status = useSyncExternalStore(subscribe, () => client.status)
  const following = useSyncExternalStore(subscribe, () => client.following)
  return { status, following }
}
