import type { Sql, TransactionSql } from 'postgres'
import {
  type Committed,
  FORK_EVENT_TYPES,
  PAGE,
  ForkParentGone,
  type EventRows,
  type LoggedEvent,
  type TurnEvents,
  type TurnFence,
} from '../../../src/sessions/eventLog.js'
import type { ServerEvent } from '../../../src/sessions/protocol.js'
import { payloadKeyLock } from './payloadKeys.js'

// `EventRows` straight on the test database, the statements the API's routes run (backend
// agent_data/events.py), for tests with no API (#5299).

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
export async function chainRows(
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

/**
 * Appends `events` to the session's log on `db` (a transaction of the caller's, or the pool),
 * bumping `event_seq`; their seqs, oldest first. Whoever commits it calls EventLog `committed`
 * after. The API runs the same statement for a workspace write (backend agent_data/workspace.py).
 */
export async function appendEventsIn(db: Sql | TransactionSql, sessionId: string, events: readonly ServerEvent[]): Promise<number[]> {
  if (events.length === 0) return []
  const texts = events.map((e) => JSON.stringify(e))
  const rows = await db<{ seq: string }[]>`
    WITH s AS (
      UPDATE ai_sessions SET event_seq = event_seq + ${texts.length}, updated_at = now()
      WHERE id = ${sessionId}
      RETURNING event_seq - ${texts.length} AS base
    )
    INSERT INTO ai_session_events (session_id, seq, event)
    SELECT ${sessionId}, s.base + e.ord, e.event
    FROM s, unnest(${db.array(texts)}::text[]) WITH ORDINALITY AS e(event, ord)
    RETURNING seq`
  if (rows.length !== texts.length) throw new Error(`session ${sessionId} does not exist`)
  return rows.map((r) => Number(r.seq)).sort((a, b) => a - b)
}

export class PgEventRows implements EventRows {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async append(sessionId: string, events: readonly ServerEvent[], fence?: TurnFence, committed?: Committed): Promise<number[] | undefined> {
    const seqs = !fence
      ? await appendEventsIn(this.#sql, sessionId, events)
      : await this.#sql.begin(async (tx) => {
          const [held] = await tx`
            SELECT 1 FROM ai_sessions WHERE id = ${sessionId} AND turn_attempt = ${fence.attempt} AND durable_turn_id = ${fence.turnId} FOR UPDATE`
          return held ? appendEventsIn(tx, sessionId, events) : undefined
        })
    if (seqs && seqs.length > 0) committed?.(events, seqs)
    return seqs
  }

  read(sessionId: string, afterSeq: number, limit: number): Promise<LoggedEvent[]> {
    return chainRows(this.#sql, sessionId, afterSeq, limit, true)
  }

  async userTurnText(sessionId: string, turnId: string): Promise<string | undefined> {
    const [row] = await this.#sql<{ event: string }[]>`
      SELECT event FROM ai_session_events WHERE ${userTurnRows(this.#sql, sessionId, turnId)}
      ORDER BY seq DESC LIMIT 1`
    const turn = row ? (JSON.parse(row.event) as ServerEvent) : undefined
    return turn?.type === 'user.turn' ? turn.text : undefined
  }

  async lastSeq(sessionId: string): Promise<number> {
    const [row] = await this.#sql<{ event_seq: string }[]>`
      SELECT event_seq FROM ai_sessions WHERE id = ${sessionId}`
    return row ? Number(row.event_seq) : 0
  }

  async turnEvents(sessionId: string, turnId: string, after: string | undefined): Promise<TurnEvents> {
    const sql = this.#sql
    if (after === undefined) {
      const [start] = await sql<{ here: boolean; seq: string | null }[]>`
        SELECT EXISTS (SELECT 1 FROM ai_sessions WHERE id = ${sessionId}::uuid) AS here, e.seq
          FROM (SELECT 1) AS one
          LEFT JOIN ai_session_events e ON ${userTurnRows(sql, sessionId, turnId)}
          ORDER BY e.seq DESC NULLS LAST LIMIT 1`
      if (!start?.here) return { here: false, after: undefined, events: [] }
      if (start.seq === null) return { here: true, after: undefined, events: [] }
      after = start.seq
    }
    const rows = await sql<{ here: boolean; seq: string | null; event: string | null }[]>`
      SELECT EXISTS (SELECT 1 FROM ai_sessions WHERE id = ${sessionId}::uuid) AS here, e.seq, e.event
        FROM (SELECT 1) AS one
        LEFT JOIN LATERAL (
          SELECT seq, event FROM ai_session_events WHERE session_id = ${sessionId} AND seq > ${after} ORDER BY seq
        ) e ON true`
    if (!rows[0]?.here) return { here: false, after: undefined, events: [] }
    const events: ServerEvent[] = []
    for (const r of rows) {
      if (r.seq === null || r.event === null) continue
      events.push(JSON.parse(r.event) as ServerEvent)
      after = r.seq
    }
    return { here: true, after, events }
  }

  forkLog(childId: string, parentId: string, cut: number, started: readonly ServerEvent[], then: readonly ServerEvent[]): Promise<number[]> {
    return this.#sql.begin(async (tx) => {
      // Under the parent's lock, shared, as the insert was: a forget of the parent since the insert
      // has copied the child's transcript and cleared its link, and the log it reads through is gone.
      await tx`SELECT pg_advisory_xact_lock_shared(hashtext(${payloadKeyLock(`session-${parentId}`)}))`
      const [linked] = await tx`SELECT 1 FROM ai_sessions WHERE id = ${childId}::uuid AND fork_parent_id = ${parentId}::uuid`
      if (!linked) throw new ForkParentGone(`session ${parentId} was forgotten while it was forked`)
      const first = await appendEventsIn(tx, childId, started)
      await tx`
        UPDATE ai_sessions SET fork_event_cut = ${cut}, event_seq = GREATEST(event_seq, ${cut}) WHERE id = ${childId}`
      return [...first, ...(await appendEventsIn(tx, childId, then))]
    })
  }
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
