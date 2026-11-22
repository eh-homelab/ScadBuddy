import { CrudReadError, crudRead, wellFormed } from '../api/crudRead.js'
import { isUuid } from '../harness/uuid.js'
import type { ServerEvent, SessionStatus } from './protocol.js'
import type { SessionRuntime } from './durable.js'
import { sessionPath, type Who } from './rows.js'

// A durable turn's writes of its session's row (#6449, #5299 step 9), behind one interface
// (durable.ts, turnHost.ts, temporal/session/finishActivity.ts, durableSweep.ts): `CrudTurnRows`,
// over the API's CRUD routes (backend api/crud_agent_turns.py, area `session-turns`), each method
// one route and one transaction, its fence checked there (#5299 D2).
//
// The fences: a placement takes the turn (`turn_attempt`, which nothing else writes) only while
// the row runs it; a turn's end is written only while the row runs that turn
// (`durable_turn_id`), so a retry writes nothing twice and never over the next turn; a lost
// turn's end only while the row is as the sweep read it (`event_seq`). A turn's closing events go
// in unworded (`turnEvents` with no spend) and the end words them with the lineage's spend and
// budget read under the row's lock (turnCore.ts `wordTail`).

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
 * counted twice (`ai_turn_spend_keys`).
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
  giveBack(id: string, prior: SessionStatus, turnId: string): Promise<Logged | undefined>
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
 * one; anything else throws.
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

/**
 * A row write's request through the API (the turn routes, and an edit's, edits.ts): longer than
 * the route's own waits, a pooled connection (`POOL_WAIT_S`, 10 s) and then the row's lock
 * (`turns.LOCK_TIMEOUT_MS`, 10 s, backend api/crud_agent_turns.py), so the route gives up before
 * the agent does and a write never commits after the agent reported it failed. Change them
 * together.
 */
export const ROW_WRITE_TIMEOUT_MS = 30_000
const TIMEOUT_MS = ROW_WRITE_TIMEOUT_MS

/** `TurnRows` over the API's CRUD routes (area `session-turns`). */
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

  /** Session `id`'s turn path, or a throw: a session id is a UUID. */
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

  async giveBack(id: string, prior: SessionStatus, turnId: string): Promise<Logged | undefined> {
    const path = this.#path(id, '/give-back')
    const step = `a claim of session ${id} given back`
    let answer: { logged: WireLogged | null }
    try {
      answer = await this.#call<{ logged: WireLogged | null }>(step, path, { prior, turn_id: turnIdOf(turnId) })
    } catch (error) {
      // An API pod older than the fence (during a roll) refuses `turn_id` (422 naming that field,
      // its body forbids extra fields): give back unfenced, as that pod's route does, rather than
      // leave the row running until the sweep. Any other refusal keeps the fence and throws.
      if (!(error instanceof CrudReadError && error.fromRoute && error.status === 422 && error.fields.includes('body.turn_id'))) {
        throw error
      }
      answer = await this.#call<{ logged: WireLogged | null }>(step, path, { prior })
    }
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
