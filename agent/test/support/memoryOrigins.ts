import type { ApprovedScope } from '../../src/browser/origins.js'
import type { ApprovedOrigin, SessionOrigins } from '../../src/browser/store.js'

/**
 * The sessions' approved origins in memory, as the API keeps them in `ai_browser_session_origins`:
 * an approval needs a known session (`sessions`, id to title; `undefined` accepts every id), and
 * `once` never narrows `session`.
 */
export class MemorySessionOrigins implements SessionOrigins {
  readonly rows: ApprovedOrigin[] = []
  readonly sessions: Map<string, string> | undefined

  constructor(sessions?: Map<string, string>) {
    this.sessions = sessions
  }

  async approved(sessionId: string): Promise<Map<string, ApprovedScope>> {
    return new Map(this.rows.filter((r) => r.session_id === sessionId).map((r) => [r.origin, r.scope]))
  }

  async approve(sessionId: string, origin: string, scope: ApprovedScope): Promise<boolean> {
    if (this.sessions && !this.sessions.has(sessionId)) return false
    const at = this.rows.findIndex((r) => r.session_id === sessionId && r.origin === origin)
    const kept: ApprovedScope = at >= 0 && this.rows[at]!.scope === 'session' ? 'session' : scope
    if (at >= 0) this.rows.splice(at, 1)
    this.rows.push({ session_id: sessionId, title: this.sessions?.get(sessionId) ?? '', origin, scope: kept, approved_at: new Date().toISOString() })
    return true
  }

  async revoke(sessionId: string, origin: string): Promise<boolean> {
    const at = this.rows.findIndex((r) => r.session_id === sessionId && r.origin === origin)
    if (at < 0) return false
    this.rows.splice(at, 1)
    return true
  }

  async listApproved(sessionId?: string): Promise<ApprovedOrigin[]> {
    return this.rows.filter((r) => sessionId === undefined || r.session_id === sessionId).reverse()
  }
}
