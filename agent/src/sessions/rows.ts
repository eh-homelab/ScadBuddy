import type { EffortLevel } from '@anthropic-ai/claude-agent-sdk'
import { CrudReadError, crudRead, wellFormed } from '../api/crudRead.js'
import { isUuid } from '../harness/uuid.js'
import type { SessionRuntime } from './durable.js'
import type { Origin, Owner, SessionStatus } from './protocol.js'
import type { ResourceRef } from './touched.js'

// A session's row as the session manager reads and writes it (`ai_sessions`), behind one
// interface (#5299 step 9, part a): `CrudSessionRows`, over the API's CRUD routes (backend
// api/crud_agent_sessions.py, area `sessions`), each method one route and one transaction
// (#5299 D2). A durable turn's writes
// of the row with its events (a send's claim, a placement's, its spend, the turn's end) are
// turnRows.ts's (#6449); an edit's are sessions/edits.ts's.

/**
 * What a session's browser reports (#7027, `ai_sessions.browser`); a key left out is the
 * browser's default. The API checks the bounds (browserOptions.ts).
 */
export type SessionBrowser = { user_agent?: string; viewport?: { width: number; height: number }; locale?: string }

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
  /** Its browser options, or null for the browser's defaults (#7027). */
  browser: SessionBrowser | null
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
  /** `ai_sessions.browser` (#7027): left out, the parent's for a session with one, else none. */
  browser?: SessionBrowser
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

/** `SessionRows` over the API's CRUD routes (area `sessions`). */
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
        ...(row.browser ? { browser: row.browser } : {}),
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
