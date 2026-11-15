import type { EffortLevel } from '@anthropic-ai/claude-agent-sdk'
import type { Sql } from 'postgres'
import { CrudReadError, crudRead, wellFormed } from '../api/crudRead.js'
import { isUuid } from '../harness/uuid.js'
import { payloadKeyLock } from '../temporal/payloadCodec.js'
import type { SessionRuntime } from './durable.js'
import type { Origin, Owner, SessionStatus } from './protocol.js'
import type { ResourceRef } from './touched.js'
import { POOL_BUDGET, POOL_COST } from './turnShared.js'

// A session's row as the session manager reads and writes it (`ai_sessions`), behind one
// interface (#5299 step 9, part a): `PgSessionRows`, the SQL the manager ran itself, and
// `CrudSessionRows`, the same over the API's CRUD routes (backend api/crud_agent_sessions.py,
// area `sessions`), each method one route and one transaction (#5299 D2). main.ts picks one per
// call with `gated` once the API lists the area, the SQL until then (D9). The turn's writes of
// the row that go with its event log (a send's claim, a turn's end, an edit) are not here: they
// move with the event log.

/** The row as `SessionRecord` is built from it (manager.ts `record`). */
export type SessionRow = {
  id: string
  origin: Origin
  owner_kind: Owner['kind']
  owner_id: string
  owner_label: string
  creator_kind: Owner['kind']
  creator_id: string
  offer_kind: Owner['kind'] | null
  offer_id: string | null
  offer_label: string | null
  offer_until: Date | null
  status: SessionStatus
  title: string
  tags: string[]
  scope: Record<string, unknown>
  parent_id: string | null
  runtime: SessionRuntime | null
  max_turns: number
  budget_usd: number
  cost_usd: number
  pool_cost_usd: number
  turns: number
  turn_active: boolean
  created_at: Date
  updated_at: Date
  archived_at: Date | null
}

/** The row with what a turn's claim reads (manager.ts `ClaimedSession`). */
export type TurnRow = SessionRow & {
  unpriced_cost_usd: number
  agent_model: string | null
  agent_effort: EffortLevel | null
  agent_type: string | null
  /** The column itself, not yet `AND NOT turn_flow`. */
  background_tools: boolean
  turn_flow: boolean
  turn_tiers: { tiers?: string[] } | null
  durable_turn_id: string | null
  /** When the running turn was claimed, at Postgres's microseconds (`CLAIMED_AT`). */
  claimed_at: string
}

export type ListFilter = {
  status?: SessionStatus
  origin?: Origin
  limit?: number
  /** Only sessions whose tool calls touched it (#931, touched.ts `ResourceRef`). */
  resource?: ResourceRef
  /**
   * Archived sessions (#1885): left out (`exclude`, the default, as the panel's list
   * leaves them out), the only ones listed (`only`, the archive view), or both (`include`).
   */
  archived?: 'exclude' | 'only' | 'include'
  /**
   * #4703 — only sessions whose title, or the path of a file in whose workspace, holds this
   * text, ignoring case (the workspace file picker's search). Matched before `limit`, so an
   * older match is found.
   */
  search?: string
  /**
   * #4703 — the file kinds a target takes, as a file input's `accept` checked by
   * routes/workspacePicker.ts (`.ext`, `type/subtype`, `type/*`, lowercase). It narrows
   * `search`'s path match and `withFile` to those files. Empty or left out: any file.
   */
  accept?: readonly string[]
  /** #4703 — only sessions whose workspace holds a file `accept` takes. Matched before `limit`. */
  withFile?: boolean
}

/** A principal as a condition: its kind and id. */
export type Who = Pick<Owner, 'kind' | 'id'>

/** A row's title at most (backend api/crud_agent_sessions.py `TITLE_MAX`): a chain of forks adds ` (fork)` each. */
export const ROW_TITLE_MAX = 4096

/** A new row (manager.ts `insert`). */
export type NewSessionRow = {
  id: string
  origin: Origin
  owner: Owner
  title: string
  tags: string[]
  scope: Record<string, unknown>
  parentId: string | null
  runtime: SessionRuntime | null
  maxTurns: number
  budgetUsd: number
  /** The session whose lineage's budget this one spends from (#823). */
  budgetOf?: string
  /** A fork that reads its parent's transcript through the cut (store.ts). */
  forkOf?: { parentId: string; cut: number }
  /** `ai_sessions.background_tools` (#5870): false given; else the parent's, for a session with one; else true. */
  backgroundTools?: false
  /** Counted under the owner's start lock: refused `rate_limited` at `max` in the window. */
  limit?: { max: number; windowMs: number }
}

