import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { migrate, MIGRATIONS } from '../src/db/migrations.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #5014: the migration that drops ai_plugins' old access columns and deletes the four old keys again.

const MIRRORS = '20261030T1020Z_access_drop_mirrors'
const DROP = MIGRATIONS.find((m) => m.id.endsWith('_access_drop_columns'))?.id ?? ''
const skip = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

describe.skipIf(skip !== '')(`the column-drop migration${skip}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase({ empty: true }))
    expect(DROP).not.toBe('')
    expect(DROP > MIRRORS).toBe(true)
    await migrate(
      db.sql,
      MIGRATIONS.filter((m) => m.id < DROP),
    )
  })
  afterEach(async () => {
    await drop()
  })

  const columns = async () =>
    (await db.sql`SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'ai_plugins' ORDER BY column_name`).map(
      (c) => c.column_name as string,
    )

  it('drops enabled, tool_tiers and disabled_tools and keeps the rest', async () => {
    expect(await columns()).toEqual(expect.arrayContaining(['enabled', 'tool_tiers', 'disabled_tools']))
    await db.sql`INSERT INTO ai_plugins (name, kind, url) VALUES ('hs', 'remote_mcp', 'https://example.test/mcp')`
    await migrate(db.sql, MIGRATIONS)
    const after = await columns()
    expect(after).not.toContain('enabled')
    expect(after).not.toContain('tool_tiers')
    expect(after).not.toContain('disabled_tools')
    expect(after).toEqual(expect.arrayContaining(['name', 'kind', 'url', 'auth_header', 'kek_id', 'last4']))
    expect(await db.sql`SELECT name FROM ai_plugins`).toEqual([{ name: 'hs' }])
  })

  it('deletes the four old keys that a previous-build replica wrote, and no other', async () => {
    const setting = (key: string, value: unknown) => db.sql`INSERT INTO ai_settings (key, value) VALUES (${key}, ${db.sql.json(value as never)})`
    await setting('builtin_tools.scadbuddy', { tool_tiers: {} })
    await setting('printer_camera_enabled', false)
    await setting('http_request_enabled', false)
    await setting('scadbuddy_plugin_enabled', false)
    await setting('model', 'claude-x')
    await migrate(db.sql, MIGRATIONS)
    const keys = await db.sql`SELECT key FROM ai_settings ORDER BY key`
    expect(keys.map((k) => k.key)).toEqual(['model'])
  })
})
