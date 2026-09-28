import { randomUUID } from 'node:crypto'
import postgres from 'postgres'
import { connectDatabase, type Database } from '../../src/db.js'

// A Postgres the tests may create and drop schemas in, the same contract as the
// backend's `requires_postgres` tests (backend/tests/conftest.py): unset, the
// tests that need it skip; CI sets it to a service container.
export const TEST_DATABASE_URL_ENV = 'SCADBUDDY_TEST_DATABASE_URL'
export const TEST_DATABASE_URL = process.env[TEST_DATABASE_URL_ENV]?.trim() || undefined

/** A throwaway schema and a Database whose search_path is it; `drop()` removes both. */
export async function throwawayDatabase(): Promise<{ db: Database; schema: string; drop: () => Promise<void> }> {
  if (!TEST_DATABASE_URL) throw new Error(`${TEST_DATABASE_URL_ENV} is not set`)
  const url = TEST_DATABASE_URL
  const schema = `test_${randomUUID().replaceAll('-', '').slice(0, 12)}`
  const admin = postgres(url, { max: 1, onnotice: () => {} })
  await admin.unsafe(`CREATE SCHEMA "${schema}"`)
  const db = connectDatabase(url, { searchPath: schema })
  return {
    db,
    schema,
    drop: async () => {
      await db.close()
      await admin.unsafe(`DROP SCHEMA "${schema}" CASCADE`)
      await admin.end({ timeout: 5 })
    },
  }
}