/** Why an insert wrote nothing: the owner's limit, or a fork's parent forgotten. */
export type InsertRefusal = 'rate_limited' | 'forgotten'

export interface SessionRows {
  /** The row, or undefined (an id that is no UUID is no session). */
  get(id: string): Promise<TurnRow | undefined>
  /** `principal`'s sessions (every one for the browser user), filtered, newest first. */
  list(principal: Owner, filter: ListFilter): Promise<SessionRow[]>
  /** How many sessions `owner` started in the last `windowMs`. */
  startedRecently(owner: Who, windowMs: number): Promise<number>
  /** Inserts the row in one transaction; undefined, or why it was refused. */
  insert(row: NewSessionRow): Promise<InsertRefusal | undefined>
  /** A Workflow agent's chat's own model, effort and subagent. */
  setAgent(id: string, agent: { model: string | null; effort: string | null; type: string | null }): Promise<void>
  /** An offer to `to` while `owner` still owns the session; whether it applied. */
  offer(id: string, owner: Who, to: Owner, ttlMs: number): Promise<boolean>
  /** Ends the live offer to `to`; whether there was one. */
  cancelOffer(id: string, to: Who): Promise<boolean>
  /** `to` owns it while `owner` did (and, `accepting`, the live offer is to `to`); whether it applied. */
  transfer(id: string, owner: Who, to: Owner, accepting: boolean): Promise<boolean>
  /** Adds to the lineage's budget while `by` owns the session, up to `maxUsd`: the budget after, or undefined. */
  raiseBudget(id: string, by: Who, addUsd: number, maxUsd: number): Promise<number | undefined>
  /** A fork that failed before it logged anything goes whole; whether it went. */
  discard(id: string): Promise<boolean>
}

/** A pending handoff offer, read as none once it has expired (the columns stay until the next change clears them). */
const LIVE_OFFER = 'pending_owner_until > now()'

export const COLUMNS = `id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_kind END AS offer_kind,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_id END AS offer_id,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_label END AS offer_label,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_until END AS offer_until,
  status, title, tags, scope, parent_id, runtime, max_turns, ${POOL_BUDGET} AS budget_usd, cost_usd,
  ${POOL_COST} AS pool_cost_usd, turns,
  (runtime = 'unified' AND durable_turn_id IS NOT NULL) AS turn_active, created_at, updated_at, archived_at`

/**
 * When the running turn was claimed (`turn_started_at`, the send's claim) as `claimedAt`:
 * ISO 8601 in UTC at Postgres's microseconds, whatever the connection's `DateStyle` and
 * `TimeZone` (a plain `::text` follows them). Not `updated_at`, which every event and status
 * change moves: each placement of the turn reads it again (#5050). A row no turn was claimed
 * on since `turn_started_at` came gives its `updated_at`.
 */
export const CLAIMED_AT = `to_char(coalesce(turn_started_at, updated_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS claimed_at`

/** What `get` reads besides `COLUMNS`. */
const TURN_COLUMNS = `unpriced_cost_usd, agent_model, agent_effort, agent_type, background_tools, turn_flow, turn_tiers,
  durable_turn_id::text AS durable_turn_id, ${CLAIMED_AT}`

/** `text` as a literal part of an ILIKE pattern: its `%`, `_` and backslash match themselves. */
export function likeLiteral(text: string): string {
  return text.replace(/[\\%_]/g, (c) => `\\${c}`)
}

/**
 * The SQL behind `list()`, exported so a test can EXPLAIN it: the owner-or-
 * creator-or-offered filter is served by the ai_sessions_owner,
 * ai_sessions_creator (backend/scadbuddy/migrations_agent/20260928T0107Z_sessions.sql) and
 * ai_sessions_pending_owner (20260929T1825Z_session_handoff_offers.sql) indexes. The API's
 * `agent_data/sessions.py` `list_query` is the same query.
 */
