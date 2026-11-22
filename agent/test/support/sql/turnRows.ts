import type { Sql, TransactionSql } from 'postgres'
import { event, type ServerEvent, type SessionStatus } from '../../../src/sessions/protocol.js'
import type { Who } from '../../../src/sessions/rows.js'
import { wordTail } from '../../../src/sessions/turnCore.js'
import {
  type Begun,
  type Logged,
  type NextClaim,
  QUEUED_DROPPED,
  type SendClaim,
  type SendClaimed,
  type SpendDelta,
  type StaleRow,
  type TurnRows,
  type TurnState,
} from '../../../src/sessions/turnRows.js'
import { appendEventsIn } from './eventRows.js'
import { POOL_BUDGET, POOL_COST } from './sessionRows.js'

// `TurnRows` straight on the test database, the statements the API's routes run (backend
// agent_data/turns.py), for tests with no API (#5299).

/** A durable row's statuses while it holds a turn (durableSweep.ts `TURN_STATUSES`). */
const RUNNING = ['running', 'waiting_approval', 'waiting_input']

const tiersJson = (sql: Sql | TransactionSql, claim: SendClaim) =>
  claim.tiers ? sql.json({ by: { kind: claim.author.kind, id: claim.author.id }, tiers: [...claim.tiers] }) : null

type ClaimRow = {
  status: SessionStatus
  durable_turn_id: string | null
  owner_kind: string
  owner_id: string
  archived: boolean
  runtime: string | null
  spent: string | null
  budget: string
}

/** Why the session cannot run a queued message now; undefined when it can (DurableTurns.send's claim condition). */
function whyNot(row: ClaimRow, author: Who): string | undefined {
  if (row.status === 'done') return 'the session is done'
  if (row.archived) return 'the session is archived'
  if (row.runtime === 'durable') return "the session's conversation ended with the old session runtime"
  if (!(row.owner_kind === author.kind && (row.owner_id === author.id || row.owner_kind === 'browser'))) return 'the session was handed off'
  if (Number(row.spent ?? 0) >= Number(row.budget)) return 'the session has spent its budget'
  return undefined
}

async function logged(tx: TransactionSql | Sql, id: string, events: ServerEvent[]): Promise<Logged> {
  return { events, seqs: await appendEventsIn(tx, id, events) }
}

/** Claims the row for the queue's head in `tx` (#6386); see `TurnRows.begin`. A head already claimed (a retry) is `started` and writes nothing. */
async function claimQueued(tx: TransactionSql, id: string, next: NextClaim): Promise<Begun> {
  const [row] = await tx.unsafe<ClaimRow[]>(
    `SELECT status, durable_turn_id, owner_kind, owner_id, archived_at IS NOT NULL AS archived, runtime,
       ${POOL_COST} AS spent, ${POOL_BUDGET} AS budget
     FROM ai_sessions WHERE id = $1 FOR UPDATE`,
    [id],
  )
  // The session is gone (forgotten): nothing to write.
  if (!row) return { outcome: 'dropped' }
  if (row.durable_turn_id === next.turnId) return { outcome: 'started' }
  if (RUNNING.includes(row.status)) return { outcome: 'busy' }
  const why = whyNot(row, next.author)
  if (why !== undefined) {
    const error = event({ type: 'error', sessionId: id, code: QUEUED_DROPPED, message: `A queued message was not sent: ${why}.` })
    return { outcome: 'dropped', logged: await logged(tx, id, [error]) }
  }
  await tx`
    UPDATE ai_sessions SET status = 'running', turn_tiers = ${tiersJson(tx, next)},
      durable_turn_id = ${next.turnId}, turn_flow = ${next.bare}, updated_at = now(), turn_started_at = now(),
      interrupt_requested = false
    WHERE id = ${id}`
  return { outcome: 'started', logged: await logged(tx, id, next.started) }
}

