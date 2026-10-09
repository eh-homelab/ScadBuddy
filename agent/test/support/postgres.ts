import { createHash, randomUUID } from 'node:crypto'
import postgres from 'postgres'
import { connectDatabase, type Database } from '../../src/db.js'
import { MIGRATIONS } from '../../src/db/migrations.js'

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
 * `drop()` removes both. It is copied from a template migrated once per set of
 * migrations (#2018): migrating per test cost each test ~0.2 s, and seconds under
 * load, since MIGRATION_LOCK serialises migrating across the whole server.
 *
 * `{ empty: true }` gives a fresh, unmigrated schema in the shared database
 * instead, for the tests of migrating itself.
 */
export async function throwawayDatabase(options: { empty?: boolean } = {}): Promise<Throwaway> {
  if (!TEST_DATABASE_URL) throw new Error(`${TEST_DATABASE_URL_ENV} is not set`)
  const base = TEST_DATABASE_URL
  const admin = postgres(base, { max: 1, onnotice: () => {} })
  const name = `test_${randomUUID().replaceAll('-', '').slice(0, 12)}`
  if (options.empty) {
    await admin.unsafe(`CREATE SCHEMA "${name}"`)
    const db = connectDatabase(base, { searchPath: name })
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
  await admin.unsafe(`CREATE DATABASE "${name}" TEMPLATE "${await migratedTemplate(admin)}"`)
  const url = databaseUrl(base, name)
  const db = connectDatabase(url, { searchPath: 'public' })
  return {
    db,
    url,
    schema: 'public',
    drop: async () => {
      await db.close()
      await admin.unsafe(`DROP DATABASE "${name}" WITH (FORCE)`)
      await admin.end({ timeout: 5 })
    },
  }
}

function databaseUrl(url: string, database: string): string {
  const u = new URL(url)
  u.pathname = `/${database}`
  return u.toString()
}

// Named for the migrations it holds, so a changed or added migration builds a
// new template rather than copying a stale one.
const TEMPLATE = `test_template_${createHash('sha256').update(JSON.stringify(MIGRATIONS)).digest('hex').slice(0, 16)}`
// Distinct from MIGRATION_LOCK: it only orders the workers building the template.
const TEMPLATE_LOCK = 0x5343_4144_5450_4c54n

let built: Promise<string> | undefined

/** The migrated template, built by whichever worker of the run gets there first. */
function migratedTemplate(admin: postgres.Sql): Promise<string> {
  built ??= (async () => {
    await admin`SELECT pg_advisory_lock(${TEMPLATE_LOCK.toString()}::bigint)`
    try {
      const [found] = await admin`SELECT 1 FROM pg_database WHERE datname = ${TEMPLATE}`
      if (found) return TEMPLATE
      // Built under another name and renamed once complete, so nothing ever
      // copies a half-migrated template, even one a killed run left behind.
      const building = `${TEMPLATE}_building`
      await admin.unsafe(`DROP DATABASE IF EXISTS "${building}" WITH (FORCE)`)
      await admin.unsafe(`CREATE DATABASE "${building}"`)
      let cause: unknown
      const db = connectDatabase(databaseUrl(TEST_DATABASE_URL!, building), {
        searchPath: 'public',
        onMigrationError: (err) => (cause = err),
      })
      try {
        if (!(await db.ready())) throw new Error(`migrating the test template ${building} failed`, { cause })
      } finally {
        await db.close()
      }
      await admin.unsafe(`ALTER DATABASE "${building}" RENAME TO "${TEMPLATE}"`)
      return TEMPLATE
    } finally {
      await admin`SELECT pg_advisory_unlock(${TEMPLATE_LOCK.toString()}::bigint)`
    }
  })()
  // A failed build is not kept: the next test tries again, and says why.
  built.catch(() => (built = undefined))
  return built
}
