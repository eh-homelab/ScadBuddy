import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { connectDatabase, type Database } from '../src/db.js'
import { awaitMigrations, EXPECTED_MIGRATIONS, MigrationChecksumError, MigrationsPendingError } from '../src/db/schema.js'
import { migrate, MIGRATIONS } from './support/migrations.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #5299 D6: the agent no longer migrates; `ready()` waits until the API has applied
// every file the agent expects. The API is stood in for by the test runner.

describe.skipIf(!TEST_DATABASE_URL)(
  `waiting for the API's migrations${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let schema: string
    let drop: () => Promise<void>
    const lines: string[] = []
    const log = (line: string) => void lines.push(line)

    beforeEach(async () => {
      ;({ db, schema, drop } = await throwawayDatabase({ empty: true }))
      lines.length = 0
    })
    afterEach(async () => {
      await drop()
    })

    it('expects every file, so a migrated schema is ready at the first read and nothing is logged', async () => {
      await migrate(db.sql)
      expect(EXPECTED_MIGRATIONS.map((m) => m.id)).toEqual(MIGRATIONS.map((m) => m.id))
      await awaitMigrations(db.sql, EXPECTED_MIGRATIONS, { attempts: 1, log })
      expect(lines).toEqual([])
    })

    it('waits for a ledger the API has not made yet, says so once, and gives up after its reads', async () => {
      const started = performance.now()
      await expect(awaitMigrations(db.sql, EXPECTED_MIGRATIONS, { attempts: 3, intervalMs: 20, log })).rejects.toThrow(
        MigrationsPendingError,
      )
      expect(performance.now() - started).toBeGreaterThanOrEqual(40)
      expect(lines).toEqual([
        `waiting for the API to apply the agent's database migrations: ${EXPECTED_MIGRATIONS.length} of ` +
          `${EXPECTED_MIGRATIONS.length} not in ai_migrations yet (first ${EXPECTED_MIGRATIONS[0]!.id})`,
      ])
    })

    it('says how many reads it made only when it made more than one', async () => {
      const once = awaitMigrations(db.sql, EXPECTED_MIGRATIONS, { attempts: 1, log })
      await expect(once).rejects.toThrow(/migrations \(first \S+\) in ai_migrations; is the API/)
      await expect(once).rejects.not.toThrow(/after 1 reads/)
      await expect(awaitMigrations(db.sql, EXPECTED_MIGRATIONS, { attempts: 2, intervalMs: 1, log })).rejects.toThrow(
        /after 2 reads of ai_migrations/,
      )
    })

    it('waits on a ledger still keyed by position (before #491), which the API rewrites at its next start', async () => {
      await db.sql`CREATE TABLE ai_migrations (version integer PRIMARY KEY, story text)`
      await db.sql`INSERT INTO ai_migrations (version, story) VALUES (1, 'the first')`
      await expect(awaitMigrations(db.sql, EXPECTED_MIGRATIONS, { attempts: 1, log })).rejects.toThrow(
        MigrationsPendingError,
      )
      expect(lines).toEqual([
        `waiting for the API to apply the agent's database migrations: ai_migrations is still keyed by ` +
          `position (before #491), and the API's next start rewrites it`,
      ])
    })

    it('goes on once the API has applied the last file it expects', async () => {
      const last = MIGRATIONS[MIGRATIONS.length - 1]!
      await migrate(db.sql, MIGRATIONS.slice(0, -1))
      const waiting = awaitMigrations(db.sql, EXPECTED_MIGRATIONS, { attempts: 200, intervalMs: 20, log })
      await expect.poll(() => lines.length).toBe(1)
      expect(lines[0]).toContain(`1 of ${EXPECTED_MIGRATIONS.length} not in ai_migrations yet (first ${last.id})`)
      await migrate(db.sql)
      await waiting
      expect(lines).toHaveLength(1)
    })

    it('refuses a file applied with other SQL than this build expects', async () => {
      await migrate(db.sql)
      const [first, ...rest] = EXPECTED_MIGRATIONS
      const edited = [{ id: first!.id, sha256: '0'.repeat(64) }, ...rest]
      await expect(awaitMigrations(db.sql, edited, { attempts: 1, log })).rejects.toThrow(MigrationChecksumError)
    })

    it('counts a row with no checksum (from before #354) as applied', async () => {
      await migrate(db.sql)
      await db.sql`UPDATE ai_migrations SET checksum = NULL WHERE id = ${EXPECTED_MIGRATIONS[0]!.id}`
      await awaitMigrations(db.sql, EXPECTED_MIGRATIONS, { attempts: 1, log })
    })

    it('ready() reads the ledger once and fails fast; it says it is waiting once, and tells onMigrationError only of other failures', async () => {
      const errors: unknown[] = []
      const fast = connectDatabase(TEST_DATABASE_URL!, {
        searchPath: schema,
        awaitMigrations: { log },
        onMigrationError: (err) => errors.push(err),
      })
      try {
        const started = performance.now()
        expect(await fast.ready()).toBe(false)
        expect(await fast.ready()).toBe(false)
        expect(performance.now() - started).toBeLessThan(1_000)
        // The wait's own line says it is waiting; a pending read is not an error to log each time.
        expect(errors).toEqual([])
        expect(lines).toHaveLength(1)
        expect(fast.waitingForMigrations()).toBe(true)
        // A ledger still keyed by position (before #491) is waiting too: the API rewrites it.
        await db.sql`CREATE TABLE ai_migrations (version integer PRIMARY KEY)`
        expect(await fast.ready()).toBe(false)
        expect(errors).toEqual([])
        expect(fast.waitingForMigrations()).toBe(true)
        // An edited migration is still told, so main.ts can stop.
        await db.sql`DROP TABLE ai_migrations`
        await migrate(db.sql)
        await db.sql`UPDATE ai_migrations SET checksum = ${'0'.repeat(64)} WHERE id = ${EXPECTED_MIGRATIONS[0]!.id}`
        expect(await fast.ready()).toBe(false)
        expect(errors.map((e) => (e as Error).name)).toEqual(['MigrationChecksumError'])
      } finally {
        await fast.close()
      }
    })

    it('is what ready() runs: false while the API has not migrated, true at the next call once it has', async () => {
      const errors: unknown[] = []
      const waiting = connectDatabase(TEST_DATABASE_URL!, {
        searchPath: schema,
        awaitMigrations: { attempts: 2, intervalMs: 10, log },
        onMigrationError: (err) => errors.push(err),
      })
      try {
        expect(await waiting.ready()).toBe(false)
        expect(errors).toEqual([])
        expect(waiting.waitingForMigrations()).toBe(true)
        // Nothing made the agent's tables: it never migrates.
        expect(await db.sql`SELECT to_regclass('ai_sessions') AS t`).toEqual([{ t: null }])
        await migrate(db.sql)
        expect(await waiting.ready()).toBe(true)
        expect(waiting.waitingForMigrations()).toBe(false)
      } finally {
        await waiting.close()
      }
    })
  },
)
