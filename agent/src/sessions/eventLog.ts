import { EventEmitter } from 'node:events'
import type { Sql } from 'postgres'
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
// Going live: a write on THIS replica wakes local followers at once; writes on
// another replica are picked up by polling every `pollMs`. #264 replaces the
// poll with the bus's LISTEN/NOTIFY (spec §7) — see `wake()`.

export type LoggedEvent = { seq: number; event: ServerEvent }

export const DEFAULT_POLL_MS = 1000
const PAGE = 500

export class EventLog {
  private readonly sql: Sql
  private readonly pollMs: number
  private readonly emitter = new EventEmitter()
  /** Bumped on every local append, so a follower cannot miss a wake between read and wait. */
  private readonly versions = new Map<string, number>()

  constructor(sql: Sql, options: { pollMs?: number } = {}) {
    this.sql = sql
    this.pollMs = options.pollMs ?? DEFAULT_POLL_MS
    this.emitter.setMaxListeners(0)
  }

  /** Appends events to a session's log; returns their seqs. The session row must exist. */
  async append(sessionId: string, events: readonly ServerEvent[]): Promise<number[]> {
    if (events.length === 0) return []
    const texts = events.map((e) => JSON.stringify(e))
    const rows = await this.sql<{ seq: string }[]>`
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
    this.wake(sessionId)
    return rows.map((r) => Number(r.seq)).sort((a, b) => a - b)
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
   * Replays the log after `afterSeq`, then follows it until `signal` aborts
   * (or the consumer stops iterating).
   */
  async *follow(sessionId: string, afterSeq = 0, signal?: AbortSignal): AsyncGenerator<LoggedEvent> {
    let last = afterSeq
    while (!signal?.aborted) {
      const version = this.versions.get(sessionId) ?? 0
      const rows = await this.read(sessionId, last)
      for (const row of rows) {
        if (signal?.aborted) return
        yield row
        last = row.seq
      }
      if (rows.length === PAGE) continue
      await this.waitForChange(sessionId, version, signal)
    }
  }

  /**
   * Wakes this replica's followers of a session. The seam for #264: its
   * LISTEN handler calls this for `session.*` notifications from other
   * replicas, and the poll then only backs it up.
   */
  wake(sessionId: string): void {
    this.versions.set(sessionId, (this.versions.get(sessionId) ?? 0) + 1)
    this.emitter.emit(sessionId)
  }

  private waitForChange(sessionId: string, seen: number, signal?: AbortSignal): Promise<void> {
    if ((this.versions.get(sessionId) ?? 0) !== seen || signal?.aborted) return Promise.resolve()
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
