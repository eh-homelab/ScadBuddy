import type { Sql, TransactionSql } from 'postgres'
import { crudRead, wellFormed } from '../api/crudRead.js'
import { isUuid } from '../harness/uuid.js'
import { appendEventsIn } from './eventLog.js'
import { event, type ServerEvent, type SessionStatus } from './protocol.js'
import type { SessionRuntime } from './durable.js'
import { sessionPath, type Who } from './rows.js'
import { wordTail } from './turnCore.js'
import { POOL_BUDGET, POOL_COST } from './turnShared.js'

// A durable turn's writes of its session's row (#6449, #5299 step 9), behind one interface:
// `PgTurnRows`, the SQL the turn's code ran itself (durable.ts, turnHost.ts,
// temporal/session/finishActivity.ts, durableSweep.ts), and `CrudTurnRows`, the same over the
// API's CRUD routes (backend api/crud_agent_turns.py, area `session-turns`), each method one
// route and one transaction, its fence checked there (#5299 D2). main.ts picks one per call with
// `gated` once the API lists the area, the SQL until then (D9).
//
// The fences: a placement takes the turn (`turn_attempt`, which nothing else writes) only while
// the row runs it; a turn's end is written only while the row runs that turn
// (`durable_turn_id`), so a retry writes nothing twice and never over the next turn; a lost
// turn's end only while the row is as the sweep read it (`event_seq`). A turn's closing events go
// in unworded (`turnEvents` with no spend) and the end words them with the lineage's spend and
// budget read under the row's lock (turnCore.ts `wordTail`).

/** A durable row's statuses while it holds a turn (durableSweep.ts `TURN_STATUSES`). */
const RUNNING = ['running', 'waiting_approval', 'waiting_input']

/** The code of the `error` event that says a queued message could not start. */
export const QUEUED_DROPPED = 'queued_message_dropped'

/** Events appended in one transaction: whoever made the call tells `EventLog.committed`. */
export type Logged = { events: ServerEvent[]; seqs: number[] }

/** What a send's claim found: the status before it, which a give-back restores, and the runtime. */
export type SendClaimed = { prior_status: SessionStatus; runtime: SessionRuntime | null }

/** A send's claim: whose turn, which, the tiers its tool activities run with, and whether it is bare (no background built-ins). */
export type SendClaim = { author: Who; turnId: string; tiers: readonly string[] | null; bare: boolean }

/** The queue's head as a claim takes it: `started`, its `user.turn` and `session.status running`. */
export type NextClaim = SendClaim & { started: ServerEvent[] }

/** What a queue claim came to: the row is the head's (`started`), runs another (`busy`), or cannot run it (`dropped`). */
export type BeginOutcome = 'started' | 'busy' | 'dropped'
export type Begun = { outcome: BeginOutcome; logged?: Logged | undefined }

/** Who runs the row's turn and where, as the turn's readers read it. */
export type TurnState = {
  status: SessionStatus
  durable_turn_id: string | null
  turn_attempt: string | null
  interrupt_requested: boolean
  owner_kind: string
  runtime: SessionRuntime | null
  /** The claim the sweep's guard reads. */
  event_seq: string
  cost_usd: number
  /** No write for the grace asked for. */
  settled: boolean
}

/** A row in TURN_STATUSES as the sweep reads it: its claim (`event_seq`). */
export type StaleRow = { id: string; event_seq: string }

/**
 * What a placement spent since its last write, added whoever holds the row. `key` is the write's
 * own: the route adds a key's write once, so a write sent again after its answer was lost is not
 * counted twice (`ai_turn_spend_keys`). The SQL path, whose write is one statement, ignores it.
 */
export type SpendDelta = { key: string; costUsd: number; unpricedUsd: number; turns: number }

