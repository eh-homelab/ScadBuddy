import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { migrate, MIGRATIONS } from '../src/db/migrations.js'
import { PluginStore } from '../src/plugins/registry.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #4828: the migration that drops #2171's mirror triggers and deletes the four old settings keys.

const COPY = '20261026T2201Z_access_copy'
const DROP = MIGRATIONS.find((m) => m.id.endsWith('_access_drop_mirrors'))?.id ?? ''
const skip = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

describe.skipIf(skip !== '')(`the mirror-drop migration${skip}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase({ empty: true }))
    expect(DROP).not.toBe('')
    expect(DROP > COPY).toBe(true)
    await migrate(
      db.sql,
      MIGRATIONS.filter((m) => m.id < DROP),
    )
  })
  afterEach(async () => {
    await drop()
  })

  const setting = async (key: string, value: unknown) => {
    await db.sql`INSERT INTO ai_settings (key, value) VALUES (${key}, ${db.sql.json(value as never)})`
  }

  it('drops both mirror triggers and their functions', async () => {
    await migrate(db.sql, MIGRATIONS)
    expect(
      await db.sql`SELECT tgname FROM pg_trigger WHERE tgrelid IN ('ai_plugins'::regclass, 'ai_settings'::regclass) AND NOT tgisinternal`,
    ).toEqual([])
    expect(
      await db.sql`SELECT proname FROM pg_proc WHERE proname IN ('ai_plugins_mirror_access', 'ai_settings_mirror_access', 'ai_legacy_registry_rows')`,
    ).toEqual([])
  })

  it('deletes the old keys (the four tool-access keys and builtin_tools.claude) and no other', async () => {
    await setting('builtin_tools.scadbuddy', { tool_tiers: {} })
    await setting('printer_camera_enabled', false)
    await setting('http_request_enabled', false)
    await setting('scadbuddy_plugin_enabled', false)
    await setting('model', 'claude-x')
    await setting('builtin_tools.claude', {})
    await migrate(db.sql, MIGRATIONS)
    const keys = await db.sql`SELECT key FROM ai_settings ORDER BY key`
    expect(keys.map((k) => k.key)).toEqual(['model'])
  })

  it('leaves every migration:2171 row as it is', async () => {
    await setting('builtin_tools.scadbuddy', {
      tool_tiers: { send_to_bambuddy: 'read', get_model: 'outward' },
      disabled_tools: ['list_models'],
    })
    const snapshot = () => db.sql`SELECT source, item, access, updated_by, updated_at::text AS t FROM ai_access ORDER BY source, item`
    const before = await snapshot()
    expect(before.length).toBeGreaterThan(0)
    await migrate(db.sql, MIGRATIONS)
    expect(await snapshot()).toEqual(before)
  })

  it('an old-key write after the drop changes no access row', async () => {
    await migrate(db.sql, MIGRATIONS)
    await setting('http_request_enabled', false)
    await setting('scadbuddy_plugin_enabled', false)
    await setting('printer_camera_enabled', false)
    await setting('builtin_tools.scadbuddy', { disabled_tools: ['get_model'] })
    expect(await db.sql`SELECT * FROM ai_access`).toHaveLength(0)
  })

  it('an old-column write after the drop changes no access row', async () => {
    // Through this file only: a later migration (#5014) drops the columns.
    await migrate(
      db.sql,
      MIGRATIONS.filter((m) => m.id <= DROP),
    )
    await db.sql`
      INSERT INTO ai_plugins (name, kind, url, enabled, tool_tiers, disabled_tools)
      VALUES ('hs', 'remote_mcp', 'https://example.test/mcp', true, '{"a":"read"}', '{b}')`
    expect(await db.sql`SELECT * FROM ai_access`).toHaveLength(0)
    // The rest, so today's store can read the row (#780's tables are later files).
    await migrate(db.sql, MIGRATIONS)
    expect((await new PluginStore(db.sql).enabled(undefined)).plugins).toEqual([])
  })
})
