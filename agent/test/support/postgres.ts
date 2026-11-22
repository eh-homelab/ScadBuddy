import { createHash, randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import postgres, { type Sql } from 'postgres'
import { connectDatabase, type Database } from './database.js'
import { migrate, MIGRATIONS } from './migrations.js'

// A Postgres the tests may create and drop schemas and databases in, the same
// contract as the backend's `requires_postgres` tests (backend/tests/conftest.py):
// unset, the tests that need it skip; CI sets it to a service container.
export const TEST_DATABASE_URL_ENV = 'SCADBUDDY_TEST_DATABASE_URL'
export const TEST_DATABASE_URL = process.env[TEST_DATABASE_URL_ENV]?.trim() || undefined

export type Throwaway = {
  db: Database
  /** Where `db` connects: give it, with `schema`, to any other connection the test opens. */
  url: string
  /** The `search_path` of `db`. */
  schema: string
  drop: () => Promise<void>
}

/**
 * A database of the test's own, already migrated, and a Database connected to it;
 * `drop()` closes the Database, then drops the database outside the hook (`dropLater`).
 * It is copied from a template migrated once per set of migrations (#2018): migrating
 * per test cost each test ~0.2 s, and seconds under load, since MIGRATION_LOCK
 * serialises migrating across the whole server.
 *
 * `{ empty: true }` gives a fresh, unmigrated schema in the shared database
 * instead, for the tests of migrating itself; its `ready()` migrates it (`migrateSchema`).
 */
export async function throwawayDatabase(options: { empty?: boolean } = {}): Promise<Throwaway> {
  if (!TEST_DATABASE_URL) throw new Error(`${TEST_DATABASE_URL_ENV} is not set`)
  const base = TEST_DATABASE_URL
  const admin = postgres(base, { max: 1, onnotice: () => {} })
  const name = `test_${randomUUID().replaceAll('-', '').slice(0, 12)}`
  if (options.empty) {
    await admin.unsafe(`CREATE SCHEMA "${name}"`)
    const db = connectDatabase(base, { searchPath: name, schema: migrateSchema })
    return {
      db,
      url: base,
      schema: name,
      drop: async () => {
        await db.close()
        await admin.unsafe(`DROP SCHEMA "${name}" CASCADE`)
        await admin.end({ timeout: 5 })
      },
    }
  }
  try {
    await admin.unsafe(`CREATE DATABASE "${name}" TEMPLATE "${TEMPLATE}"`)
  } finally {
    await admin.end({ timeout: 5 })
  }
  const url = databaseUrl(base, name)
  const db = connectDatabase(url, { searchPath: 'public' })
  return {
    db,
    url,
    schema: 'public',
    drop: async () => {
      await db.close()
      dropLater(base, name)
    },
  }
}

/** This process's one connection for `dropLater`: it drops the databases one at a time. */
let dropper: Sql | undefined

/**
 * Drops a throwaway database after its test, outside the test's hook (#7044). `DROP DATABASE`
 * requests an immediate checkpoint and waits for it (dropdb in
 * [dbcommands.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/commands/dbcommands.c)).
 * On a slow CI disk one such checkpoint took 20.7 s (sync 15.8 s), and an `afterEach` that
 * waited for it timed out at 20 s, although the test had closed every connection it opened.
 * So `drop()` waits only for `db.close()`, and the DROP runs here, queued on one connection.
 * A failed DROP is an unhandled rejection, which fails the run. A process that ends with a
 * DROP still queued leaves that database behind, as a killed run does.
 */
function dropLater(base: string, name: string): void {
  dropper ??= postgres(base, { max: 1, idle_timeout: 1, onnotice: () => {} })
  dropper.unsafe(`DROP DATABASE "${name}" WITH (FORCE)`).catch((err: unknown) => {
    throw new Error(`dropping the test database ${name} failed`, { cause: err })
  })
}

/**
 * How a test's schema is made: the agent's own runner over backend/scadbuddy/migrations_agent/
 * (support/migrations.ts). In production the API applies them and `ready()` waits (#5299 D6);
 * a copy of the template already holds the ledger, so its `ready()` takes that path.
 */
export const migrateSchema = async (sql: Sql): Promise<void> => {
  await migrate(sql)
}

function databaseUrl(url: string, database: string): string {
  const u = new URL(url)
  u.pathname = `/${database}`
  return u.toString()
}

// Named for the migrations it holds and the code that applies them (its ledger
// included), so a changed or added migration, or a change to how they are recorded,
// builds a new template rather than copying a stale one.
const TEMPLATE = `test_template_${createHash('sha256')
  .update(JSON.stringify(MIGRATIONS))
  .update(readFileSync(new URL('./migrations.ts', import.meta.url)))
  .digest('hex')
  .slice(0, 16)}`
// Distinct from MIGRATION_LOCK: it only orders the suites building the template.
const TEMPLATE_LOCK = 0x5343_4144_5450_4c54n

/**
 * Builds the migrated template unless it exists: once per run, from vitest's
 * globalSetup, before any worker copies it. A killed run cannot leave a
 * half-migrated template: it is built under another name and renamed when done.
 */
export async function ensureTemplate(): Promise<void> {
  if (!TEST_DATABASE_URL) return
  const admin = postgres(TEST_DATABASE_URL, { max: 1, onnotice: () => {} })
  try {
    // Two suites started at once (two worktrees, say) take turns.
    await admin`SELECT pg_advisory_lock(${TEMPLATE_LOCK.toString()}::bigint)`
    const [found] = await admin`SELECT 1 FROM pg_database WHERE datname = ${TEMPLATE}`
    if (found) return
    const building = `${TEMPLATE}_building`
    await admin.unsafe(`DROP DATABASE IF EXISTS "${building}" WITH (FORCE)`)
    await admin.unsafe(`CREATE DATABASE "${building}"`)
    let cause: unknown
    const db = connectDatabase(databaseUrl(TEST_DATABASE_URL, building), {
      searchPath: 'public',
      schema: migrateSchema,
      onMigrationError: (err) => (cause = err),
    })
    try {
      if (!(await db.ready())) throw new Error(`migrating the test template ${building} failed`, { cause })
    } finally {
      await db.close()
    }
    await admin.unsafe(`ALTER DATABASE "${building}" RENAME TO "${TEMPLATE}"`)
  } finally {
    await admin.end({ timeout: 5 })
  }
}
