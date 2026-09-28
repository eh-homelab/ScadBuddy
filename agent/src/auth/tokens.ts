import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { type Principal, type Tier, tiersUpTo } from './principal.js'

// Bearer tokens for `/mcp` (spec §8.3, issue #251 "Auth"): minted in Settings,
// each with a name, a tier and an optional expiry, shown once, stored hashed,
// revocable, with a last-used timestamp.
//
// TODO(#255, PR #354): a Postgres `TokenStore` over an `ai_mcp_tokens` table
// (id uuid, name, tier, token_hash unique, created_at, expires_at, revoked_at,
// last_used_at), appended as the next entry in #354's numbered migration list
// (agent/src/db/migrations.ts) once that merges. Until then production runs
// with `FailClosedTokenStore`, so `bearer` mode (the default) answers 401 to
// everyone; `InMemoryTokenStore` is for tests.

export type TokenRecord = {
  readonly id: string
  readonly name: string
  readonly tier: Tier
  readonly createdAt: Date
  readonly expiresAt: Date | undefined
  readonly revokedAt: Date | undefined
  readonly lastUsedAt: Date | undefined
}

export type MintRequest = { name: string; tier: Tier; expiresAt?: Date | undefined }

export interface TokenStore {
  /** The principal a presented token stands for, or null when it is unknown, expired or revoked. */
  verify(token: string, now?: Date): Promise<Principal | null>
  /** Creates a token. The plaintext is returned here once and never stored. */
  mint(request: MintRequest): Promise<{ token: string; record: TokenRecord }>
  /** True when a live token was revoked. */
  revoke(id: string): Promise<boolean>
  list(): Promise<TokenRecord[]>
}

/** Recognisable in logs and secret scanners; the rest is 256 random bits. */
export const TOKEN_PREFIX = 'sbmcp_'

export function hashToken(token: string): string {
  // A token is 256 bits of randomness, so a fast unsalted hash is enough: there
  // is nothing to brute-force that a slow KDF would protect.
  return createHash('sha256').update(token, 'utf8').digest('hex')
}

type Stored = { hash: string; record: TokenRecord }

export class InMemoryTokenStore implements TokenStore {
  readonly #byHash = new Map<string, Stored>()
  readonly #byId = new Map<string, Stored>()

  async verify(token: string, now: Date = new Date()): Promise<Principal | null> {
    const stored = this.#byHash.get(hashToken(token))
    if (!stored) return null
    const { record } = stored
    if (record.revokedAt) return null
    if (record.expiresAt && record.expiresAt.getTime() <= now.getTime()) return null
    stored.record = { ...record, lastUsedAt: now }
    return { id: `token:${record.id}`, kind: 'bearer', tiers: tiersUpTo(record.tier) }
  }

  async mint(request: MintRequest): Promise<{ token: string; record: TokenRecord }> {
    const token = TOKEN_PREFIX + randomBytes(32).toString('base64url')
    const record: TokenRecord = {
      id: randomUUID(),
      name: request.name,
      tier: request.tier,
      createdAt: new Date(),
      expiresAt: request.expiresAt,
      revokedAt: undefined,
      lastUsedAt: undefined,
    }
    const stored = { hash: hashToken(token), record }
    this.#byHash.set(stored.hash, stored)
    this.#byId.set(record.id, stored)
    return { token, record }
  }

  async revoke(id: string): Promise<boolean> {
    const stored = this.#byId.get(id)
    if (!stored || stored.record.revokedAt) return false
    stored.record = { ...stored.record, revokedAt: new Date() }
    return true
  }

  async list(): Promise<TokenRecord[]> {
    return [...this.#byId.values()].map((s) => s.record)
  }
}

/**
 * What production uses until the Postgres store exists (#255): no token
 * verifies, so `bearer` mode refuses every request rather than accepting
 * tokens that would vanish on restart and differ between replicas.
 */
export class FailClosedTokenStore implements TokenStore {
  async verify(): Promise<Principal | null> {
    return null
  }
  async mint(): Promise<never> {
    throw new Error('MCP bearer tokens need the database-backed token store (#255), which is not available yet')
  }
  async revoke(): Promise<boolean> {
    return false
  }
  async list(): Promise<TokenRecord[]> {
    return []
  }
}