export function listQuery(principal: Owner, filter: ListFilter = {}): { text: string; params: (string | number)[] } {
  const where: string[] = []
  const params: (string | number)[] = []
  if (principal.kind !== 'browser') {
    params.push(principal.kind, principal.id)
    where.push(
      '((owner_kind = $1 AND owner_id = $2) OR (creator_kind = $1 AND creator_id = $2)' +
        ` OR (pending_owner_kind = $1 AND pending_owner_id = $2 AND ${LIVE_OFFER}))`,
    )
  }
  if (filter.status) {
    params.push(filter.status)
    where.push(`status = $${params.length}`)
  }
  if (filter.origin) {
    params.push(filter.origin)
    where.push(`origin = $${params.length}`)
  }
  const archived = filter.archived ?? 'exclude'
  if (archived !== 'include') where.push(archived === 'only' ? 'archived_at IS NOT NULL' : 'archived_at IS NULL')
  if (filter.resource) {
    // Served by ai_session_resources_model / _resource (20261001T1824Z_session_resources.sql).
    let match: string
    if (filter.resource.type === 'model') {
      params.push(filter.resource.id)
      match = `r.model_slug = $${params.length}`
    } else {
      params.push(filter.resource.type, filter.resource.id)
      match = `r.resource_type = $${params.length - 1} AND r.resource_id = $${params.length}`
    }
    where.push(`EXISTS (SELECT 1 FROM ai_session_resources r WHERE r.session_id = ai_sessions.id AND ${match})`)
  }
  // A file of the session that `accept` takes: by its primary key's prefix (`workspace`,
  // ai_session_files), then its extension or media type, as workspacePicker.ts `accepts`.
  // Its parameters only when a filter below uses them: an unused one has no type to infer.
  const accepted: string[] = []
  for (const entry of filter.search || filter.withFile ? (filter.accept ?? []) : []) {
    if (entry.startsWith('.')) {
      params.push(`%${likeLiteral(entry)}`)
      accepted.push(`lower(f.path) LIKE $${params.length}`)
    } else if (entry.endsWith('/*')) {
      params.push(`${likeLiteral(entry.slice(0, -1))}%`)
      accepted.push(`lower(f.media_type) LIKE $${params.length}`)
    } else {
      params.push(entry)
      accepted.push(`lower(f.media_type) = $${params.length}`)
    }
  }
  const fileWhere = (extra: string) =>
    `EXISTS (SELECT 1 FROM ai_session_files f WHERE f.workspace = 'session:' || ai_sessions.id::text` +
    `${accepted.length ? ` AND (${accepted.join(' OR ')})` : ''}${extra})`
  if (filter.search) {
    params.push(`%${likeLiteral(filter.search)}%`)
    const n = params.length
    where.push(`(title ILIKE $${n} OR ${fileWhere(` AND f.path ILIKE $${n}`)})`)
  }
  if (filter.withFile) where.push(fileWhere(''))
  params.push(Math.min(Math.max(filter.limit ?? 100, 1), 500))
  const text = `SELECT ${COLUMNS} FROM ai_sessions ${where.length ? `WHERE ${where.join(' AND ')}` : ''}
       ORDER BY updated_at DESC LIMIT $${params.length}`
  return { text, params }
}

