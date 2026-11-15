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

// A fork is copy-on-write for its log (#4212): its `ai_sessions` row names the session it was
// forked from (`fork_parent_id`) and the seq of the last event of that session's log, as that
// session reads it, it takes (`fork_event_cut`). Its own `session.started` is its seq 1, and its
// own later events come after the cut (`inheritThrough`), so `read` is the parent's conversation
// events through the cut (for a fork of a fork, the parent's own read, through the smallest cut on
// the chain) in among the child's own rows, by seq, each addressed to the child. Nothing is copied
// at the fork. Forgetting a parent copies each child's range into the child first
// (`copyForkEvents`, store.ts `copyForkRanges`), at the same seqs, and clears the link. A fork made
// before the cut has its own copy and no `fork_event_cut`, and the chain stops there.

export type LoggedEvent = { seq: number; event: ServerEvent }

/** The conversation events a fork takes; lifecycle events (status, owner, result) are the parent's own. */
export const FORK_EVENT_TYPES: readonly ServerEvent['type'][] = ['user.turn', 'assistant.text.delta', 'assistant.text.done', 'tool.call', 'tool.result']

export const DEFAULT_POLL_MS = 1000
const PAGE = 500

/**
 * `sessionId` and the sessions its log reads through: each one's id, its depth (the session itself
 * is 0), and the highest seq of its log taken (`bound`, NULL for the session itself). Stops at a
 * session with no event cut. Links are only ever made to an existing session and only ever
 * cleared, so the chain has no cycle.
 */
export function forkChain(sql: Sql | TransactionSql, sessionId: string) {
  return sql`
    WITH RECURSIVE chain (id, parent, cut, bound, depth) AS (
      SELECT id, fork_parent_id, fork_event_cut, NULL::bigint, 0 FROM ai_sessions WHERE id = ${sessionId}::uuid
      UNION ALL
      SELECT s.id, s.fork_parent_id, s.fork_event_cut, LEAST(c.bound, c.cut), c.depth + 1
        FROM ai_sessions s JOIN chain c ON s.id = c.parent WHERE c.cut IS NOT NULL
    )`
}

/**
 * The rows of `sessionId`'s log after `afterSeq`, by seq: its own (with `ancestors`) and those it
 * takes through its fork chain, the latter re-addressed to it. One statement, so one snapshot.
 */
async function chainRows(
  sql: Sql | TransactionSql,
  sessionId: string,
  afterSeq: number,
  limit: number | null,
  own: boolean,
): Promise<LoggedEvent[]> {
  const rows = await sql<{ seq: string; event: string; depth: number }[]>`
    ${forkChain(sql, sessionId)}
    SELECT e.seq, e.event, c.depth FROM chain c JOIN ai_session_events e ON e.session_id = c.id
     WHERE e.seq > ${afterSeq}
       AND (${own ? sql`c.depth = 0 OR` : sql``} (c.depth > 0 AND e.seq <= c.bound AND e.event::jsonb->>'type' = ANY(${sql.array(FORK_EVENT_TYPES as string[])})))
     ORDER BY e.seq ${limit === null ? sql`` : sql`LIMIT ${limit}`}`
  return rows.map((r) => {
    const event = JSON.parse(r.event) as ServerEvent
    return { seq: Number(r.seq), event: r.depth === 0 ? event : { ...event, sessionId } }
  })
}

/**
 * Before the parent of `sessionId` goes (store.ts `copyForkRanges`, in forgetSubject's
 * transaction): the events it reads through its chain become rows of its own, at the same seqs.
 * The caller clears its link. The events copied, so their images can be copied too.
 */
export async function copyForkEvents(tx: TransactionSql, sessionId: string): Promise<ServerEvent[]> {
  const rows = await chainRows(tx, sessionId, 0, null, false)
  for (let at = 0; at < rows.length; at += PAGE) {
    const batch = rows.slice(at, at + PAGE)
    await tx`
      INSERT INTO ai_session_events (session_id, seq, event)
      SELECT ${sessionId}, e.seq, e.event
        FROM unnest(${tx.array(batch.map((r) => r.seq))}::bigint[], ${tx.array(batch.map((r) => JSON.stringify(r.event)))}::text[]) AS e(seq, event)`
  }
  return rows.map((r) => r.event)
}

/**
 * The `WHERE` of a session's `user.turn` rows with `turnId`, in `ai_session_events`: the
 * one copy of how a turn's start is found, for every reader of it (#3361). A `turnId`
 * logged twice matches both rows; every reader takes the latest (`ORDER BY seq DESC`),
 * so they agree on which start they use (#3750). Its columns are unqualified, so it
 * assumes `ai_session_events` is the only table in scope with a `session_id` or an
 * `event` column; a join that adds one must qualify them instead.
 */
export function userTurnRows(sql: Sql, sessionId: string, turnId: string) {
  return sql`session_id = ${sessionId} AND event::jsonb->>'type' = 'user.turn' AND event::jsonb->>'turnId' = ${turnId}`
}

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

  /**
   * A new fork (`sessionId`, its `session.started` already logged) takes its parent's log through
   * seq `cut`: its own events go on after it. Its row must already name the parent.
   */
  async inheritThrough(sessionId: string, cut: number, tx?: TransactionSql): Promise<void> {
    await (tx ?? this.sql)`
      UPDATE ai_sessions SET fork_event_cut = ${cut}, event_seq = GREATEST(event_seq, ${cut}) WHERE id = ${sessionId}`
  }

  /** Events after `afterSeq`, oldest first: a fork's through its chain too. */
  async read(sessionId: string, afterSeq = 0, limit = PAGE): Promise<LoggedEvent[]> {
    return chainRows(this.sql, sessionId, afterSeq, limit, true)
  }

  /**
   * The words of a turn's `user.turn` (the last one logged with that `turnId`), or
   * undefined when none is. One round trip in place of reading the log (#2419). The
   * JSON is not indexed, so Postgres still parses every row of the session to find
   * it: the read's cost still grows with the session, only the transfer does not.
   */
  async userTurnText(sessionId: string, turnId: string): Promise<string | undefined> {
    const [row] = await this.sql<{ event: string }[]>`
      SELECT event FROM ai_session_events WHERE ${userTurnRows(this.sql, sessionId, turnId)}
      ORDER BY seq DESC LIMIT 1`
    const turn = row ? (JSON.parse(row.event) as ServerEvent) : undefined
    return turn?.type === 'user.turn' ? turn.text : undefined
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
