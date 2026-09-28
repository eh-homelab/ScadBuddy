import { createHash, randomBytes, randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { type Principal, type Tier, tiersUpTo } from './principal.js'

// Bearer tokens for `/mcp` (spec §8.3, issue #251 "Auth"): minted in Settings,
// each with a name, a tier and an optional expiry, shown once, stored hashed,
// revocable, with a last-used timestamp.
//
// Persistence is Postgres only (spec §9: "All AI state lives in the #241
// database, in `ai_*` tables"): `PostgresTokenStore` over `ai_mcp_tokens`
// (db/migrations/20260928T0734Z_mcp_tokens.sql). There is no file or in-memory
// fallback. With no database configured, main.ts wires `FailClosedTokenStore`
// and /mcp answers 503 "AI disabled: no database" before any token is looked
// at (app.ts); tests use test/support/memoryTokens.ts.

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

type TokenRow = {
  id: string
  name: string
  tier: Tier
  created_at: Date
  expires_at: Date | null
  revoked_at: Date | null
  last_used_at: Date | null
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
  }
}

export function principalFor(id: string, tier: Tier): Principal {
  return { id: `token:${id}`, kind: 'bearer', tiers: tiersUpTo(tier) }
}

/** A fresh token: the prefix plus 32 bytes from the OS CSPRNG (`crypto.randomBytes`), base64url. */
export function newToken(): string {
  return TOKEN_PREFIX + randomBytes(32).toString('base64url')
}

// A token id is a uuid column; anything else cannot match a row, and would make
// Postgres raise `invalid input syntax for type uuid` instead of answering false.
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
    const token = newToken()
    const rows = await this.#sql<TokenRow[]>`
      INSERT INTO ai_mcp_tokens (id, name, tier, token_hash, expires_at)
      VALUES (${randomUUID()}, ${request.name}, ${request.tier}, ${hashToken(token)}, ${request.expiresAt ?? null})
      RETURNING id, name, tier, created_at, expires_at, revoked_at, last_used_at`
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
      SELECT id, name, tier, created_at, expires_at, revoked_at, last_used_at FROM ai_mcp_tokens ORDER BY created_at, id`
    return rows.map(recordOf)
  }
}

/**
 * No token verifies and none can be minted: what main.ts wires when no
 * database is configured, and what mcp/http.ts falls back to when the auth
 * settings cannot be read. `bearer` mode then refuses every request.
 */
export class FailClosedTokenStore implements TokenStore {
  async verify(): Promise<Principal | null> {
    return null
  }
  async mint(): Promise<never> {
    throw new Error('MCP bearer tokens are stored in the database; configure SCADBUDDY_DATABASE_URL (spec §9)')
  }
  async revoke(): Promise<boolean> {
    return false
  }
  async list(): Promise<TokenRecord[]> {
    return []
  }
}
