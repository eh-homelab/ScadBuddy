import { randomBytes } from 'node:crypto'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { CredentialStore, SettingsStore } from '../src/credentials.js'
import { connectDatabase, type Database } from '../src/db.js'
import postgres from 'postgres'
import {
  LEGACY_VERSIONS,
  loadMigrations,
  migrate,
  MIGRATION_ID,
  MIGRATION_LOCK,
  MigrationChecksumError,
  migrationChecksum,
  MigrationLedgerError,
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
      it('apply once, in timestamp order, recorded by file id', async () => {
        const ids = MIGRATIONS.map((m) => m.id)
        expect(ids).toEqual([...ids].sort())
        expect(await migrate(db.sql)).toEqual(ids)
        expect(await migrate(db.sql)).toEqual([])
        const rows = await db.sql<{ id: string; version: number | null; story: string | null }[]>`
          SELECT id, version, story FROM ai_migrations ORDER BY applied_at, id`
        expect(rows.map((r) => r.id)).toEqual(ids)
        // The pre-#491 files keep their position, so an older image still reads the ledger.
        expect(rows.slice(0, LEGACY_VERSIONS.length).map((r) => [r.id, r.version, r.story])).toEqual(
          LEGACY_VERSIONS.map((legacy, i) => [legacy.id, i + 1, legacy.story]),
        )
      })

      it('two pods starting together apply each migration exactly once', async () => {
        const other = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
        try {
          const [a, b] = await Promise.all([migrate(db.sql), migrate(other.sql)])
          expect([...a, ...b].sort()).toEqual(MIGRATIONS.map((m) => m.id))
        } finally {
          await other.close()
        }
      })

      it('apply a later migration on top of an existing schema', async () => {
        await migrate(db.sql)
        const next = [...MIGRATIONS, { id: '29990101T0000Z_example', sql: 'CREATE TABLE ai_example (id int PRIMARY KEY)' }]
        expect(await migrate(db.sql, next)).toEqual(['29990101T0000Z_example'])
        expect(await db.sql`SELECT * FROM ai_example`).toHaveLength(0)
      })

      it('apply a file with an OLDER timestamp that arrives after newer ones ran (a late-merged branch)', async () => {
        const newer = { id: '29990102T0000Z_newer', sql: 'CREATE TABLE ai_newer (id int PRIMARY KEY)' }
        const older = {
          id: '29990101T0000Z_older',
          sql: 'CREATE TABLE ai_older (id int PRIMARY KEY); INSERT INTO ai_newer VALUES (1)',
        }
        expect(await migrate(db.sql, [...MIGRATIONS, newer])).toEqual([...MIGRATIONS.map((m) => m.id), newer.id])
        expect(await migrate(db.sql, [...MIGRATIONS, newer, older])).toEqual([older.id])
        expect(await db.sql`SELECT * FROM ai_newer`).toHaveLength(1)
        // Several unapplied files go in timestamp order, whatever order they were passed in.
        const [c, d] = [
          { id: '29990104T0000Z_d', sql: 'INSERT INTO ai_c VALUES (1)' },
          { id: '29990103T0000Z_c', sql: 'CREATE TABLE ai_c (id int)' },
        ]
        expect(await migrate(db.sql, [...MIGRATIONS, newer, older, c!, d!])).toEqual([d!.id, c!.id])
      })

      it('leave a ledger row for a file this build does not have alone', async () => {
        await migrate(db.sql, [...MIGRATIONS, { id: '29990101T0000Z_future', sql: 'SELECT 1' }])
        expect(await migrate(db.sql)).toEqual([])
        expect(await db.sql`SELECT 1 FROM ai_migrations WHERE id = '29990101T0000Z_future'`).toHaveLength(1)
      })

      it('are what ready() runs, memoised', async () => {
        expect(await db.ready()).toBe(true)
        expect(await db.ready()).toBe(true)
        expect(await db.sql`SELECT id FROM ai_migrations`).toHaveLength(MIGRATIONS.length)
      })

      it('record a checksum and refuse an applied file whose SQL changed (finding 10)', async () => {
        await migrate(db.sql)
        const rows = await db.sql<{ checksum: string }[]>`SELECT checksum FROM ai_migrations ORDER BY id`
        expect(rows.map((r) => r.checksum)).toEqual(MIGRATIONS.map(migrationChecksum))

        const first = MIGRATIONS[0]!.id
        const edited = MIGRATIONS.map((m, i) => (i === 0 ? { ...m, sql: `${m.sql}\n-- edited` } : m))
        await expect(migrate(db.sql, edited)).rejects.toThrow(MigrationChecksumError)
        await expect(migrate(db.sql, edited)).rejects.toThrow(`ai migration ${first} was applied with different SQL`)
        // The original still passes.
        expect(await migrate(db.sql)).toEqual([])
      })

      describe('from the positional ledger (before #491)', () => {
        /** The ledger as #354 created it, optionally with #354's later checksum column. */
        async function positionalLedger(versions: number[], { checksums = true } = {}) {
          await db.sql.begin(async (tx) => {
            await tx`CREATE TABLE ai_migrations (
                       version integer PRIMARY KEY, story text NOT NULL,
                       applied_at timestamptz NOT NULL DEFAULT now())`
            if (checksums) await tx`ALTER TABLE ai_migrations ADD COLUMN checksum text`
            for (const version of versions) {
              const legacy = LEGACY_VERSIONS[version - 1]
              const migration = MIGRATIONS.find((m) => m.id === legacy?.id)
              if (migration) await tx.unsafe(migration.sql)
              if (checksums) {
                await tx`INSERT INTO ai_migrations (version, story, checksum)
                         VALUES (${version}, ${legacy?.story ?? 'branch'}, ${migration ? migrationChecksum(migration) : 'x'})`
              } else {
                await tx`INSERT INTO ai_migrations (version, story) VALUES (${version}, ${legacy?.story ?? 'branch'})`
              }
            }
          })
        }

        it('the legacy files are the old list, in order, byte for byte', () => {
          expect(MIGRATIONS.slice(0, LEGACY_VERSIONS.length).map((m) => m.id)).toEqual(LEGACY_VERSIONS.map((l) => l.id))
          // The checksums #354 recorded in every deployed ledger.
          expect(MIGRATIONS.slice(0, 2).map(migrationChecksum)).toEqual([
            '6e2ec704499aed89d132836b34717b1a7ccfafb7a4a1654340ee1b7fc477b613',
            '117ed74ea5125fe163247bd88e3e99c0e75e085d22ec7b220fa99fd4e84bce80',
          ])
        })

        it('rewrites positional rows to file ids without re-running them', async () => {
          await positionalLedger(LEGACY_VERSIONS.map((_, i) => i + 1))
          const later = MIGRATIONS.slice(LEGACY_VERSIONS.length).map((m) => m.id)
          expect(await migrate(db.sql)).toEqual(later)
          expect(await migrate(db.sql)).toEqual([])
          const rows = await db.sql<{ id: string; version: number | null; checksum: string }[]>`
            SELECT id, version, checksum FROM ai_migrations WHERE version IS NOT NULL ORDER BY version`
          expect(rows.map((r) => [r.id, r.version, r.checksum])).toEqual(
            MIGRATIONS.slice(0, LEGACY_VERSIONS.length).map((m, i) => [m.id, i + 1, migrationChecksum(m)]),
          )
          // What an image from before #491 reads still answers as it expects.
          const positional = await db.sql<{ version: number }[]>`
            SELECT version, checksum FROM ai_migrations WHERE version IS NOT NULL ORDER BY version`
          expect(positional.map((r) => r.version)).toEqual(LEGACY_VERSIONS.map((_, i) => i + 1))
        })

        it('converts once when two pods start together', async () => {
          await positionalLedger([1, 2])
          const other = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
          try {
            const [a, b] = await Promise.all([migrate(db.sql), migrate(other.sql)])
            expect([...a, ...b].sort()).toEqual(MIGRATIONS.slice(2).map((m) => m.id))
          } finally {
            await other.close()
          }
          expect(await db.sql`SELECT id FROM ai_migrations`).toHaveLength(MIGRATIONS.length)
        })

        it('applies what the positional ledger had not run, and adopts rows without a checksum', async () => {
          // #354's ledger before the checksum column, with only entry 1 applied.
          await positionalLedger([1], { checksums: false })
          expect(await migrate(db.sql)).toEqual(MIGRATIONS.slice(1).map((m) => m.id))
          const [row] = await db.sql<{ id: string; version: number; story: string; checksum: string | null }[]>`
            SELECT id, version, story, checksum FROM ai_migrations WHERE version = 2`
          expect(row).toMatchObject({ id: LEGACY_VERSIONS[1]!.id, story: '#300' })
          const [first] = await db.sql<{ checksum: string | null }[]>`
            SELECT checksum FROM ai_migrations WHERE id = ${LEGACY_VERSIONS[0]!.id}`
          expect(first?.checksum).toBe(migrationChecksum(MIGRATIONS[0]!))
          const edited = MIGRATIONS.map((m, i) => (i === 0 ? { ...m, sql: m.sql.replace('text', 'varchar') } : m))
          await expect(migrate(db.sql, edited)).rejects.toThrow(MigrationChecksumError)
        })

        it('refuses a positional row main never had, and changes nothing', async () => {
          await positionalLedger([1, 2, 3])
          await expect(migrate(db.sql)).rejects.toThrow(MigrationLedgerError)
          await expect(migrate(db.sql)).rejects.toThrow(/positional version\(s\) 3, but main only ever had 2/)
          const columns = await db.sql<{ column_name: string }[]>`
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'ai_migrations'`
          expect(columns.map((c) => c.column_name)).not.toContain('id')
        })
      })

      it('load files in timestamp order and refuse a misnamed one', () => {
        const dir = mkdtempSync(join(tmpdir(), 'ai-migrations-'))
        try {
          writeFileSync(join(dir, '20260102T0000Z_b.sql'), 'SELECT 2')
          writeFileSync(join(dir, '20260101T2359Z_a.sql'), 'SELECT 1')
          const url = pathToFileURL(`${dir}/`)
          expect(loadMigrations(url)).toEqual([
            { id: '20260101T2359Z_a', sql: 'SELECT 1' },
            { id: '20260102T0000Z_b', sql: 'SELECT 2' },
          ])
          writeFileSync(join(dir, '2026-01-03_c.sql'), 'SELECT 3')
          expect(() => loadMigrations(url)).toThrow(/is not a migration/)
        } finally {
          rmSync(dir, { recursive: true })
        }
        for (const m of MIGRATIONS) expect(m.id).toMatch(MIGRATION_ID)
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
          // Test files running in parallel migrate their own schemas under the
          // same advisory lock and can hold it past this connection's 200 ms
          // lock_timeout, so allow ready() a few retries.
          let ready = false
          for (let i = 0; i < 20 && !ready; i++) ready = await waiting.ready()
          expect(ready).toBe(true)
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
          db.sql`INSERT INTO ai_credentials (id, priority, kind, base_url, secret_sealed, dek_sealed, kek_id, last4)
                 VALUES ('x', 0, 'gateway', NULL, ${bytes}, ${bytes}, 'k', '')`,
        ).rejects.toThrow(/check constraint/)
        await expect(
          db.sql`INSERT INTO ai_credentials (id, priority, kind, base_url, secret_sealed, dek_sealed, kek_id, last4)
                 VALUES ('x', 0, 'bedrock', NULL, ${bytes}, ${bytes}, 'k', '')`,
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

      it('stores and reveals a Claude Code OAuth token', async () => {
        const store = new CredentialStore(db.sql)
        await store.put({ kind: 'claude_oauth_token', secret: SECRET }, kek)
        expect(await store.reveal(kek)).toEqual({ kind: 'claude_oauth_token', secret: SECRET })
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