/** The SQL the turn's code ran itself (until the API lists area `session-turns`). */
export class PgTurnRows implements TurnRows {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async sendClaim(id: string, claim: SendClaim): Promise<SendClaimed | undefined> {
    // The status before the claim, which a give-back restores (a refused send keeps `failed`).
    // The lock makes a concurrent claim read the committed `running`, so its guarded UPDATE matches nothing.
    // `archived_at` is tested here too: an archive that commits after the manager's check
    // must not let a turn start on a chat the panel no longer lists (#2118).
    // The runtime is recorded by the first send of a session that has none (a classic
    // session's): AgentSession.
    const [claimed] = await this.#sql.unsafe<SendClaimed[]>(
      `WITH prior AS (SELECT status AS prior_status FROM ai_sessions WHERE id = $1 FOR UPDATE)
       UPDATE ai_sessions SET status = 'running', turn_tiers = $4, durable_turn_id = $5, turn_flow = $6, updated_at = now(), turn_started_at = now(),
         interrupt_requested = false, runtime = coalesce(runtime, 'unified')
       FROM prior
       WHERE id = $1 AND owner_kind = $2 AND (owner_id = $3 OR owner_kind = 'browser')
         AND status NOT IN ('running', 'waiting_approval', 'waiting_input', 'done')
         AND archived_at IS NULL AND ${POOL_COST} < ${POOL_BUDGET}
       RETURNING prior.prior_status, ai_sessions.runtime`,
      [id, claim.author.kind, claim.author.id, tiersJson(this.#sql, claim), claim.turnId, claim.bare],
    )
    return claimed
  }

  async queueable(id: string, author: Who): Promise<number | undefined> {
    const [row] = await this.#sql.unsafe<{ seq: string }[]>(
      `SELECT event_seq AS seq FROM ai_sessions
       WHERE id = $1 AND owner_kind = $2 AND (owner_id = $3 OR owner_kind = 'browser')
         AND status IN ('running', 'waiting_approval', 'waiting_input') AND runtime = 'unified'
         AND archived_at IS NULL AND ${POOL_COST} < ${POOL_BUDGET}`,
      [id, author.kind, author.id],
    )
    return row ? Number(row.seq) : undefined
  }

