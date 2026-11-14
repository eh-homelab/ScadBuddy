import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { migrate, MIGRATIONS } from './support/migrations.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #5109: the migration that drops the Postgres browser relay's table.

const DROP = MIGRATIONS.find((m) => m.id.endsWith('_drop_bridge_messages'))?.id ?? ''
const skip = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

describe.skipIf(skip !== '')(`the bridge-messages drop${skip}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase({ empty: true }))
    expect(DROP).not.toBe('')
    await migrate(
      db.sql,
      MIGRATIONS.filter((m) => m.id < DROP),
    )
  })
  afterEach(async () => {
    await drop()
  })

  const exists = async () => (await db.sql`SELECT to_regclass('ai_bridge_messages') AS t`)[0]?.t !== null

  it('drops ai_bridge_messages, rows and all', async () => {
    expect(await exists()).toBe(true)
    await db.sql`INSERT INTO ai_bridge_messages (id, body) VALUES (gen_random_uuid(), '{}'::jsonb)`
    await migrate(db.sql, MIGRATIONS)
    expect(await exists()).toBe(false)
  })
})
