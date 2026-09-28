import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { hashToken, PostgresTokenStore, TOKEN_PREFIX } from '../src/auth/tokens.js'
import { connectDatabase, type Database } from '../src/db.js'
import { migrate } from '../src/db/migrations.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// PostgresTokenStore on `ai_mcp_tokens` (#251; db/migrations/20260928T0734Z_mcp_tokens.sql).

type Row = {
  id: string
  name: string
  tier: string
  token_hash: string
  created_at: Date
  expires_at: Date | null
  revoked_at: Date | null
  last_used_at: Date | null
}

describe.skipIf(!TEST_DATABASE_URL)(
  `PostgresTokenStore${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let schema: string
    let drop: () => Promise<void>
    let store: PostgresTokenStore

    beforeEach(async () => {
      ;({ db, schema, drop } = await throwawayDatabase())
      await migrate(db.sql)
      store = new PostgresTokenStore(db.sql)
    })
    afterEach(async () => {
      await drop()
    })

    const rows = () => db.sql<Row[]>`SELECT * FROM ai_mcp_tokens ORDER BY created_at, id`

    it('mints a token, returns it once, and lists its metadata', async () => {
      const expiresAt = new Date('2030-01-01T00:00:00Z')
      const { token, record } = await store.mint({ name: 'ci', tier: 'write', expiresAt })
      expect(token.startsWith(TOKEN_PREFIX)).toBe(true)
      expect(token.length).toBe(TOKEN_PREFIX.length + 43) // 32 bytes, base64url, unpadded
      expect(record).toMatchObject({
        name: 'ci',
        tier: 'write',
        expiresAt,
        revokedAt: undefined,
        lastUsedAt: undefined,
      })
      expect(record.createdAt).toBeInstanceOf(Date)
      expect(await store.list()).toEqual([record])
      expect(JSON.stringify(await store.list())).not.toContain(token)
    })

    it('stores only the SHA-256 of the token, never the plaintext', async () => {
      const { token, record } = await store.mint({ name: 'hash-only', tier: 'outward' })
      const [row] = await rows()
      expect(row?.id).toBe(record.id)
      expect(row?.token_hash).toBe(hashToken(token))
      expect(row?.token_hash).toMatch(/^[0-9a-f]{64}$/)
      // No column of the row, as Postgres renders it, holds the token or its random part.
      const [whole] = await db.sql<{ t: string }[]>`SELECT t::text AS t FROM ai_mcp_tokens t`
      expect(whole?.t).not.toContain(token)
      expect(whole?.t).not.toContain(token.slice(TOKEN_PREFIX.length))
      // And the schema refuses a plaintext token in the hash column.
      await expect(
        db.sql`INSERT INTO ai_mcp_tokens (id, name, tier, token_hash) VALUES (${randomUUID()}, 'x', 'read', ${token})`,
      ).rejects.toThrow(/token_hash_check/)
    })

    it('verifies to the token tier, and refuses unknown and expired tokens', async () => {
      const { token, record } = await store.mint({ name: 'w', tier: 'write' })
      expect(await store.verify(token)).toEqual({ id: `token:${record.id}`, kind: 'bearer', tiers: ['read', 'write'] })
      expect(await store.verify(`${TOKEN_PREFIX}unknown`)).toBeNull()
      expect(await store.verify('')).toBeNull()

      const expiring = await store.mint({ name: 'e', tier: 'read', expiresAt: new Date('2026-01-01T00:00:00Z') })
      expect(await store.verify(expiring.token, new Date('2025-12-31T23:59:59Z'))).not.toBeNull()
      expect(await store.verify(expiring.token, new Date('2026-01-01T00:00:00Z'))).toBeNull()
    })

    it('revokes a live token once; a revoked token no longer verifies', async () => {
      const { token, record } = await store.mint({ name: 'r', tier: 'read' })
      expect(await store.revoke(record.id)).toBe(true)
      expect(await store.revoke(record.id)).toBe(false)
      expect(await store.verify(token)).toBeNull()
      expect((await store.list())[0]?.revokedAt).toBeInstanceOf(Date)
      // Unknown ids, and ids that are not uuids at all, answer false rather than throwing.
      expect(await store.revoke(randomUUID())).toBe(false)
      expect(await store.revoke('not-a-uuid')).toBe(false)
    })

    it('records last use, and never moves it backwards', async () => {
      const { token } = await store.mint({ name: 'l', tier: 'read' })
      expect((await store.list())[0]?.lastUsedAt).toBeUndefined()
      const t1 = new Date('2026-09-28T12:00:00Z')
      const t0 = new Date('2026-09-28T11:00:00Z')
      await store.verify(token, t1)
      expect((await store.list())[0]?.lastUsedAt).toEqual(t1)
      await store.verify(token, t0)
      expect((await store.list())[0]?.lastUsedAt).toEqual(t1)
      // A refused verify does not touch it.
      await store.verify(`${TOKEN_PREFIX}other`, new Date('2027-01-01T00:00:00Z'))
      expect((await store.list())[0]?.lastUsedAt).toEqual(t1)
    })

    it('verifies concurrently from two replicas; last use is the latest', async () => {
      const other = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
      try {
        const replica = new PostgresTokenStore(other.sql)
        const { token, record } = await store.mint({ name: 'c', tier: 'outward' })
        const base = Date.parse('2026-09-28T12:00:00Z')
        const times = Array.from({ length: 40 }, (_, i) => new Date(base + i * 1000))
        const results = await Promise.all(times.map((t, i) => (i % 2 ? replica : store).verify(token, t)))
        expect(results.every((p) => p?.id === `token:${record.id}`)).toBe(true)
        expect((await replica.list())[0]?.lastUsedAt).toEqual(times.at(-1))
        expect(await rows()).toHaveLength(1)
      } finally {
        await other.close()
      }
    })

    it('sees a revoke made by another replica immediately', async () => {
      const other = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
      try {
        const { token, record } = await store.mint({ name: 'x', tier: 'read' })
        const replica = new PostgresTokenStore(other.sql)
        expect(await replica.verify(token)).not.toBeNull()
        expect(await store.revoke(record.id)).toBe(true)
        expect(await replica.verify(token)).toBeNull()
      } finally {
        await other.close()
      }
    })
  },
)