  async state(id: string, graceMs = 0): Promise<TurnState | undefined> {
    const [row] = await this.#sql<TurnState[]>`
      SELECT status, durable_turn_id::text AS durable_turn_id, turn_attempt, interrupt_requested, owner_kind, runtime,
        event_seq::text AS event_seq, cost_usd, updated_at < now() - make_interval(secs => ${graceMs / 1000}) AS settled
      FROM ai_sessions WHERE id = ${id}`
    return row
  }

  giveBack(id: string, prior: SessionStatus, turnId: string): Promise<Logged | undefined> {
    return this.#sql.begin(async (tx) => {
      const released = await tx`
        UPDATE ai_sessions SET status = ${prior}, durable_turn_id = NULL, updated_at = now()
        WHERE id = ${id} AND status = 'running' AND durable_turn_id = ${turnId}`
      if (released.count === 0) return undefined
      return logged(tx, id, [event({ type: 'session.status', sessionId: id, status: prior })])
    })
  }

  async interrupt(id: string): Promise<boolean> {
    const flagged = await this.#sql`UPDATE ai_sessions SET interrupt_requested = true WHERE id = ${id} AND durable_turn_id IS NOT NULL`
    return flagged.count > 0
  }

  async placement(id: string, turnId: string, attempt: string, markKey: string): Promise<number | undefined> {
    const [row] = await this.#sql<{ turn_mark: string }[]>`
      UPDATE ai_sessions SET
        turn_attempt = ${attempt},
        turn_mark = CASE WHEN turn_mark_turn = ${markKey} THEN turn_mark ELSE (
          SELECT coalesce(max(e.id), 0) FROM ai_session_entries e WHERE e.session_id = ai_sessions.id::text AND e.subpath = ''
        ) END,
        turn_mark_turn = ${markKey}
      WHERE id = ${id} AND durable_turn_id = ${turnId} AND status IN ('running', 'waiting_approval', 'waiting_input')
      RETURNING turn_mark::text`
    return row ? Number(row.turn_mark) : undefined
  }

  async spend(id: string, delta: SpendDelta): Promise<boolean> {
    const [row] = await this.#sql<{ id: string }[]>`
      UPDATE ai_sessions
      SET cost_usd = cost_usd + ${delta.costUsd}, turns = turns + ${delta.turns},
          unpriced_cost_usd = unpriced_cost_usd + ${delta.unpricedUsd}, updated_at = now()
      WHERE id = ${id}
      RETURNING id`
    return row !== undefined
  }

  finish(id: string, turnId: string, tail: readonly ServerEvent[], next?: NextClaim): Promise<{ ended?: Logged; next?: Begun }> {
    return this.#sql.begin(async (tx) => {
      const [row] = await tx.unsafe<{ turns: number; budget: string; spent: string | null }[]>(
        `SELECT ai_sessions.turns, ${POOL_BUDGET} AS budget, ${POOL_COST} AS spent
         FROM ai_sessions WHERE id = $1 AND status = ANY($2) AND durable_turn_id = $3::uuid FOR UPDATE`,
        [id, RUNNING, turnId],
      )
      let ended: Logged | undefined
      // Written already, the next turn has the row, or the session is gone: nothing.
      if (row) {
        const worded = wordTail(tail, { spentUsd: Number(row.spent ?? 0), budgetUsd: Number(row.budget), turns: Number(row.turns) })
        const last = worded.at(-1) as Extract<ServerEvent, { type: 'session.status' }>
        await tx`UPDATE ai_sessions SET status = ${last.status}, durable_turn_id = NULL, interrupt_requested = false, updated_at = now() WHERE id = ${id}`
        ended = await logged(tx, id, worded)
      }
      // The queue's head, in the same transaction: the row never goes idle for a send to take.
      return { ...(ended ? { ended } : {}), ...(next ? { next: await claimQueued(tx, id, next) } : {}) }
    })
  }

  begin(id: string, next: NextClaim): Promise<Begun> {
    return this.#sql.begin((tx) => claimQueued(tx, id, next))
  }

  queueChanged(id: string, queued: ServerEvent): Promise<Logged | undefined> {
    return this.#sql.begin(async (tx) => {
      const [row] = await tx`SELECT 1 FROM ai_sessions WHERE id = ${id} FOR UPDATE`
      if (!row) return undefined
      return logged(tx, id, [queued])
    })
  }

  async lastQueue(id: string): Promise<ServerEvent | undefined> {
    // One read, unindexed as `userTurnText` is: Postgres parses the session's events to find it.
    const [row] = await this.#sql<{ event: string }[]>`
      SELECT event FROM ai_session_events
      WHERE session_id = ${id} AND event::jsonb->>'type' = 'session.queue'
      ORDER BY seq DESC LIMIT 1`
    return row ? (JSON.parse(row.event) as ServerEvent) : undefined
  }

  stale(after: string, graceMs: number, limit: number): Promise<StaleRow[]> {
    const sql = this.#sql
    return sql<StaleRow[]>`
      SELECT id, event_seq FROM ai_sessions
      WHERE status IN ${sql(RUNNING)} AND id::text > ${after}
        AND updated_at < now() - make_interval(secs => ${graceMs / 1000})
      ORDER BY id::text LIMIT ${limit}`
  }

  endLost(id: string, eventSeq: string, tail: readonly ServerEvent[]): Promise<Logged | undefined> {
    return this.#sql.begin(async (tx) => {
      const [claimed] = await tx`
        UPDATE ai_sessions SET status = 'idle', durable_turn_id = NULL, updated_at = now()
        WHERE id = ${id} AND status IN ${tx(RUNNING)}
          AND event_seq = ${eventSeq}
        RETURNING id`
      return claimed ? logged(tx, id, [...tail]) : undefined
    })
  }
}
