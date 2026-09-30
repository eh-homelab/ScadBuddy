import { EventEmitter } from 'node:events'
import type { Sql, TransactionSql } from 'postgres'
import type { AppendHook } from './busEvents.js'
import type { ServerEvent } from './protocol.js'

// Each session's panel-protocol events, in order, in `ai_session_events`
// (#300). Attach (manager.ts) replays this log and then follows it, so a
// watcher sees exactly the sequence a live watcher saw, whichever replica ran
// the turn, and can resume from the last `seq` it has (the "resumability
// event log" of spec §9).
//
// Ordering without gaps: `seq` comes from `ai_sessions.event_seq`, bumped in
// the same statement that inserts the events. The UPDATE holds the session
// row's lock until commit, so two writers of one session commit in seq order
// and a reader that has seen seq N never later finds a smaller one appear.
//
// Going live: a write on THIS replica wakes local followers at once. A write
// on another replica is announced on the event bus (spec §7, #300): `onAppend`
// publishes it as `session.*` (busEvents.ts `SessionEventPublisher`), and every
// replica's LISTEN consumer calls `wake()` (busEvents.ts
// `followSessionEvents`). Polling every `pollMs` stays as the fallback for a
// NOTIFY that never arrives (a dropped LISTEN connection, no listener).

export type LoggedEvent = { seq: number; event: ServerEvent }

export const DEFAULT_POLL_MS = 1000
const PAGE = 500

export class EventLog {
  private readonly sql: Sql
  private readonly pollMs: number
  private readonly emitter = new EventEmitter()
  /**
   * Per session with at least one local follower: how many follow it, and a
   * counter bumped on every wake, so a follower cannot miss a wake between its
   * read and its wait. The entry exists only while a follower does (created
   * by the first, deleted by the last), so the map is bounded by the number
   * of sessions being watched on this replica, not by every session it has
   * ever written. A wake with no entry has nobody to wake; a follower that
   * starts later reads the table first, so it cannot miss those events.
   */
  private readonly watched = new Map<string, { followers: number; version: number }>()
  /** Told of every append once it has committed (busEvents.ts, the `session.*` publisher). */
  private readonly onAppend: AppendHook | undefined

  constructor(sql: Sql, options: { pollMs?: number; onAppend?: AppendHook } = {}) {
    this.sql = sql
    this.pollMs = options.pollMs ?? DEFAULT_POLL_MS
    this.onAppend = options.onAppend
    this.emitter.setMaxListeners(0)
  }

  /**
   * Appends events to a session's log; returns their seqs. The session row must exist.
   * With `tx`, the events are written in that transaction, so they commit with the
   * change they report; the caller then calls `committed` once it has committed.
   */
  async append(sessionId: string, events: readonly ServerEvent[], tx?: TransactionSql): Promise<number[]> {
    if (events.length === 0) return []
    const texts = events.map((e) => JSON.stringify(e))
    const db = tx ?? this.sql
    const rows = await db<{ seq: string }[]>`
      WITH s AS (
        UPDATE ai_sessions SET event_seq = event_seq + ${texts.length}, updated_at = now()
        WHERE id = ${sessionId}
        RETURNING event_seq - ${texts.length} AS base
      )
      INSERT INTO ai_session_events (session_id, seq, event)
      SELECT ${sessionId}, s.base + e.ord, e.event
      FROM s, unnest(${this.sql.array(texts)}::text[]) WITH ORDINALITY AS e(event, ord)
      RETURNING seq`
    if (rows.length !== texts.length) throw new Error(`session ${sessionId} does not exist`)
    const seqs = rows.map((r) => Number(r.seq)).sort((a, b) => a - b)
    if (!tx) this.committed(sessionId, events, seqs)
    return seqs
  }

  /**
   * An append has committed: wakes this replica's followers and announces it on
   * the bus (`onAppend`). `append` calls it itself; after an append with `tx`,
   * the caller calls it once that transaction has committed.
   */
  committed(sessionId: string, events: readonly ServerEvent[], seqs: readonly number[]): void {
    this.wake(sessionId)
    this.onAppend?.(sessionId, events, seqs.at(-1) ?? 0)
  }

  /**
   * Announces a change to the session row that is not a transcript event (a
   * handoff offer, manager.ts) on the bus, as a batch of `events` at the log's
   * current seq; nothing is appended, so local followers have nothing new.
   */
  async announce(sessionId: string, events: readonly ServerEvent[]): Promise<void> {
    if (!this.onAppend) return
    this.onAppend(sessionId, events, await this.lastSeq(sessionId))
  }

  /** Events after `afterSeq`, oldest first. */
  async read(sessionId: string, afterSeq = 0, limit = PAGE): Promise<LoggedEvent[]> {
    const rows = await this.sql<{ seq: string; event: string }[]>`
      SELECT seq, event FROM ai_session_events
      WHERE session_id = ${sessionId} AND seq > ${afterSeq}
      ORDER BY seq LIMIT ${limit}`
    return rows.map((r) => ({ seq: Number(r.seq), event: JSON.parse(r.event) as ServerEvent }))
  }

  /**
   * The last seq handed out for a session (0 when it has none), to follow it
   * from "now". `sessionId` must be a session's id (a UUID).
   */
  async lastSeq(sessionId: string): Promise<number> {
    const [row] = await this.sql<{ event_seq: string }[]>`
      SELECT event_seq FROM ai_sessions WHERE id = ${sessionId}`
    return row ? Number(row.event_seq) : 0
  }

  /**
   * Replays the log after `afterSeq`, then follows it until `signal` aborts
   * (or the consumer stops iterating).
   */
  async *follow(sessionId: string, afterSeq = 0, signal?: AbortSignal): AsyncGenerator<LoggedEvent> {
    const entry = this.watched.get(sessionId) ?? { followers: 0, version: 0 }
    entry.followers += 1
    this.watched.set(sessionId, entry)
    try {
      let last = afterSeq
      while (!signal?.aborted) {
        const version = entry.version
        const rows = await this.read(sessionId, last)
        for (const row of rows) {
          if (signal?.aborted) return
          yield row
          last = row.seq
        }
        if (rows.length === PAGE) continue
        await this.waitForChange(sessionId, entry, version, signal)
      }
    } finally {
      // Runs on abort, on the consumer's break/return, and on a read error.
      entry.followers -= 1
      if (entry.followers === 0 && this.watched.get(sessionId) === entry) this.watched.delete(sessionId)
    }
  }

  /** Sessions with a local follower right now (for tests and metrics). */
  watchedSessions(): number {
    return this.watched.size
  }

  /**
   * Wakes this replica's followers of a session: after a local append, and
   * from the LISTEN consumer for `session.*` from other replicas
   * (busEvents.ts `followSessionEvents`); the poll then only backs it up.
   */
  wake(sessionId: string): void {
    const entry = this.watched.get(sessionId)
    if (!entry) return
    entry.version += 1
    this.emitter.emit(sessionId)
  }

  /** Wakes every local follower: the bus may have missed events (a resync, a reconnect). */
  wakeAll(): void {
    for (const id of [...this.watched.keys()]) this.wake(id)
  }

  private waitForChange(
    sessionId: string,
    entry: { version: number },
    seen: number,
    signal?: AbortSignal,
  ): Promise<void> {
    if (entry.version !== seen || signal?.aborted) return Promise.resolve()
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer)
        this.emitter.off(sessionId, done)
        signal?.removeEventListener('abort', done)
        resolve()
      }
      const timer = setTimeout(done, this.pollMs)
      this.emitter.on(sessionId, done)
      signal?.addEventListener('abort', done, { once: true })
    })
  }
}