const recentCount = async (sql: Sql, owner: Who, windowMs: number): Promise<number> => {
  const [recent] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM ai_sessions
    WHERE owner_kind = ${owner.kind} AND owner_id = ${owner.id}
      AND created_at > now() - (${windowMs} * interval '1 millisecond')`
  return recent?.n ?? 0
}

/** The SQL the session manager ran itself (until the API lists area `sessions`). */
export class PgSessionRows implements SessionRows {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async get(id: string): Promise<TurnRow | undefined> {
    // Anything but a canonical UUID would make Postgres throw on the uuid column; it is simply
    // not a session.
    if (!isUuid(id)) return undefined
    const [row] = await this.#sql.unsafe<TurnRow[]>(`SELECT ${COLUMNS}, ${TURN_COLUMNS} FROM ai_sessions WHERE id = $1`, [id])
    return row
  }

  async list(principal: Owner, filter: ListFilter): Promise<SessionRow[]> {
    const { text, params } = listQuery(principal, filter)
    return this.#sql.unsafe<SessionRow[]>(text, params)
  }

  startedRecently(owner: Who, windowMs: number): Promise<number> {
    return recentCount(this.#sql, owner, windowMs)
  }

  async insert(row: NewSessionRow): Promise<InsertRefusal | undefined> {
    const write = async (sql: Sql) => {
      await sql`
        INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,
                                 status, title, tags, scope, parent_id, runtime, max_turns, budget_usd, budget_root_id,
                                 fork_parent_id, fork_cut, background_tools)
        VALUES (${row.id}, ${row.origin}, ${row.owner.kind}, ${row.owner.id}, ${row.owner.label},
                ${row.owner.kind}, ${row.owner.id}, 'idle', ${row.title}, ${sql.array(row.tags)},
                ${sql.json(row.scope as never)}, ${row.parentId}, ${row.runtime}, ${row.maxTurns}, ${row.budgetUsd},
                ${row.budgetOf ? sql`(SELECT coalesce(budget_root_id, id) FROM ai_sessions WHERE id = ${row.budgetOf})` : null},
                ${row.forkOf?.parentId ?? null}, ${row.forkOf?.cut ?? null},
                ${
                  row.backgroundTools === false
                    ? false
                    : row.parentId
                      ? sql`coalesce((SELECT background_tools FROM ai_sessions WHERE id = ${row.parentId}), true)`
                      : true
                })`
    }
    const { limit, forkOf } = row
    if (!limit && !forkOf) {
      await write(this.#sql)
      return undefined
    }
    return this.#sql.begin(async (tx) => {
      if (limit) {
        // Per owner, held until commit, so concurrent starts count each other.
        await tx`SELECT pg_advisory_xact_lock(hashtextextended(${`ai_sessions.start:${row.owner.kind}:${row.owner.id}`}, 0))`
        if ((await recentCount(tx as unknown as Sql, row.owner, limit.windowMs)) >= limit.max) return 'rate_limited' as const
      }
      if (forkOf) {
        // Under the parent's lock, shared, as an append is (store.ts appendNow): a forget of the
        // parent either went first, and its tombstone refuses the fork here, or waits for this to
        // commit and then finds the fork to copy its range into (copyForkRanges).
        const subject = `session-${forkOf.parentId}`
        await tx`SELECT pg_advisory_xact_lock_shared(hashtext(${payloadKeyLock(subject)}))`
        const [forgotten] = await tx`SELECT 1 FROM ai_forgotten_subjects WHERE subject = ${subject}`
        if (forgotten) return 'forgotten' as const
      }
      await write(tx as unknown as Sql)
      return undefined
    })
  }

  async setAgent(id: string, agent: { model: string | null; effort: string | null; type: string | null }): Promise<void> {
    await this.#sql`
      UPDATE ai_sessions SET agent_model = ${agent.model}, agent_effort = ${agent.effort}, agent_type = ${agent.type}
       WHERE id = ${id}`
  }

  async offer(id: string, owner: Who, to: Owner, ttlMs: number): Promise<boolean> {
    const rows = await this.#sql`
      UPDATE ai_sessions
      SET pending_owner_kind = ${to.kind}, pending_owner_id = ${to.id}, pending_owner_label = ${to.label},
          pending_owner_until = now() + (${ttlMs} * interval '1 millisecond'), updated_at = now()
      WHERE id = ${id} AND owner_kind = ${owner.kind} AND owner_id = ${owner.id}`
    return rows.count > 0
  }

  async cancelOffer(id: string, to: Who): Promise<boolean> {
    const rows = await this.#sql`
      UPDATE ai_sessions
      SET pending_owner_kind = NULL, pending_owner_id = NULL, pending_owner_label = NULL,
          pending_owner_until = NULL, updated_at = now()
      WHERE id = ${id} AND pending_owner_kind = ${to.kind} AND pending_owner_id = ${to.id} AND pending_owner_until > now()`
    return rows.count > 0
  }

  async transfer(id: string, owner: Who, to: Owner, accepting: boolean): Promise<boolean> {
    const offerStill = accepting
      ? this.#sql`AND pending_owner_kind = ${to.kind} AND pending_owner_id = ${to.id} AND pending_owner_until > now()`
      : this.#sql``
    const rows = await this.#sql`
      UPDATE ai_sessions
      SET owner_kind = ${to.kind}, owner_id = ${to.id}, owner_label = ${to.label},
          pending_owner_kind = NULL, pending_owner_id = NULL, pending_owner_label = NULL,
          pending_owner_until = NULL, updated_at = now()
      WHERE id = ${id} AND owner_kind = ${owner.kind} AND owner_id = ${owner.id} ${offerStill}`
    return rows.count > 0
  }

  async raiseBudget(id: string, by: Who, addUsd: number, maxUsd: number): Promise<number | undefined> {
    // One statement on the lineage's root, so two raises add up and the owner cannot change
    // in between; the owner checked is this session's, which need not be the root's.
    const [hit] = await this.#sql.unsafe<{ budget_usd: number }[]>(
      `UPDATE ai_sessions r SET budget_usd = round((r.budget_usd + $2)::numeric, 2)::double precision, updated_at = now()
         FROM ai_sessions s
        WHERE s.id = $1 AND s.owner_kind = $3 AND (s.owner_id = $4 OR s.owner_kind = 'browser') AND r.id = coalesce(s.budget_root_id, s.id)
          AND round((r.budget_usd + $2)::numeric, 2) <= $5
       RETURNING r.budget_usd`,
      [id, addUsd, by.kind, by.id, maxUsd],
    )
    return hit?.budget_usd
  }

  async discard(id: string): Promise<boolean> {
    const subject = `session-${id}`
    return this.#sql.begin(async (tx) => {
      // The lock a key's creation takes (payloadCodec.ts), held over the row's delete too: a
      // creation that saw the row standing commits its key before this deletes it, and one
      // after this commit sees no row and makes none.
      await tx`SELECT pg_advisory_xact_lock(hashtext(${payloadKeyLock(subject)}))`
      const [row] = await tx`SELECT 1 FROM ai_sessions WHERE id = ${id} AND event_seq = 0 FOR UPDATE`
      if (!row) return false
      await tx`DELETE FROM ai_session_entries WHERE session_id = ${id}`
      await tx`DELETE FROM ai_sessions WHERE id = ${id}`
      await tx`DELETE FROM ai_payload_keys WHERE subject = ${subject}`
      return true
    })
  }
}

// -- over the CRUD routes ----------------------------------------------------------------------

/** A row as the routes answer it: its timestamps as text. */
type WireRow = Omit<SessionRow, 'offer_until' | 'created_at' | 'updated_at' | 'archived_at'> & {
  offer_until: string | null
  created_at: string
  updated_at: string
  archived_at: string | null
}
type WireTurnRow = WireRow & Omit<TurnRow, keyof SessionRow>

const date = (text: string): Date => new Date(text)

function fromWire<T extends WireRow>(row: T): Omit<T, 'offer_until' | 'created_at' | 'updated_at' | 'archived_at'> & SessionRow {
  return {
    ...row,
    offer_until: row.offer_until === null ? null : date(row.offer_until),
    created_at: date(row.created_at),
    updated_at: date(row.updated_at),
    archived_at: row.archived_at === null ? null : date(row.archived_at),
    pool_cost_usd: row.pool_cost_usd ?? 0,
  }
}

/**
 * A session's path segment: a UUID, lowercase as Postgres writes one (an uppercase id names the
 * same row there), encoded. Undefined for anything else, which is no session.
 */
export function sessionPath(id: string): string | undefined {
  return isUuid(id) ? `/sessions/${encodeURIComponent(id.toLowerCase())}` : undefined
}

/** `PgSessionRows` over the API's CRUD routes (area `sessions`). */
export class CrudSessionRows implements SessionRows {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  #call<T>(step: string, path: string, body?: unknown): Promise<T> {
    // Well-formed: a title cut between a pair's halves holds a lone surrogate, which the API refuses (crudRead.ts).
    return crudRead<T>(this.#fetch, this.#base, step, path, body === undefined ? {} : { method: 'POST', body: wellFormed(body) })
  }

  /** The path of session `id`, or a throw: a write names a session the caller read. */
  #path(id: string, tail = ''): string {
    const path = sessionPath(id)
    if (path === undefined) throw new Error(`session ${JSON.stringify(id)} is not a session id`)
    return `${path}${tail}`
  }

  async get(id: string): Promise<TurnRow | undefined> {
    const path = sessionPath(id)
    if (path === undefined) return undefined
    const { session } = await this.#call<{ session: WireTurnRow | null }>(`session ${id}`, path)
    return session === null ? undefined : (fromWire(session) as TurnRow)
  }

  async list(principal: Owner, filter: ListFilter): Promise<SessionRow[]> {
    const { sessions } = await this.#call<{ sessions: WireRow[] }>('the sessions list', '/sessions/list', {
      principal: principal.kind === 'browser' ? null : { kind: principal.kind, id: principal.id },
      ...(filter.status ? { status: filter.status } : {}),
      ...(filter.origin ? { origin: filter.origin } : {}),
      archived: filter.archived ?? 'exclude',
      ...(filter.resource ? { resource: filter.resource } : {}),
      ...(filter.search ? { search: filter.search } : {}),
      accept: [...(filter.accept ?? [])],
      with_file: filter.withFile === true,
      limit: Math.min(Math.max(filter.limit ?? 100, 1), 500),
    })
    return sessions.map((row) => fromWire(row))
  }

  async startedRecently(owner: Who, windowMs: number): Promise<number> {
    const query = new URLSearchParams({ owner_kind: owner.kind, owner_id: owner.id, window_ms: String(windowMs) })
    return (await this.#call<{ count: number }>('the sessions started lately', `/session-starts?${query.toString()}`)).count
  }

  async insert(row: NewSessionRow): Promise<InsertRefusal | undefined> {
    try {
      await this.#call('a new session', '/sessions', {
        id: row.id,
        origin: row.origin,
        owner: { kind: row.owner.kind, id: row.owner.id, label: row.owner.label },
        title: row.title,
        tags: row.tags,
        scope: row.scope,
        parent_id: row.parentId,
        runtime: row.runtime,
        max_turns: row.maxTurns,
        budget_usd: row.budgetUsd,
        ...(row.budgetOf ? { budget_of: row.budgetOf } : {}),
        ...(row.forkOf ? { fork_of: { parent_id: row.forkOf.parentId, cut: row.forkOf.cut } } : {}),
        ...(row.backgroundTools === false ? { background_tools: false } : {}),
        ...(row.limit ? { limit: { max: row.limit.max, window_ms: row.limit.windowMs } } : {}),
      })
      return undefined
    } catch (error) {
      if (error instanceof CrudReadError && error.fromRoute) {
        if (error.status === 429 && error.reason === 'rate_limited') return 'rate_limited'
        if (error.status === 404 && error.reason === 'forgotten') return 'forgotten'
      }
      throw error
    }
  }

  async setAgent(id: string, agent: { model: string | null; effort: string | null; type: string | null }): Promise<void> {
    await this.#call(`the agent of session ${id}`, this.#path(id, '/agent'), agent)
  }

  async offer(id: string, owner: Who, to: Owner, ttlMs: number): Promise<boolean> {
    const body = { owner: { kind: owner.kind, id: owner.id }, to: { kind: to.kind, id: to.id, label: to.label }, ttl_ms: ttlMs }
    return (await this.#call<{ changed: boolean }>(`an offer of session ${id}`, this.#path(id, '/offer'), body)).changed
  }

  async cancelOffer(id: string, to: Who): Promise<boolean> {
    const body = { to: { kind: to.kind, id: to.id } }
    return (await this.#call<{ changed: boolean }>(`the offer of session ${id}`, this.#path(id, '/offer/cancel'), body)).changed
  }

  async transfer(id: string, owner: Who, to: Owner, accepting: boolean): Promise<boolean> {
    const body = { owner: { kind: owner.kind, id: owner.id }, to: { kind: to.kind, id: to.id, label: to.label }, accepting }
    return (await this.#call<{ changed: boolean }>(`the owner of session ${id}`, this.#path(id, '/owner'), body)).changed
  }

  async raiseBudget(id: string, by: Who, addUsd: number, maxUsd: number): Promise<number | undefined> {
    const body = { by: { kind: by.kind, id: by.id }, add_usd: addUsd, max_usd: maxUsd }
    const { budget_usd: after } = await this.#call<{ budget_usd: number | null }>(`the budget of session ${id}`, this.#path(id, '/budget'), body)
    return after ?? undefined
  }

  async discard(id: string): Promise<boolean> {
    return (await this.#call<{ discarded: boolean }>(`a failed fork ${id}`, this.#path(id, '/discard'), {})).discarded
  }
}
