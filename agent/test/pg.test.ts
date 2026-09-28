import { randomBytes } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { CredentialStore, SettingsStore } from '../src/credentials.js'
import { connectDatabase, type Database } from '../src/db.js'
import postgres from 'postgres'
import {
  migrate,
  MIGRATION_LOCK,
  MigrationChecksumError,
  migrationChecksum,
  MIGRATIONS,
} from '../src/db/migrations.js'
import { kekFromBase64, SealError } from '../src/secrets.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const SECRET = 'sk-ant-api03-postgres-test-secret-5b5b'

describe.skipIf(!TEST_DATABASE_URL)(
  `the ai_* tables in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let schema: string
    let drop: () => Promise<void>

    beforeEach(async () => {
      ;({ db, schema, drop } = await throwawayDatabase())
    })
    afterEach(async () => {
      await drop()
    })

    describe('migrations', () => {
      it('apply once, in order, and record their story', async () => {
        expect(await migrate(db.sql)).toEqual(MIGRATIONS.map((_, i) => i + 1))
        expect(await migrate(db.sql)).toEqual([])
        const rows = await db.sql<{ version: number; story: string }[]>`
          SELECT version, story FROM ai_migrations ORDER BY version`
        expect(rows.map((r) => [r.version, r.story])).toEqual(MIGRATIONS.map((m, i) => [i + 1, m.story]))
      })

      it('two pods starting together apply each migration exactly once', async () => {
        const other = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
        try {
          const [a, b] = await Promise.all([migrate(db.sql), migrate(other.sql)])
          expect([...a, ...b].sort()).toEqual(MIGRATIONS.map((_, i) => i + 1))
        } finally {
          await other.close()
        }
      })

      it('apply a later migration on top of an existing schema', async () => {
        await migrate(db.sql)
        const next = [...MIGRATIONS, { story: 'test', sql: 'CREATE TABLE ai_example (id int PRIMARY KEY)' }]
        expect(await migrate(db.sql, next)).toEqual([MIGRATIONS.length + 1])
        expect(await db.sql`SELECT * FROM ai_example`).toHaveLength(0)
      })

      it('are what ready() runs, memoised', async () => {
        expect(await db.ready()).toBe(true)
        expect(await db.ready()).toBe(true)
        expect(await db.sql`SELECT version FROM ai_migrations`).toHaveLength(MIGRATIONS.length)
      })

      it('record a checksum and refuse an applied entry whose SQL changed (finding 10)', async () => {
        await migrate(db.sql)
        const rows = await db.sql<{ version: number; checksum: string }[]>`
          SELECT version, checksum FROM ai_migrations ORDER BY version`
        expect(rows.map((r) => r.checksum)).toEqual(MIGRATIONS.map(migrationChecksum))

        const edited = MIGRATIONS.map((m, i) => (i === 0 ? { ...m, sql: `${m.sql}\n-- edited` } : m))
        await expect(migrate(db.sql, edited)).rejects.toThrow(MigrationChecksumError)
        await expect(migrate(db.sql, edited)).rejects.toThrow(/ai migration 1 \(#255\) was applied with different SQL/)
        // The original still passes.
        expect(await migrate(db.sql)).toEqual([])
      })

      it('adopt rows applied before the checksum column existed, then check them', async () => {
        // The ledger exactly as #354 created it: no checksum column.
        await db.sql.begin(async (tx) => {
          await tx`CREATE TABLE ai_migrations (
                     version integer PRIMARY KEY, story text NOT NULL,
                     applied_at timestamptz NOT NULL DEFAULT now())`
          await tx.unsafe(MIGRATIONS[0]!.sql)
          await tx`INSERT INTO ai_migrations (version, story) VALUES (1, '#255')`
        })
        expect(await migrate(db.sql)).toEqual(MIGRATIONS.slice(1).map((_, i) => i + 2))
        const [row] = await db.sql<{ checksum: string | null }[]>`SELECT checksum FROM ai_migrations WHERE version = 1`
        expect(row?.checksum).toBe(migrationChecksum(MIGRATIONS[0]!))
        const edited = MIGRATIONS.map((m, i) => (i === 0 ? { ...m, sql: m.sql.replace('text', 'varchar') } : m))
        await expect(migrate(db.sql, edited)).rejects.toThrow(MigrationChecksumError)
      })

      it('give up on a held advisory lock after lock_timeout, and ready() retries (finding 3)', async () => {
        const errors: unknown[] = []
        const holder = postgres(TEST_DATABASE_URL!, { max: 1, onnotice: () => {} })
        const waiting = connectDatabase(TEST_DATABASE_URL!, {
          searchPath: schema,
          migrate: { lockTimeoutMs: 200 },
          onMigrationError: (err) => errors.push(err),
        })
        let release!: () => void
        const released = new Promise<void>((resolve) => (release = resolve))
        let locked!: () => void
        const isLocked = new Promise<void>((resolve) => (locked = resolve))
        const held = holder.begin(async (tx) => {
          await tx`SELECT pg_advisory_xact_lock(${MIGRATION_LOCK.toString()}::bigint)`
          locked()
          await released
        })
        try {
          await isLocked
          const started = Date.now()
          expect(await waiting.ready()).toBe(false)
          expect(Date.now() - started).toBeLessThan(5000)
          expect(String(errors[0])).toMatch(/lock timeout/i)
          release()
          await held
          expect(await waiting.ready()).toBe(true)
        } finally {
          release()
          await held.catch(() => {})
          await holder.end({ timeout: 5 })
          await waiting.close()
        }
      })

      it('enforce a base URL for gateways and none for API keys', async () => {
        await migrate(db.sql)
        const bytes = Buffer.from([1])
        await expect(
          db.sql`INSERT INTO ai_credentials (id, kind, base_url, secret_sealed, dek_sealed, kek_id, last4)
                 VALUES ('x', 'gateway', NULL, ${bytes}, ${bytes}, 'k', '')`,
        ).rejects.toThrow(/check constraint/)
        await expect(
          db.sql`INSERT INTO ai_credentials (id, kind, base_url, secret_sealed, dek_sealed, kek_id, last4)
                 VALUES ('x', 'bedrock', NULL, ${bytes}, ${bytes}, 'k', '')`,
        ).rejects.toThrow(/check constraint/)
      })
    })

    describe('CredentialStore', () => {
      beforeEach(async () => {
        await migrate(db.sql)
      })

      it('seals the secret, reads back only the summary, and reveals with the KEK', async () => {
        const store = new CredentialStore(db.sql)
        expect(await store.get()).toBeUndefined()
        const saved = await store.put({ kind: 'anthropic_api_key', secret: SECRET }, kek)
        expect(saved).toMatchObject({ kind: 'anthropic_api_key', base_url: null, last4: '5b5b', kekId: kek.id })
        expect(JSON.stringify(await store.get())).not.toContain(SECRET)
        expect(await store.reveal(kek)).toEqual({ kind: 'anthropic_api_key', secret: SECRET })

        const [raw] = await db.sql<{ secret_sealed: Buffer; dek_sealed: Buffer }[]>`
          SELECT secret_sealed, dek_sealed FROM ai_credentials`
        expect(raw?.secret_sealed.includes(Buffer.from(SECRET))).toBe(false)
        const dump = await db.sql`SELECT * FROM ai_credentials`
        expect(JSON.stringify(dump)).not.toContain(SECRET)
      })

      it('keeps the stored secret on a PUT without one, and replaces it on a PUT with one', async () => {
        const store = new CredentialStore(db.sql)
        await store.put({ kind: 'gateway', base_url: 'https://llm.example/', secret: 'gw-token-aaaaaaaa1111' }, kek)
        const before = await db.sql`SELECT secret_sealed FROM ai_credentials`
        await store.put({ kind: 'gateway', base_url: 'https://llm.example' }, kek)
        expect(await db.sql`SELECT secret_sealed FROM ai_credentials`).toEqual(before)
        await store.put({ kind: 'gateway', base_url: 'https://llm.example', secret: 'gw-token-bbbbbbbb2222' }, kek)
        expect(await store.reveal(kek)).toEqual({
          kind: 'gateway',
          baseUrl: 'https://llm.example',
          secret: 'gw-token-bbbbbbbb2222',
        })
        expect(await db.sql`SELECT id FROM ai_credentials`).toHaveLength(1)
      })

      it('detects a tampered row and a different KEK', async () => {
        const store = new CredentialStore(db.sql)
        await store.put({ kind: 'anthropic_api_key', secret: SECRET }, kek)
        await expect(store.reveal(kekFromBase64(randomBytes(32).toString('base64')))).rejects.toThrow(SealError)
        await db.sql`UPDATE ai_credentials SET secret_sealed = set_byte(secret_sealed, 40, get_byte(secret_sealed, 40) # 1)`
        await expect(store.reveal(kek)).rejects.toThrow(SealError)
      })

      it('binds kind and base_url: a row re-pointed in the database does not decrypt (finding 2)', async () => {
        const store = new CredentialStore(db.sql)
        await store.put({ kind: 'gateway', base_url: 'https://llm.example', secret: 'gw-token-aaaaaaaa1111' }, kek)
        await db.sql`UPDATE ai_credentials SET base_url = 'https://attacker.example'`
        await expect(store.reveal(kek)).rejects.toThrow(SealError)
        // Even a base URL that differs only in a trailing slash is a different binding.
        await db.sql`UPDATE ai_credentials SET base_url = 'https://llm.example/'`
        await expect(store.reveal(kek)).rejects.toThrow(SealError)
        await db.sql`UPDATE ai_credentials SET base_url = 'https://llm.example'`
        expect(await store.reveal(kek)).toMatchObject({ baseUrl: 'https://llm.example' })
        // Turning the gateway token into an Anthropic API key fails too.
        await db.sql`UPDATE ai_credentials SET kind = 'anthropic_api_key', base_url = NULL`
        await expect(store.reveal(kek)).rejects.toThrow(SealError)
      })

      it('reports a #354-format (v1) row as legacy and refuses to open it', async () => {
        const store = new CredentialStore(db.sql)
        await store.put({ kind: 'anthropic_api_key', secret: SECRET }, kek)
        await db.sql`UPDATE ai_credentials SET secret_sealed = set_byte(secret_sealed, 0, 1)`
        expect(await store.get()).toMatchObject({ legacyFormat: true })
        await expect(store.reveal(kek)).rejects.toThrow(/older format/)
      })

      it('re-wraps rows sealed under the previous key, and only those (finding 4)', async () => {
        const store = new CredentialStore(db.sql)
        const previous = kekFromBase64(randomBytes(32).toString('base64'))
        await store.put({ kind: 'gateway', base_url: 'https://llm.example', secret: 'gw-token-rotate-9999' }, previous)
        const [before] = await db.sql<{ secret_sealed: Buffer; updated_at: Date }[]>`
          SELECT secret_sealed, updated_at FROM ai_credentials`

        expect(await store.rewrapFrom(previous, kek)).toEqual({ rewrapped: 1, failed: 0 })
        const [after] = await db.sql<{ secret_sealed: Buffer; updated_at: Date; kek_id: string }[]>`
          SELECT secret_sealed, updated_at, kek_id FROM ai_credentials`
        expect(after?.kek_id).toBe(kek.id)
        expect(after?.secret_sealed.equals(before!.secret_sealed)).toBe(true)
        expect(after?.updated_at).toEqual(before?.updated_at)
        expect(await store.reveal(kek)).toMatchObject({ secret: 'gw-token-rotate-9999' })
        await expect(store.reveal(previous)).rejects.toThrow(SealError)

        // Nothing left to do on the next start, and the same key twice is a no-op.
        expect(await store.rewrapFrom(previous, kek)).toEqual({ rewrapped: 0, failed: 0 })
        expect(await store.rewrapFrom(kek, kek)).toEqual({ rewrapped: 0, failed: 0 })
      })

      it('leaves a row the previous key cannot open, and counts it', async () => {
        const store = new CredentialStore(db.sql)
        const previous = kekFromBase64(randomBytes(32).toString('base64'))
        await store.put({ kind: 'anthropic_api_key', secret: SECRET }, previous)
        await db.sql`UPDATE ai_credentials SET dek_sealed = set_byte(dek_sealed, 40, get_byte(dek_sealed, 40) # 1)`
        expect(await store.rewrapFrom(previous, kek)).toEqual({ rewrapped: 0, failed: 1 })
        expect(await store.get()).toMatchObject({ kekId: previous.id })
      })

      it('rotates as part of ready(), after migrations', async () => {
        const previous = kekFromBase64(randomBytes(32).toString('base64'))
        await new CredentialStore(db.sql).put({ kind: 'anthropic_api_key', secret: SECRET }, previous)
        const rotating = connectDatabase(TEST_DATABASE_URL!, {
          searchPath: schema,
          afterMigrate: async (sql) => {
            await new CredentialStore(sql).rewrapFrom(previous, kek)
          },
        })
        try {
          expect(await rotating.ready()).toBe(true)
          expect(await new CredentialStore(db.sql).get()).toMatchObject({ kekId: kek.id })
        } finally {
          await rotating.close()
        }
      })

      it('deletes', async () => {
        const store = new CredentialStore(db.sql)
        await store.put({ kind: 'anthropic_api_key', secret: SECRET }, kek)
        expect(await store.delete()).toBe(true)
        expect(await store.delete()).toBe(false)
        expect(await store.get()).toBeUndefined()
      })
    })

    describe('SettingsStore', () => {
      it('stores JSON values by key', async () => {
        await migrate(db.sql)
        const settings = new SettingsStore(db.sql)
        expect(await settings.get('model')).toBeUndefined()
        await settings.set('model', 'claude-sonnet-4-5')
        await settings.set('budget', { per_session_usd: 2 })
        await settings.set('model', 'claude-opus-4-1')
        expect(await settings.get('model')).toBe('claude-opus-4-1')
        expect(await settings.get('budget')).toEqual({ per_session_usd: 2 })
      })
    })
  },
)
