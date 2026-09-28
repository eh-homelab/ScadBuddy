import type { Owner } from '../sessions/protocol.js'

// Principals and tiers, spec §8.1
// (docs/superpowers/specs/2026-09-27-ai-integration-design.md). Every call —
// a tool, and later a resource read, a subscription or a session operation
// (#264, #300) — resolves to one of these through `authenticate`.

/**
 * `read` never changes anything; `write` is reversible through history;
 * `outward` sends, prints, deletes, or writes settings or credentials, and
 * always needs a human approval (spec §8.2).
 */
export const TIERS = ['read', 'write', 'outward'] as const
export type Tier = (typeof TIERS)[number]

/** The kinds of spec §8.1's table; the same set as a session `Owner`'s (sessions/protocol.ts). */
export type PrincipalKind = Owner['kind']

export type Principal = {
  /** Stable per principal: `token:<id>`, `anonymous:<mcp session id>`, or the browser user. */
  readonly id: string
  readonly kind: PrincipalKind
  readonly tiers: readonly Tier[]
  /** For the audit log (spec §8.3: `disabled` mode records the client IP). */
  readonly clientIp?: string | undefined
}

/** Every tier up to and including `max`: a `write` token may also read. */
export function tiersUpTo(max: Tier): Tier[] {
  return TIERS.slice(0, TIERS.indexOf(max) + 1)
}

export function hasTier(principal: Principal, tier: Tier): boolean {
  return principal.tiers.includes(tier)
}

/**
 * The principal a session's harness tools run as (spec §8.1: the browser
 * user, or a flow's declared permissions). The browser user holds every tier;
 * its outward calls still park for its own approval (harness/permissions.ts).
 * Any other owner gets `read` only: an `Owner` does not carry the tiers of
 * the token or flow behind it, so the `sessions.*` MCP tools (#251, #300) and
 * flows (#284, #297) have to pass those in before such a session may write.
 */
export function harnessPrincipal(owner: Owner): Principal {
  return { id: owner.id, kind: owner.kind, tiers: owner.kind === 'browser' ? [...TIERS] : ['read'] }
}
