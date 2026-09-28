import { randomBytes } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { CredentialStore, SettingsStore } from '../src/credentials.js'
import { connectDatabase, type Database } from '../src/db.js'
import { migrate, MIGRATIONS } from '../src/db/migrations.js'
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
