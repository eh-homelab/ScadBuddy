import type { BusEvent, EventSource } from '../events/bus.js'
import { affectedBy, NOTIFY_ONLY_PREFIXES } from './events.js'

// From the event bus to each MCP session's subscriptions (issue #264).
//
// One hub per process follows the bus (src/events/pgListener.ts) and hands
// every event to each open `/mcp` session's `Subscriptions`. A session sends
// `notifications/resources/updated { uri }` for the URIs it subscribed to and
// `notifications/resources/list_changed` when the listed set changes
// (https://modelcontextprotocol.io/specification/2025-11-25/server/resources#subscriptions
// and #list-changed-notification). Both carry no content: the client re-reads.
//
// COALESCING. Job, print and printer progress can change many times a
// second. Per session and per URI, at most one notification is sent per
// `minIntervalMs` (default 250 ms, so ≤ 4/s): the first at once, then one
// trailing notification at the end of the window for everything that arrived
// inside it. Nothing is lost, since a notification only says "re-read".
//
// DELIVERY. Notifications go on the session's standalone GET SSE stream,
// through the transport's event store, so a client that reconnects with
// `Last-Event-ID` gets what it missed (Streamable HTTP, "Resumability and
// Redelivery",
// https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#resumability-and-redelivery;
// src/mcp/eventStore.ts). A notification sent while no GET stream is open is
// stored the same way and replayed on the next resume.

export const DEFAULT_MIN_INTERVAL_MS = 250
export const DEFAULT_MAX_SUBSCRIPTIONS = 500

const LIST = '\u0000list_changed'

export type Notify = {
  updated(uri: string): Promise<void>
  listChanged(): Promise<void>
}

export class SubscriptionLimitError extends Error {
  override name = 'SubscriptionLimitError'
}

/** One MCP session's subscriptions and its coalescing sender. */
export class Subscriptions {
  readonly #uris = new Set<string>()
  readonly #notify: Notify
  readonly #minMs: number
  readonly #max: number
  readonly #windows = new Map<string, { last: number; timer?: NodeJS.Timeout }>()
  #closed = false

  constructor(notify: Notify, options: { minIntervalMs?: number; maxSubscriptions?: number } = {}) {
    this.#notify = notify
    this.#minMs = options.minIntervalMs ?? DEFAULT_MIN_INTERVAL_MS
    this.#max = options.maxSubscriptions ?? DEFAULT_MAX_SUBSCRIPTIONS
  }

  /** `uri` must already be canonical (catalog.ts `canonical`). Idempotent. */
  add(uri: string): void {
    if (this.#uris.has(uri)) return
    if (this.#uris.size >= this.#max) {
      throw new SubscriptionLimitError(`this session already has ${this.#max} resource subscriptions; unsubscribe from one first`)
    }
    this.#uris.add(uri)
  }

  delete(uri: string): void {
    this.#uris.delete(uri)
    const w = this.#windows.get(uri)
    clearTimeout(w?.timer)
    this.#windows.delete(uri)
  }

  has(uri: string): boolean {
    return this.#uris.has(uri)
  }

  get size(): number {
    return this.#uris.size
  }

  onEvent(event: BusEvent): void {
    const { uris, listChanged } = affectedBy(event)
    for (const uri of uris) if (this.#uris.has(uri)) this.#push(uri)
    if (listChanged) this.#push(LIST)
  }

  /** Events may have been missed: every subscribed resource, and the list, may have changed. */
  onResync(): void {
    for (const uri of this.#uris) this.#push(uri)
    this.#push(LIST)
  }

  /** The bus reconnected: NOTIFY-only resources (events.ts) may have changed unseen. */
  onReconnect(): void {
    for (const uri of this.#uris) if (NOTIFY_ONLY_PREFIXES.some((p) => uri.startsWith(p))) this.#push(uri)
  }

  close(): void {
    this.#closed = true
    for (const w of this.#windows.values()) clearTimeout(w.timer)
    this.#windows.clear()
    this.#uris.clear()
  }

  #push(key: string): void {
    if (this.#closed) return
    const now = Date.now()
    const w = this.#windows.get(key)
    if (!w || (now - w.last >= this.#minMs && w.timer === undefined)) {
      this.#windows.set(key, { last: now })
      this.#send(key)
      return
    }
    if (w.timer !== undefined) return
    w.timer = setTimeout(() => {
      w.timer = undefined
      w.last = Date.now()
      // Unsubscribed meanwhile: nothing to say.
      if (key === LIST || this.#uris.has(key)) this.#send(key)
    }, Math.max(0, this.#minMs - (now - w.last)))
    w.timer.unref()
  }

  #send(key: string): void {
    if (this.#closed) return
    const sent = key === LIST ? this.#notify.listChanged() : this.#notify.updated(key)
    // A session whose transport has gone cannot be told; it ends on its own.
    sent.catch(() => {})
  }
}

/** The process-wide fan-out from one event source to every session's subscriptions. */
export class ResourceHub {
  readonly #sessions = new Set<Subscriptions>()
  readonly #stop: () => void
  readonly options: { minIntervalMs?: number; maxSubscriptions?: number }

  constructor(source: EventSource | undefined, options: { minIntervalMs?: number; maxSubscriptions?: number } = {}) {
    this.options = options
    this.#stop =
      source?.follow({
        onEvent: (event) => {
          for (const s of this.#sessions) s.onEvent(event)
        },
        onResync: () => {
          for (const s of this.#sessions) s.onResync()
        },
        onReconnect: () => {
          for (const s of this.#sessions) s.onReconnect()
        },
      }) ?? (() => {})
  }

  /** A session's subscriptions, following the bus until `detach` is called. */
  attach(notify: Notify): { subscriptions: Subscriptions; detach: () => void } {
    const subscriptions = new Subscriptions(notify, this.options)
    this.#sessions.add(subscriptions)
    return {
      subscriptions,
      detach: () => {
        subscriptions.close()
        this.#sessions.delete(subscriptions)
      },
    }
  }

  get sessions(): number {
    return this.#sessions.size
  }

  close(): void {
    this.#stop()
    for (const s of this.#sessions) s.close()
    this.#sessions.clear()
  }
}
