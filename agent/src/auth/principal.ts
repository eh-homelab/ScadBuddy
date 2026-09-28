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

export type PrincipalKind = 'bearer' | 'oidc' | 'anonymous' | 'browser'

export type Principal = {
  /**
   * Stable per principal: `token:<id>`, `oidc:<issuer>#<sub>` (the IdP's subject, #262),
   * `anonymous:<mcp session id>`, or the browser user.
   */
  readonly id: string
  readonly kind: PrincipalKind
  readonly tiers: readonly Tier[]
  /** For the audit log (spec §8.3: `disabled` mode records the client IP). */
  readonly clientIp?: string | undefined
  /** `oidc` principals: the token's `sub`, which the audit log records in place of a token name (#262). */
  readonly subject?: string | undefined
  /** `oidc` principals: the OAuth client the token was issued to (`azp` or `client_id`), when the token says. */
  readonly clientId?: string | undefined
}

/** Every tier up to and including `max`: a `write` token may also read. */
export function tiersUpTo(max: Tier): Tier[] {
  return TIERS.slice(0, TIERS.indexOf(max) + 1)
}

export function hasTier(principal: Principal, tier: Tier): boolean {
  return principal.tiers.includes(tier)
}
