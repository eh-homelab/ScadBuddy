import type { MiddlewareHandler } from 'hono'
import type { MintRequest, TokenStore } from '../auth/tokens.js'
import type { RemoteAddress } from '../routes/guard.js'
import type { AuditActor, AuditContext, AuditKind, AuditOutcome, AuditSink } from './log.js'

// Auditing the writes that are not tool calls (#258): credential and plugin
// writes over HTTP (`auditWrites`, mounted in app.ts) and MCP token mint and
// revoke (`auditedTokenStore`, wired in main.ts). Settings writes audit
// themselves (credentials.ts SettingsStore.set); approval decisions in
// approvals/service.ts.

/** The browser user, as the UI's write routes treat a request that passes the origin gate (routes/guard.ts). */
export const UI_ACTOR: AuditActor = { kind: 'browser', id: 'browser', label: 'You' }

/**
 * A write the origin gate refused (403): it did not come from the UI, so it
 * is not recorded as the browser user. Its address is in `client_ip`.
 */
export const UNVERIFIED_ACTOR: AuditActor = { kind: 'anonymous', id: 'unverified', label: 'Refused request' }

function outcomeOf(status: number): AuditOutcome {
  if (status < 400) return 'ok'
  if (status === 403) return 'refused'
  return 'error'
}

/**
 * Records every request `verb` maps to an action (`undefined`: not a write,
 * not recorded) after the route has answered: the outcome from the status
 * (2xx/3xx ok, 403 refused by the origin gate, other errors error), the
 * target from the path, and the peer's address. Bodies are never read, so no
 * secret in a credential PUT can reach the log.
 */
export function auditWrites(options: {
  audit: AuditSink
  kind: AuditKind
  remoteAddress: RemoteAddress
  /** The action for a method and path, or undefined for a request that is not a write. */
  verb: (method: string, path: string) => string | undefined
  /**
   * Record only requests that did not succeed: for routes whose store records
   * its own successes with more detail (MCP tokens: `auditedTokenStore`).
   */
  failuresOnly?: boolean
}): MiddlewareHandler {
  return async (c, next) => {
    const action = options.verb(c.req.method, c.req.path)
    const startedAt = new Date()
    await next()
    if (action === undefined) return
    const status = c.res.status
    if (options.failuresOnly && status < 400) return
    await options.audit.record({
      kind: options.kind,
      action,
      surface: 'http',
      actor: status === 403 ? UNVERIFIED_ACTOR : UI_ACTOR,
      clientIp: options.remoteAddress(c),
      outcome: outcomeOf(status),
      detail: `${c.req.method} ${c.req.path} → ${status}`,
      startedAt,
      finishedAt: new Date(),
    })
  }
}

/**
 * `store` with mint and revoke recorded. Tokens are minted in Settings (spec
 * §8.1), so the actor is the browser user unless the caller says otherwise.
 * The plaintext token is never passed to the log: only the name, tier,
 * expiry and id.
 */
export function auditedTokenStore(store: TokenStore, audit: AuditSink, context: AuditContext = { actor: UI_ACTOR, surface: 'http' }): TokenStore {
  const base = { kind: 'token' as const, surface: context.surface, actor: context.actor, clientIp: context.clientIp }
  return {
    verify: (token, now) => store.verify(token, now),
    list: () => store.list(),
    async mint(request: MintRequest) {
      const startedAt = new Date()
      const describe = `"${request.name}" (${request.tier}${request.expiresAt ? `, expires ${request.expiresAt.toISOString()}` : ''})`
      try {
        const minted = await store.mint(request)
        await audit.record({
          ...base,
          action: 'mint',
          outcome: 'ok',
          detail: `token ${minted.record.id} ${describe}`,
          startedAt,
          finishedAt: new Date(),
        })
        return minted
      } catch (err) {
        await audit.record({ ...base, action: 'mint', outcome: 'error', detail: `token ${describe}: ${(err as Error).message}`, startedAt, finishedAt: new Date() })
        throw err
      }
    },
    async revoke(id: string) {
      const startedAt = new Date()
      let revoked: boolean
      try {
        revoked = await store.revoke(id)
      } catch (err) {
        await audit.record({ ...base, action: 'revoke', outcome: 'error', detail: `token ${id}: ${(err as Error).message}`, startedAt, finishedAt: new Date() })
        throw err
      }
      await audit.record({
        ...base,
        action: 'revoke',
        outcome: revoked ? 'ok' : 'error',
        detail: revoked ? `token ${id}` : `token ${id}: not a live token`,
        startedAt,
        finishedAt: new Date(),
      })
      return revoked
    },
  }
}
