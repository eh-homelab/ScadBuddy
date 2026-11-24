import type { Sql } from 'postgres'
import type { InsertRefusal, ListFilter, NewSessionRow, SessionRow, SessionRows, TurnRow, Who } from '../../../src/sessions/rows.js'
import { isUuid } from '../../../src/harness/uuid.js'
import { payloadKeyLock } from './payloadKeys.js'
import type { Owner } from '../../../src/sessions/protocol.js'

// `SessionRows` straight on the test database, the statements the API's routes run (backend
// agent_data/sessions.py), for tests with no API (#5299).

/** The session whose budget this one spends from: itself, or a fork's lineage root (#823). */
const ROOT = 'coalesce(ai_sessions.budget_root_id, ai_sessions.id)'

/** The budget a session spends from: its root's (20261006T0100Z_session_budget_root.sql). */
export const POOL_BUDGET = `coalesce((SELECT r.budget_usd FROM ai_sessions r WHERE r.id = ${ROOT}), ai_sessions.budget_usd)`

/** What every session spending from that budget has spent. Read at the statement's snapshot. */
export const POOL_COST = `(SELECT sum(m.cost_usd) FROM ai_sessions m WHERE coalesce(m.budget_root_id, m.id) = ${ROOT})`

/** A pending handoff offer, read as none once it has expired (the columns stay until the next change clears them). */
const LIVE_OFFER = 'pending_owner_until > now()'

export const COLUMNS = `id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_kind END AS offer_kind,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_id END AS offer_id,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_label END AS offer_label,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_until END AS offer_until,
  status, title, tags, scope, browser, parent_id, runtime, max_turns, ${POOL_BUDGET} AS budget_usd, cost_usd,
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
                                 fork_parent_id, fork_cut, background_tools, browser)
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
                },
                ${
                  row.browser && Object.keys(row.browser).length
                    ? sql.json(row.browser as never)
                    : row.parentId
                      ? sql`(SELECT browser FROM ai_sessions WHERE id = ${row.parentId})`
                      : null
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
