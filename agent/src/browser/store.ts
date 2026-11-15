import type { Sql } from 'postgres'
import { type CrudTarget, crudOf, qs } from '../api/crudStores.js'
import type { AuditContext } from '../audit/log.js'
import { isUuid } from '../harness/stateDirs.js'
import { type ApprovedScope, type BrowserLists, checkRules, listsOf, SETTING_BROWSER_ORIGINS } from './origins.js'

// The headless browser's lists and approvals (#982): the allow and deny lists in
// `ai_settings` (`browser_origins`), written by the user only (routes/browserOrigins.ts),
// and each session's approved origins in `ai_browser_session_origins`: over SQL, or through
// the API's CRUD routes once it lists area `browser-origins` (#5299 step 5, `CrudSessionOrigins`).

/** `ai_settings` (credentials.ts SettingsStore); a turn's reader has no `set`. */
export type SettingsAccess = {
  get<T>(key: string): Promise<T | undefined>
  set?(key: string, value: unknown, context: AuditContext): Promise<void>
}

export type ApprovedOrigin = {
  session_id: string
  /** The session's title, for Settings' list. */
  title: string
  origin: string
  scope: ApprovedScope
  approved_at: string
}

/** How many approved origins Settings lists, newest first. */
export const APPROVED_LISTED = 200

/** A session's approved origins, as `BrowserOrigins` reads and writes them. */
export type SessionOrigins = Pick<BrowserOrigins, 'approved' | 'approve' | 'revoke' | 'listApproved'>

/** Where `BrowserOrigins` keeps the approvals while `use()` holds: the API's CRUD routes. */
export type RemoteOrigins = { crud: SessionOrigins; use: () => boolean }

export class BrowserOrigins {
  private readonly sql: Sql
  private readonly settings: SettingsAccess | undefined
  private readonly remote: RemoteOrigins | undefined

  /** Without settings (tests), the lists are the defaults and cannot be changed. */
  constructor(sql: Sql, settings: SettingsAccess | undefined, remote?: RemoteOrigins) {
    this.sql = sql
    this.settings = settings
    this.remote = remote
  }

  private crud(): SessionOrigins | undefined {
    return this.remote?.use() ? this.remote.crud : undefined
  }

  async lists(): Promise<BrowserLists> {
    return listsOf(await this.settings?.get<unknown>(SETTING_BROWSER_ORIGINS))
  }

  /** Checks and stores both lists (RuleError when a rule is not one); returns what was stored. */
  async setLists(lists: BrowserLists, context: AuditContext): Promise<BrowserLists> {
    const checked = { allow: checkRules(lists.allow, 'allow'), deny: checkRules(lists.deny, 'deny') }
    if (!this.settings?.set) throw new Error('the browser lists cannot be changed without the settings store')
    await this.settings.set(SETTING_BROWSER_ORIGINS, checked, context)
    return checked
  }

  /** Adds one rule to the allow list ("always", from an approval card); no change when it is there. */
  async allow(rule: string, context: AuditContext): Promise<BrowserLists> {
    const lists = await this.lists()
    const [checked] = checkRules([rule], 'allow')
    if (lists.allow.includes(checked!)) return lists
    return this.setLists({ allow: [...lists.allow, checked!], deny: lists.deny }, context)
  }

  /** The session's approved origins. */
  async approved(sessionId: string): Promise<Map<string, ApprovedScope>> {
    if (!isUuid(sessionId)) return new Map()
    const crud = this.crud()
    if (crud) return crud.approved(sessionId)
    const rows = await this.sql<{ origin: string; scope: ApprovedScope }[]>`
      SELECT origin, scope FROM ai_browser_session_origins WHERE session_id = ${sessionId}`
    return new Map(rows.map((r) => [r.origin, r.scope]))
  }

  /**
   * Records an approval; `once` never narrows a `session` one. False when there is no
   * such session.
   */
  async approve(sessionId: string, origin: string, scope: ApprovedScope): Promise<boolean> {
    if (!isUuid(sessionId)) return false
    const crud = this.crud()
    if (crud) return crud.approve(sessionId, origin, scope)
    const rows = await this.sql`
      INSERT INTO ai_browser_session_origins (session_id, origin, scope)
      SELECT id, ${origin}, ${scope} FROM ai_sessions WHERE id = ${sessionId}
      ON CONFLICT (session_id, origin) DO UPDATE
        SET scope = CASE WHEN ai_browser_session_origins.scope = 'session' THEN 'session' ELSE EXCLUDED.scope END,
            approved_at = now()
      RETURNING origin`
    return rows.length > 0
  }

  /** Withdraws an approval; false when there was none. */
  async revoke(sessionId: string, origin: string): Promise<boolean> {
    if (!isUuid(sessionId)) return false
    const crud = this.crud()
    if (crud) return crud.revoke(sessionId, origin)
    const rows = await this.sql`
      DELETE FROM ai_browser_session_origins WHERE session_id = ${sessionId} AND origin = ${origin} RETURNING origin`
    return rows.length > 0
  }

  /** Every session's approved origins, newest first, for Settings. */
  async listApproved(sessionId?: string): Promise<ApprovedOrigin[]> {
    if (sessionId !== undefined && !isUuid(sessionId)) return []
    const crud = this.crud()
    if (crud) return crud.listApproved(sessionId)
    const rows = await this.sql<{ session_id: string; title: string; origin: string; scope: ApprovedScope; approved_at: Date }[]>`
      SELECT o.session_id::text, s.title, o.origin, o.scope, o.approved_at
      FROM ai_browser_session_origins o JOIN ai_sessions s ON s.id = o.session_id
      WHERE ${sessionId === undefined ? this.sql`true` : this.sql`o.session_id = ${sessionId}`}
      ORDER BY o.approved_at DESC, o.origin
      LIMIT ${APPROVED_LISTED}`
    return rows.map((r) => ({ ...r, approved_at: r.approved_at.toISOString() }))
  }
}

/** The approvals through the API's CRUD routes, area `browser-origins` (backend api/crud_agent_stores.py). */
export class CrudSessionOrigins implements SessionOrigins {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async approved(sessionId: string): Promise<Map<string, ApprovedScope>> {
    const { origins } = await this.#call<{ origins: { origin: string; scope: ApprovedScope }[] }>(
      "a session's approved origins",
      `/browser-origins${qs({ session_id: sessionId })}`,
    )
    return new Map(origins.map((r) => [r.origin, r.scope]))
  }

  async approve(sessionId: string, origin: string, scope: ApprovedScope): Promise<boolean> {
    const body = { session_id: sessionId, origin, scope }
    return (await this.#call<{ done: boolean }>('an origin approved', '/browser-origins/approve', { body })).done
  }

  async revoke(sessionId: string, origin: string): Promise<boolean> {
    const body = { session_id: sessionId, origin }
    return (await this.#call<{ done: boolean }>('an origin withdrawn', '/browser-origins/revoke', { body })).done
  }

  async listApproved(sessionId?: string): Promise<ApprovedOrigin[]> {
    const path = `/browser-origins/approved${sessionId === undefined ? '' : qs({ session_id: sessionId })}`
    const { approved } = await this.#call<{ approved: ApprovedOrigin[] }>('the approved origins', path)
    // As the SQL answers it: the time to the millisecond.
    return approved.map((r) => ({ ...r, approved_at: new Date(r.approved_at).toISOString() }))
  }
}
