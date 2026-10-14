import { randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { type BusEvent, type EventSource, PG_CHANNEL } from '../events/bus.js'
import type { EventLog } from './eventLog.js'
import type { ServerEvent, SessionStatus } from './protocol.js'

// `session.*` on the event bus (#300; spec §7: "from the agent side
// `session.*`"). Every batch a session appends to its event log
// (eventLog.ts `append`) is announced on `scadbuddy_events` as ONE small
// event, ids only like every other kind (spec §7: "kind plus ids, and never
// content"):
//
//   { id, at, kind, session_id, seq, status?, replica }
//
//   session.started  the session was created (start, fork)
//   session.owner    it was handed off (a new owner, spec §6 "Handoff")
//   session.waiting  it is waiting for a human (`waiting_approval`, `waiting_input`)
//   session.done     a turn ended, or the session did (`idle`, `done`, `failed`)
//   session.message  anything else: new transcript events (a turn's text, tool calls)
//
// `seq` is the batch's last event-log seq, so a consumer knows how far to read;
// `replica` is this process's id, so its own wake-up consumer can skip what it
// already woke locally. What changed is re-read through the session's own API
// (sessions_get, `scadbuddy://sessions/{id}`, the chat socket).
//
// CONSUMERS. Postgres delivers a NOTIFY to every listening session
// (https://www.postgresql.org/docs/current/sql-notify.html), and the backend
// and the agent each LISTEN on the channel independently (spec §7):
//   - every agent replica's `followSessionEvents` wakes that replica's event-log
//     followers (eventLog.ts `wake`), so a watcher attached on replica B sees a
//     turn running on replica A without waiting for the poll;
//   - the MCP resource hub (resources/events.ts) turns them into
//     `notifications/resources/updated` for `scadbuddy://sessions/{id}`;
//   - the backend decodes them (backend/scadbuddy/core/events.py
//     `SessionBusEvent`) and routes them to no WebSocket topic: the UI follows a
//     session over the agent's own chat socket, which checks who may see it.
//
// NOT IN THE BACKEND'S `events` LOG. That log is the backend's (its advisory
// lock, its pruning by row count, backend/scadbuddy/core/pg_events.py), and a
// streaming turn would flood it. So a `session.*` NOTIFY sent while a
// listener's connection is down is not replayed; the listener reports the
// reconnect (events/bus.ts `onReconnect`) and the consumers above re-read
// everything session-shaped instead. The event log itself is durable and
// polled (`pollMs`), so a lost NOTIFY only costs latency.
//
// RATE. A streaming turn appends once per SDK message (every text delta).
// `session.message` is throttled per session to one NOTIFY per `throttleMs`
// (the first at once, then one trailing with the latest seq); the lifecycle
// kinds go at once. The payload is a few hundred bytes, far under NOTIFY's
// 8000-byte limit (same page).

export const SESSION_BUS_KINDS = [
  'session.started',
  'session.owner',
  'session.waiting',
  'session.done',
  'session.message',
] as const
export type SessionBusKind = (typeof SESSION_BUS_KINDS)[number]

export type SessionBusEvent = {
  id: string
  at: string
  kind: SessionBusKind
  session_id: string
  seq: number
  status?: SessionStatus
  replica: string
}

export const DEFAULT_THROTTLE_MS = 100

const WAITING: readonly SessionStatus[] = ['waiting_approval', 'waiting_input']
const SETTLED: readonly SessionStatus[] = ['idle', 'done', 'failed']

/** The one bus kind a batch of session events is announced as, and the status it leaves. */
export function busKindOf(events: readonly ServerEvent[]): { kind: SessionBusKind; status?: SessionStatus } {
  const statuses = events.flatMap((e) => (e.type === 'session.status' ? [e.status] : []))
  const status = statuses.at(-1)
  const withStatus = status === undefined ? {} : { status }
  if (events.some((e) => e.type === 'session.started')) return { kind: 'session.started', ...withStatus }
  if (events.some((e) => e.type === 'session.owner')) return { kind: 'session.owner', ...withStatus }
  if (status !== undefined && WAITING.includes(status)) return { kind: 'session.waiting', status }
  if (status !== undefined && SETTLED.includes(status)) return { kind: 'session.done', status }
  return { kind: 'session.message', ...withStatus }
}

/** Called by the event log after each append (eventLog.ts `onAppend`). Must not throw. */
export type AppendHook = (sessionId: string, events: readonly ServerEvent[], lastSeq: number) => void

/** Publishes each appended batch as a `session.*` NOTIFY (see the top of this file). */
export class SessionEventPublisher {
  /** This process, in every payload it sends. */
  readonly replica: string
  readonly #sql: Sql
  readonly #throttleMs: number
  readonly #onError: (err: unknown) => void
  /** Per session: when the last `session.message` went, and a trailing one waiting. */
  readonly #windows = new Map<
    string,
    { last: number; timer?: NodeJS.Timeout | undefined; seq?: number | undefined; status?: SessionStatus | undefined }
  >()
  #closed = false
  /** Payloads sent (tests, metrics). */
  sent = 0

  constructor(sql: Sql, options: { replica?: string; throttleMs?: number; onError?: (err: unknown) => void } = {}) {
    this.#sql = sql
    this.replica = options.replica ?? randomUUID()
    this.#throttleMs = options.throttleMs ?? DEFAULT_THROTTLE_MS
    this.#onError = options.onError ?? ((err) => console.error('session events: NOTIFY failed:', (err as Error).message))
  }

  /** The AppendHook: after the append committed, so a listener that hears it can read the rows. */
  readonly onAppend: AppendHook = (sessionId, events, lastSeq) => {
    if (this.#closed || events.length === 0) return
    const { kind, status } = busKindOf(events)
    if (kind !== 'session.message') {
      // Its seq covers any trailing message still waiting, and a lifecycle
      // change is where a session's window can go: the map stays bounded by
      // the sessions streaming right now.
      clearTimeout(this.#windows.get(sessionId)?.timer)
      this.#windows.delete(sessionId)
      this.#send(kind, sessionId, lastSeq, status)
      return
    }
    const now = performance.now()
    const w = this.#windows.get(sessionId)
    if (!w || (now - w.last >= this.#throttleMs && w.timer === undefined)) {
      this.#windows.set(sessionId, { last: now })
      this.#send(kind, sessionId, lastSeq, status)
      return
    }
    w.seq = Math.max(w.seq ?? 0, lastSeq)
    if (status !== undefined) w.status = status
    if (w.timer !== undefined) return
    w.timer = setTimeout(
      () => {
        w.timer = undefined
        w.last = performance.now()
        const { seq, status: latest } = w
        w.seq = undefined
        w.status = undefined
        if (seq !== undefined) this.#send('session.message', sessionId, seq, latest)
      },
      Math.max(0, this.#throttleMs - (now - w.last)),
    )
    w.timer.unref()
  }

  close(): void {
    this.#closed = true
    for (const w of this.#windows.values()) clearTimeout(w.timer)
    this.#windows.clear()
  }

  #send(kind: SessionBusKind, sessionId: string, seq: number, status: SessionStatus | undefined): void {
    const event: SessionBusEvent = {
      id: randomUUID().replaceAll('-', ''),
      at: new Date().toISOString(),
      kind,
      session_id: sessionId,
      seq,
      ...(status === undefined ? {} : { status }),
      replica: this.replica,
    }
    this.sent += 1
    this.#sql`SELECT pg_notify(${PG_CHANNEL}, ${JSON.stringify(event)})`.then(
      () => {},
      (err: unknown) => this.#onError(err),
    )
  }
}

/** Whether a bus event is one of these, with the session it names. */
export function sessionIdOf(event: BusEvent): string | undefined {
  return event.kind.startsWith('session.') && typeof event.session_id === 'string' ? event.session_id : undefined
}

/**
 * The LISTEN consumer that wakes this replica's event-log followers
 * (eventLog.ts `wake`, the seam #264 left for it) for sessions that another
 * replica wrote. Its own writes woke them already, in `append`. After a
 * resync or a reconnect every follower is woken, since a `session.*` NOTIFY
 * sent meanwhile is not replayed (top of this file). Returns the unfollow.
 */
export function followSessionEvents(source: EventSource, log: EventLog, replica: string): () => void {
  return source.follow({
    onEvent: (event) => {
      const sessionId = sessionIdOf(event)
      if (sessionId !== undefined && event.replica !== replica) log.wake(sessionId)
    },
    onResync: () => log.wakeAll(),
    onReconnect: () => log.wakeAll(),
  })
}