export interface TurnRows {
  /** A send's claim (durable.ts `#send`), or undefined when the row cannot take it. */
  sendClaim(id: string, claim: SendClaim): Promise<SendClaimed | undefined>
  /** The row's `event_seq` while `author` may queue behind its running turn, else undefined. */
  queueable(id: string, author: Who): Promise<number | undefined>
  /** The row's turn, or undefined for a session that is gone. */
  state(id: string, graceMs?: number): Promise<TurnState | undefined>
  /** A refused send's claim given back to `prior`, with its `session.status`; undefined when the row no longer runs. */
  giveBack(id: string, prior: SessionStatus): Promise<Logged | undefined>
  /** A Stop's flag, while a turn holds the row. */
  interrupt(id: string): Promise<boolean>
  /** The placement takes the turn while the row runs it, and the transcript's mark (once per `markKey`); undefined otherwise. */
  placement(id: string, turnId: string, attempt: string, markKey: string): Promise<number | undefined>
  /** What a placement spent, whoever holds the row; false for a session that is gone. */
  spend(id: string, delta: SpendDelta): Promise<boolean>
  /**
   * The turn's end while the row runs `turnId` (`ended`, its `tail` worded with the lineage's spend
   * and budget), then the queue's `next` claimed, in one transaction.
   */
  finish(id: string, turnId: string, tail: readonly ServerEvent[], next?: NextClaim): Promise<{ ended?: Logged | undefined; next?: Begun | undefined }>
  /** The queue's head claimed while no turn ran (#6386). */
  begin(id: string, next: NextClaim): Promise<Begun>
  /** The queue's `session.queue` event, under the row's lock; undefined for a session that is gone. */
  queueChanged(id: string, queued: ServerEvent): Promise<Logged | undefined>
  /** The log's last `session.queue` event. */
  lastQueue(id: string): Promise<ServerEvent | undefined>
  /** Rows holding a turn with no write for `graceMs`, by id after `after`. */
  stale(after: string, graceMs: number, limit: number): Promise<StaleRow[]>
  /** A lost turn's end (`tail`, to `idle`) while the row holds a turn at `eventSeq`; undefined otherwise. */
  endLost(id: string, eventSeq: string, tail: readonly ServerEvent[]): Promise<Logged | undefined>
}

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

  giveBack(id: string, prior: SessionStatus): Promise<Logged | undefined> {
    return this.#sql.begin(async (tx) => {
      const released = await tx`
        UPDATE ai_sessions SET status = ${prior}, durable_turn_id = NULL, updated_at = now() WHERE id = ${id} AND status = 'running'`
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

// -- over the CRUD routes ----------------------------------------------------------------------

type WireLogged = { events: string[]; seqs: number[] }
type WireBegun = { outcome: BeginOutcome; logged: WireLogged | null }

const parsed = (wire: WireLogged | null): Logged | undefined =>
  wire === null ? undefined : { events: wire.events.map((text) => JSON.parse(text) as ServerEvent), seqs: wire.seqs }

const begun = (wire: WireBegun): Begun => {
  const written = parsed(wire.logged)
  return { outcome: wire.outcome, ...(written ? { logged: written } : {}) }
}

/**
 * A turn id as the routes take it: a UUID, as `durable_turn_id` is, lowercase, as Postgres writes
 * one; anything else throws, as the SQL's cast does.
 */
function turnIdOf(turnId: string): string {
  if (!isUuid(turnId)) throw new Error(`turn ${JSON.stringify(turnId)} is not a turn id`)
  return turnId.toLowerCase()
}

const nextBody = (next: NextClaim) => ({
  turn_id: turnIdOf(next.turnId),
  author: { kind: next.author.kind, id: next.author.id },
  tiers: next.tiers ? [...next.tiers] : null,
  bare: next.bare,
  started: next.started.map((e) => JSON.stringify(e)),
})

/** A route's longest wait: a turn's end waits on the row's lock behind the turn's own writes. */
const TIMEOUT_MS = 30_000

/** `PgTurnRows` over the API's CRUD routes (area `session-turns`). */
export class CrudTurnRows implements TurnRows {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  #call<T>(step: string, path: string, body?: unknown): Promise<T> {
    // Well-formed: the API refuses a lone surrogate (crudRead.ts).
    return crudRead<T>(this.#fetch, this.#base, step, path, {
      ...(body === undefined ? {} : { method: 'POST' as const, body: wellFormed(body) }),
      timeoutMs: TIMEOUT_MS,
    })
  }

  /** Session `id`'s turn path, or a throw: a session id is a UUID, as the SQL's cast takes one. */
  #path(id: string, tail = ''): string {
    const path = sessionPath(id)
    if (path === undefined) throw new Error(`session ${JSON.stringify(id)} is not a session id`)
    return `${path}/turn${tail}`
  }

  async sendClaim(id: string, claim: SendClaim): Promise<SendClaimed | undefined> {
    const body = {
      author: { kind: claim.author.kind, id: claim.author.id },
      turn_id: turnIdOf(claim.turnId),
      tiers: claim.tiers ? [...claim.tiers] : null,
      bare: claim.bare,
    }
    const { claimed } = await this.#call<{ claimed: SendClaimed | null }>(`a turn's claim of session ${id}`, this.#path(id, '/send-claim'), body)
    return claimed ?? undefined
  }

  async queueable(id: string, author: Who): Promise<number | undefined> {
    const body = { author: { kind: author.kind, id: author.id } }
    const { seq } = await this.#call<{ seq: number | null }>(`the queue of session ${id}`, this.#path(id, '/queueable'), body)
    return seq ?? undefined
  }

  async state(id: string, graceMs = 0): Promise<TurnState | undefined> {
    const query = new URLSearchParams({ grace_ms: String(Math.max(0, Math.round(graceMs))) })
    const { turn } = await this.#call<{ turn: TurnState | null }>(`the turn of session ${id}`, `${this.#path(id)}?${query.toString()}`)
    return turn ?? undefined
  }

  async giveBack(id: string, prior: SessionStatus): Promise<Logged | undefined> {
    const answer = await this.#call<{ logged: WireLogged | null }>(`a claim of session ${id} given back`, this.#path(id, '/give-back'), { prior })
    return parsed(answer.logged)
  }

  async interrupt(id: string): Promise<boolean> {
    return (await this.#call<{ changed: boolean }>(`a stop of session ${id}`, this.#path(id, '/interrupt'), {})).changed
  }

  async placement(id: string, turnId: string, attempt: string, markKey: string): Promise<number | undefined> {
    const body = { turn_id: turnIdOf(turnId), attempt, mark_key: markKey }
    const { mark } = await this.#call<{ mark: number | null }>(`a placement of turn ${turnId}`, this.#path(id, '/placement'), body)
    return mark ?? undefined
  }

  async spend(id: string, delta: SpendDelta): Promise<boolean> {
    const body = { key: delta.key, cost_usd: delta.costUsd, unpriced_usd: delta.unpricedUsd, turns: delta.turns }
    return (await this.#call<{ written: boolean }>(`the spend of session ${id}`, this.#path(id, '/spend'), body)).written
  }

  async finish(id: string, turnId: string, tail: readonly ServerEvent[], next?: NextClaim): Promise<{ ended?: Logged; next?: Begun }> {
    const body = { turn_id: turnIdOf(turnId), tail: tail.map((e) => JSON.stringify(e)), ...(next ? { next: nextBody(next) } : {}) }
    const answer = await this.#call<{ ended: WireLogged | null; next: WireBegun | null }>(`the end of turn ${turnId}`, this.#path(id, '/finish'), body)
    const ended = parsed(answer.ended)
    return { ...(ended ? { ended } : {}), ...(answer.next ? { next: begun(answer.next) } : {}) }
  }

  async begin(id: string, next: NextClaim): Promise<Begun> {
    return begun(await this.#call<WireBegun>(`the start of turn ${next.turnId}`, this.#path(id, '/begin'), { next: nextBody(next) }))
  }

  async queueChanged(id: string, queued: ServerEvent): Promise<Logged | undefined> {
    const answer = await this.#call<{ logged: WireLogged | null }>(`the queue of session ${id}`, this.#path(id, '/queue'), { event: JSON.stringify(queued) })
    return parsed(answer.logged)
  }

  async lastQueue(id: string): Promise<ServerEvent | undefined> {
    const answer = await this.#call<{ event: string | null }>(`the queue of session ${id}`, this.#path(id, '/queue'))
    return answer.event === null ? undefined : (JSON.parse(answer.event) as ServerEvent)
  }

  async stale(after: string, graceMs: number, limit: number): Promise<StaleRow[]> {
    const query = new URLSearchParams({ after, grace_ms: String(Math.max(0, Math.round(graceMs))), limit: String(limit) })
    return (await this.#call<{ rows: StaleRow[] }>('the turns left running', `/turns/stale?${query.toString()}`)).rows
  }

  async endLost(id: string, eventSeq: string, tail: readonly ServerEvent[]): Promise<Logged | undefined> {
    const body = { event_seq: Number(eventSeq), tail: tail.map((e) => JSON.stringify(e)) }
    return parsed((await this.#call<{ logged: WireLogged | null }>(`a lost turn of session ${id}`, this.#path(id, '/end-lost'), body)).logged)
  }
}
