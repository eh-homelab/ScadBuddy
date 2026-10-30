import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { migrate, MIGRATIONS } from '../src/db/migrations.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #2171: the migrations that add ai_access and ai_tool_listings and copy the old settings in.

const TABLES = '20261026T2200Z_access'
const COPY = '20261026T2201Z_access_copy'
const skip = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`
const BY = 'migration:2171'

describe.skipIf(skip !== '')(`the tool-access migrations${skip}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase({ empty: true }))
    expect(MIGRATIONS.some((m) => m.id === TABLES)).toBe(true)
    expect(MIGRATIONS.some((m) => m.id === COPY)).toBe(true)
    await migrate(
      db.sql,
      MIGRATIONS.filter((m) => m.id < TABLES),
    )
  })
  afterEach(async () => {
    await drop()
  })

  const setting = async (key: string, value: unknown) => {
    await db.sql`INSERT INTO ai_settings (key, value) VALUES (${key}, ${db.sql.json(value as never)})`
  }
  const rows = async (like = '%') =>
    db.sql`SELECT source, item, access, updated_by FROM ai_access WHERE source LIKE ${like} ORDER BY source, item`
  const plugin = async (name: string, enabled: boolean, tiers: object, disabled: string[]) => {
    await db.sql`
      INSERT INTO ai_plugins (name, kind, url, enabled, tool_tiers, disabled_tools)
      VALUES (${name}, 'remote_mcp', 'https://example.test/mcp', ${enabled}, ${db.sql.json(tiers as never)}, ${disabled})`
  }

  it('copies nothing from a database with none of the old keys', async () => {
    await migrate(db.sql, MIGRATIONS)
    expect(await db.sql`SELECT * FROM ai_access`).toHaveLength(0)
    expect(await db.sql`SELECT * FROM ai_tool_listings`).toHaveLength(0)
  })

  it('copies builtin_tools.scadbuddy: tiers, disabled tools as off, browser tools to builtin:browser', async () => {
    await setting('builtin_tools.scadbuddy', {
      tool_tiers: { get_model: 'outward', browser_click: 'outward', send_to_bambuddy: 'write' },
      disabled_tools: ['get_model', 'list_models'],
    })
    await migrate(db.sql, MIGRATIONS)
    expect(await rows()).toEqual([
      { source: 'builtin:browser', item: 'browser_click', access: 'outward', updated_by: BY },
      { source: 'builtin:scadbuddy', item: 'get_model', access: 'off', updated_by: BY },
      { source: 'builtin:scadbuddy', item: 'list_models', access: 'off', updated_by: BY },
      { source: 'builtin:scadbuddy', item: 'send_to_bambuddy', access: 'write', updated_by: BY },
    ])
  })

  it('copies each ai_plugins row: enabled as *, its tool_tiers, its disabled_tools as off', async () => {
    await plugin('hs', true, { recall: 'read', retain: 'write' }, ['retain'])
    await plugin('gh', false, {}, [])
    await migrate(db.sql, MIGRATIONS)
    expect(await rows('remote:%')).toEqual([
      { source: 'remote:gh', item: '*', access: 'off', updated_by: BY },
      { source: 'remote:hs', item: '*', access: 'on', updated_by: BY },
      { source: 'remote:hs', item: 'recall', access: 'read', updated_by: BY },
      { source: 'remote:hs', item: 'retain', access: 'off', updated_by: BY },
    ])
  })

  it.each([
    ['http_request_enabled', { source: 'builtin:http', item: '*', access: 'off', updated_by: BY }],
    ['printer_camera_enabled', { source: 'builtin:scadbuddy', item: 'get_printer_camera', access: 'off', updated_by: BY }],
    ['scadbuddy_plugin_enabled', { source: 'builtin:own-plugin', item: '*', access: 'off', updated_by: BY }],
  ])('copies %s only when it is stored false', async (key, row) => {
    await setting(key, false)
    await migrate(db.sql, MIGRATIONS)
    expect(await rows()).toEqual([row])
  })

  it.each(['http_request_enabled', 'printer_camera_enabled', 'scadbuddy_plugin_enabled'])(
    'copies nothing for %s stored true',
    async (key) => {
      await setting(key, true)
      await migrate(db.sql, MIGRATIONS)
      expect(await rows()).toEqual([])
    },
  )

  it.each([
    ['not an object', 'x', []],
    ['wrong container types', { tool_tiers: [], disabled_tools: {} }, []],
    [
      'bad tier and non-string disabled',
      { tool_tiers: { a: 'none' }, disabled_tools: [3, 'b'] },
      [{ source: 'builtin:scadbuddy', item: 'b', access: 'off', updated_by: BY }],
    ],
  ])('skips malformed values: %s', async (_name, value, expected) => {
    await setting('builtin_tools.scadbuddy', value)
    await migrate(db.sql, MIGRATIONS)
    expect(await rows()).toEqual(expected)
  })

  it('the camera switch off wins over a stored get_printer_camera tier', async () => {
    await setting('builtin_tools.scadbuddy', { tool_tiers: { get_printer_camera: 'outward' } })
    await setting('printer_camera_enabled', false)
    await migrate(db.sql, MIGRATIONS)
    expect(await rows()).toEqual([{ source: 'builtin:scadbuddy', item: 'get_printer_camera', access: 'off', updated_by: BY }])
  })

  it("skips '*' and empty tool names", async () => {
    await setting('builtin_tools.scadbuddy', { tool_tiers: { '*': 'outward', '': 'read' }, disabled_tools: ['*', ''] })
    await plugin('hs', true, { '*': 'read', '': 'read' }, ['*', ''])
    await migrate(db.sql, MIGRATIONS)
    expect(await rows()).toEqual([{ source: 'remote:hs', item: '*', access: 'on', updated_by: BY }])
  })

  it('leaves the old keys and columns in place', async () => {
    await setting('builtin_tools.scadbuddy', { tool_tiers: { get_model: 'read' } })
    await setting('http_request_enabled', false)
    await plugin('hs', true, { recall: 'read' }, [])
    await migrate(
      db.sql,
      MIGRATIONS.filter((m) => m.id <= COPY),
    )
    const keys = await db.sql`SELECT key FROM ai_settings ORDER BY key`
    expect(keys.map((k) => k.key)).toEqual(['builtin_tools.scadbuddy', 'http_request_enabled'])
    const [p] = await db.sql`SELECT tool_tiers, disabled_tools, enabled FROM ai_plugins WHERE name = 'hs'`
    expect(p).toEqual({ tool_tiers: { recall: 'read' }, disabled_tools: [], enabled: true })
  })

  it('the copy file creates the ai_plugins and ai_settings mirrors before it copies, so no old-replica write falls between them', async () => {
    await migrate(
      db.sql,
      MIGRATIONS.filter((m) => m.id <= COPY),
    )
    const triggers = await db.sql`SELECT tgname FROM pg_trigger WHERE tgrelid = 'ai_plugins'::regclass AND NOT tgisinternal`
    expect(triggers.map((t) => t.tgname)).toEqual(['ai_plugins_mirror_access'])
    expect(MIGRATIONS.some((m) => m.id === '20261026T2358Z_access_plugins_mirror')).toBe(false)
    // The ai_settings mirror (the four old keys) is made in the same file, before the copy too.
    const settingsTriggers = await db.sql`SELECT tgname FROM pg_trigger WHERE tgrelid = 'ai_settings'::regclass AND NOT tgisinternal`
    expect(settingsTriggers.map((t) => t.tgname)).toEqual(['ai_settings_mirror_access'])
    expect(MIGRATIONS.some((m) => m.id === '20261027T0514Z_access_settings_mirror')).toBe(false)
    await plugin('hs', false, {}, ['recall'])
    expect(await rows('remote:%')).toEqual([
      { source: 'remote:hs', item: '*', access: 'off', updated_by: BY },
      { source: 'remote:hs', item: 'recall', access: 'off', updated_by: BY },
    ])
  })

  it('refuses a source item whose access does not fit it', async () => {
    await migrate(db.sql, MIGRATIONS)
    await expect(
      db.sql`INSERT INTO ai_access (source, item, access, updated_by) VALUES ('builtin:http', '*', 'read', 't')`,
    ).rejects.toThrow(/check/)
    await expect(
      db.sql`INSERT INTO ai_access (source, item, access, updated_by) VALUES ('builtin:scadbuddy', 'get_model', 'on', 't')`,
    ).rejects.toThrow(/check/)
  })
})
