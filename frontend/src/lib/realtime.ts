import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react'
import { API_BASE } from '../api/client'
import { socketUrl } from './lsp'

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
export type RealtimeListener = (signal: RealtimeSignal) => void

type ServerFrame =
  | { type: 'subscribed'; topics: string[] }
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
      // Already live: this listener still needs its first read.
      queueMicrotask(() => {
        if (set.has(listener)) listener('resync')
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

  close(): void {
    this.closed = true
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
          this.emit(topic, 'resync')
        }
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
        return
    }
  }

  private emit(topic: string, signal: RealtimeSignal): void {
    for (const listener of [...(this.listeners.get(topic) ?? [])]) {
      try {
        listener(signal)
      } catch (error) {
        console.error('a realtime listener failed', error)
      }
    }
  }

  private send(frame: object): void {
    if (this.socket?.readyState === this.WebSocketImpl.OPEN) {
      this.socket.send(JSON.stringify(frame))
    }
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
    return getRealtime().subscribe(topic, (signal) => latest.current(signal))
  }, [topic])
}

export function useRealtimeStatus(): { status: RealtimeStatus; following: boolean } {
  const client = getRealtime()
  const subscribe = useCallback((listener: () => void) => client.onStatus(listener), [client])
  const status = useSyncExternalStore(subscribe, () => client.status)
  const following = useSyncExternalStore(subscribe, () => client.following)
  return { status, following }
}
