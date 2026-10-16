import type { Sql } from 'postgres'
import type { AuditContext } from '../audit/log.js'
import { isUuid } from '../harness/stateDirs.js'
import { type ApprovedScope, type BrowserLists, checkRules, listsOf, SETTING_BROWSER_ORIGINS } from './origins.js'

// The headless browser's lists and approvals (#982): the allow and deny lists in
// `ai_settings` (`browser_origins`), written by the user only (routes/browserOrigins.ts),
// and each session's approved origins in `ai_browser_session_origins`.

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

export class BrowserOrigins {
  private readonly sql: Sql
  private readonly settings: SettingsAccess | undefined

  /** Without settings (tests), the lists are the defaults and cannot be changed. */
  constructor(sql: Sql, settings: SettingsAccess | undefined) {
    this.sql = sql
    this.settings = settings
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
    const rows = await this.sql`
      DELETE FROM ai_browser_session_origins WHERE session_id = ${sessionId} AND origin = ${origin} RETURNING origin`
    return rows.length > 0
  }

  /** Every session's approved origins, newest first, for Settings. */
  async listApproved(sessionId?: string): Promise<ApprovedOrigin[]> {
    if (sessionId !== undefined && !isUuid(sessionId)) return []
    const rows = await this.sql<{ session_id: string; title: string; origin: string; scope: ApprovedScope; approved_at: Date }[]>`
      SELECT o.session_id::text, s.title, o.origin, o.scope, o.approved_at
      FROM ai_browser_session_origins o JOIN ai_sessions s ON s.id = o.session_id
      WHERE ${sessionId === undefined ? this.sql`true` : this.sql`o.session_id = ${sessionId}`}
      ORDER BY o.approved_at DESC, o.origin
      LIMIT ${APPROVED_LISTED}`
    return rows.map((r) => ({ ...r, approved_at: r.approved_at.toISOString() }))
  }
}
