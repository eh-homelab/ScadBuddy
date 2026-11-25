import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from './support/database.js'
import { migrate, MIGRATIONS } from './support/migrations.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #7323: the migration that records the runtime of the rows a classic release left with none.

const ID = '20261124T0215Z_session_runtime_not_null'
const skip = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

describe.skipIf(skip !== '')(`the session-runtime migration${skip}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase({ empty: true }))
    expect(MIGRATIONS.some((m) => m.id === ID)).toBe(true)
    await migrate(
      db.sql,
      MIGRATIONS.filter((m) => m.id < ID),
    )
  })
  afterEach(async () => {
    await drop()
  })

  async function session(runtime: string | null, status = 'idle'): Promise<string> {
    const id = randomUUID()
    await db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, runtime)
      VALUES (${id}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', ${status}, 10, 1, ${runtime})`
    return id
  }

  const runtimeOf = async (id: string) => (await db.sql<{ runtime: string | null }[]>`SELECT runtime FROM ai_sessions WHERE id = ${id}`)[0]?.runtime

  it('records `unified` where there was none, and leaves every other row as it was', async () => {
    const idle = await session(null)
    const done = await session(null, 'done')
    const durable = await session('durable', 'done')
    const unified = await session('unified')
    await migrate(db.sql, MIGRATIONS)
    expect(await Promise.all([idle, done, durable, unified].map(runtimeOf))).toEqual(['unified', 'unified', 'durable', 'unified'])
  })

  it('fills an insert that names no runtime, refuses a null, and takes the SQL of the build before it', async () => {
    await migrate(db.sql, MIGRATIONS)
    const id = randomUUID()
    await db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
      VALUES (${id}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'idle', 10, 1)`
    expect(await runtimeOf(id)).toBe('unified')
    await expect(session(null)).rejects.toThrow(/not-null/)
    // That build's insert names `unified` (manager.ts), and its claim coalesces the column.
    const old = await session('unified')
    await db.sql`UPDATE ai_sessions SET status = 'running', runtime = coalesce(runtime, 'unified') WHERE id = ${old}`
    expect(await runtimeOf(old)).toBe('unified')
  })
})
