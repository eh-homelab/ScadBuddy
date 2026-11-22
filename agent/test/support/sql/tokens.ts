import { randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import type { Principal, Tier } from '../../../src/auth/principal.js'
import { hashToken, mintProblem, type MintRequest, newToken, principalFor, type TokenRecord, type TokenStore } from '../../../src/auth/tokens.js'

// Test-only: the token store over SQL, for the tests that run on a throwaway database. The
// agent itself goes through the API (src/auth/tokens.ts CrudTokenStore).

type TokenRow = {
  id: string
  name: string
  tier: Tier
  created_at: Date
  expires_at: Date | null
  revoked_at: Date | null
  last_used_at: Date | null
  approval_grant: boolean
}

function recordOf(row: TokenRow): TokenRecord {
  return {
    id: row.id,
    name: row.name,
    tier: row.tier,
    createdAt: row.created_at,
    expiresAt: row.expires_at ?? undefined,
    revokedAt: row.revoked_at ?? undefined,
    lastUsedAt: row.last_used_at ?? undefined,
    approvalGrant: row.approval_grant,
  }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/**
 * The token store on `ai_mcp_tokens`. Only `hashToken(token)` is written; the
 * plaintext leaves `mint` once and is never stored or logged.
 *
 * Every method is one statement, so replicas and concurrent requests need no
 * coordination beyond Postgres' own row locking. `verify` finds, checks and
 * stamps the row in a single `UPDATE … RETURNING`: a token revoked or expired
 * by the time the row lock is taken does not verify, and `last_used_at` only
 * moves forward (`GREATEST` ignores NULLs, per
 * https://www.postgresql.org/docs/17/functions-conditional.html#FUNCTIONS-GREATEST-LEAST).
 */
export class PostgresTokenStore implements TokenStore {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async verify(token: string, now: Date = new Date()): Promise<Principal | null> {
    const rows = await this.#sql<{ id: string; tier: Tier }[]>`
      UPDATE ai_mcp_tokens
         SET last_used_at = GREATEST(last_used_at, ${now})
       WHERE token_hash = ${hashToken(token)}
         AND revoked_at IS NULL
         AND (expires_at IS NULL OR expires_at > ${now})
      RETURNING id, tier`
    const row = rows[0]
    return row ? principalFor(row.id, row.tier) : null
  }

  async mint(request: MintRequest): Promise<{ token: string; record: TokenRecord }> {
    const problem = mintProblem(request)
    if (problem) throw new Error(problem)
    const token = newToken()
    const rows = await this.#sql<TokenRow[]>`
      INSERT INTO ai_mcp_tokens (id, name, tier, token_hash, expires_at, approval_grant)
      VALUES (${randomUUID()}, ${request.name}, ${request.tier}, ${hashToken(token)}, ${request.expiresAt ?? null},
              ${request.approvalGrant ?? false})
      RETURNING id, name, tier, created_at, expires_at, revoked_at, last_used_at, approval_grant`
    return { token, record: recordOf(rows[0]!) }
  }

  async revoke(id: string): Promise<boolean> {
    if (!UUID.test(id)) return false
    const rows = await this.#sql`
      UPDATE ai_mcp_tokens SET revoked_at = now() WHERE id = ${id} AND revoked_at IS NULL RETURNING id`
    return rows.length > 0
  }

  async list(): Promise<TokenRecord[]> {
    const rows = await this.#sql<TokenRow[]>`
      SELECT id, name, tier, created_at, expires_at, revoked_at, last_used_at, approval_grant
      FROM ai_mcp_tokens ORDER BY created_at, id`
    return rows.map(recordOf)
  }

  async approvalGrant(id: string, now: Date = new Date()): Promise<boolean> {
    if (!UUID.test(id)) return false
    const rows = await this.#sql`
      SELECT 1 FROM ai_mcp_tokens
       WHERE id = ${id} AND approval_grant AND tier = 'outward' AND revoked_at IS NULL
         AND (expires_at IS NULL OR expires_at > ${now})`
    return rows.length > 0
  }

  /** Expiry is judged by Postgres's clock, as escalation hops are (#3411). */
  async liveTier(id: string): Promise<Tier | null> {
    if (!UUID.test(id)) return null
    const rows = await this.#sql<{ tier: Tier }[]>`
      SELECT tier FROM ai_mcp_tokens
       WHERE id = ${id} AND revoked_at IS NULL
         AND (expires_at IS NULL OR expires_at > now())`
    return rows[0]?.tier ?? null
  }
}
