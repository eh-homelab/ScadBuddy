import { randomUUID } from 'node:crypto'
import type { Principal } from '../../src/auth/principal.js'
import {
  hashToken,
  type MintRequest,
  newToken,
  principalFor,
  type TokenRecord,
  type TokenStore,
} from '../../src/auth/tokens.js'

type Stored = { hash: string; record: TokenRecord }

/**
 * PostgresTokenStore's behaviour without Postgres, for the /mcp tests: the
 * same token format, hash and principal, kept in memory. Test-only; the
 * service stores tokens in `ai_mcp_tokens` (covered by test/tokens.pg.test.ts).
 */
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
    return principalFor(record.id, record.tier)
  }

  async mint(request: MintRequest): Promise<{ token: string; record: TokenRecord }> {
    const token = newToken()
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
