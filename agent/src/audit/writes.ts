import type { MiddlewareHandler } from 'hono'
import type { MintRequest, TokenStore } from '../auth/tokens.js'
import type { RemoteAddress } from '../routes/guard.js'
import type { AuditActor, AuditContext, AuditEntry, AuditKind, AuditOutcome, AuditSink } from './log.js'

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

/** How long refused writes from one peer to one action are coalesced into one row. */
export const REFUSAL_WINDOW_MS = 60_000
/** Peers tracked one by one per window; beyond it, every other peer shares one row per action. */
export const REFUSAL_MAX_PEERS = 64

/**
 * Refused (403) writes, coalesced so an unauthenticated peer cannot grow
 * `ai_audit` at its request rate (#258): the first refusal per peer and
 * action in a window is recorded at once; the rest are counted, and one more
 * row with the count is written when the window closes. At most
 * REFUSAL_MAX_PEERS peers are tracked apart; more share one `*` key, so a
 * flood from many addresses still costs a bounded number of rows a minute.
 */
export class RefusalCoalescer {
  private readonly windows = new Map<string, { entry: AuditEntry; more: number; shared: boolean }>()

  private readonly audit: AuditSink
  private readonly windowMs: number
  private readonly maxPeers: number

  constructor(audit: AuditSink, windowMs: number = REFUSAL_WINDOW_MS, maxPeers: number = REFUSAL_MAX_PEERS) {
    this.audit = audit
    this.windowMs = windowMs
    this.maxPeers = maxPeers
  }

  async record(entry: AuditEntry): Promise<void> {
    const scope = `${entry.kind}|${entry.action}|`
    let key = scope + (entry.clientIp ?? '')
    let shared = false
    if (!this.windows.has(key) && this.windows.size >= this.maxPeers) {
      key = `${scope}*`
      shared = true
    }
    const open = this.windows.get(key)
    if (open) {
      open.more += 1
      return
    }
    this.windows.set(key, { entry, more: 0, shared })
    setTimeout(() => void this.close(key), this.windowMs).unref()
    await this.audit.record(shared ? { ...entry, clientIp: undefined, detail: `${entry.detail ?? ''} (one of many peers)` } : entry)
  }

  private async close(key: string): Promise<void> {
    const open = this.windows.get(key)
    this.windows.delete(key)
    if (!open || open.more === 0) return
    const { entry, more, shared } = open
    await this.audit.record({
      ...entry,
      ...(shared ? { clientIp: undefined } : {}),
      detail: `${more} more refused ${entry.action} request${more === 1 ? '' : 's'} ${shared ? 'from other peers' : 'from this peer'} within ${Math.round(this.windowMs / 1000)}s of the first, coalesced`,
      startedAt: entry.startedAt,
      finishedAt: new Date(),
    })
  }
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
  /** Where refused (403) writes go, coalesced; shared by every mount so the bound is global. */
  refusals: RefusalCoalescer
}): MiddlewareHandler {
  return async (c, next) => {
    const action = options.verb(c.req.method, c.req.path)
    const startedAt = new Date()
    await next()
    if (action === undefined) return
    const status = c.res.status
    if (options.failuresOnly && status < 400) return
    const entry: AuditEntry = {
      kind: options.kind,
      action,
      surface: 'http',
      actor: status === 403 ? UNVERIFIED_ACTOR : UI_ACTOR,
      clientIp: options.remoteAddress(c),
      outcome: outcomeOf(status),
      detail: `${c.req.method} ${c.req.path} → ${status}`,
      startedAt,
      finishedAt: new Date(),
    }
    await (status === 403 ? options.refusals.record(entry) : options.audit.record(entry))
  }
}

/**
 * `store` with successful mints and revokes recorded, with the token's id.
 * Tokens are minted in Settings (spec §8.1), so the actor is the browser user
 * unless the caller says otherwise. The plaintext token is never passed to
 * the log: only the name, tier, expiry and id. A mint or revoke that fails
 * (throws, or finds no live token) is not recorded here: the route answers
 * an error status, and app.ts's `auditWrites` records that once, with the
 * request's client_ip.
 */
export function auditedTokenStore(store: TokenStore, audit: AuditSink, context: AuditContext = { actor: UI_ACTOR, surface: 'http' }): TokenStore {
  const base = { kind: 'token' as const, surface: context.surface, actor: context.actor, clientIp: context.clientIp }
  return {
    verify: (token, now) => store.verify(token, now),
    list: () => store.list(),
    approvalGrant: (id, now) => store.approvalGrant(id, now),
    liveTier: (id, now) => store.liveTier(id, now),
    async mint(request: MintRequest) {
      const startedAt = new Date()
      const describe =
        `"${request.name}" (${request.tier}${request.approvalGrant ? ', approval grant' : ''}` +
        `${request.expiresAt ? `, expires ${request.expiresAt.toISOString()}` : ''})`
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
    },
    async revoke(id: string) {
      const startedAt = new Date()
      const revoked = await store.revoke(id)
      if (revoked) await audit.record({ ...base, action: 'revoke', outcome: 'ok', detail: `token ${id}`, startedAt, finishedAt: new Date() })
      return revoked
    },
  }
}
