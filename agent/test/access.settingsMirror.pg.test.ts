import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AccessStore } from '../src/access/store.js'
import type { AuditContext } from '../src/audit/log.js'
import type { Database } from '../src/db.js'
import { BuiltInTools } from '../src/plugins/builtInTools.js'
import { OWN_PLUGIN_NAME } from '../src/plugins/packages/vet.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #2171 (#4756), rolling deploy: the four old ai_settings keys are dual-written by the new
// code (old key first, then ai_access) and an older replica's write of them is mirrored into
// ai_access by a trigger. Both go next release (#4828).

const skip = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`
const ctx: AuditContext = { actor: { kind: 'browser', id: 'browser', label: 'You' }, surface: 'http' }
const BY = 'migration:2171'
const SOURCES = ['builtin:scadbuddy', 'builtin:browser', 'builtin:http', 'builtin:own-plugin']
const OUTWARD = ALL_TOOLS.find((t) => t.risk === 'outward' && !t.name.startsWith('browser_'))!.name

describe.skipIf(skip !== '')(`the old ai_settings keys during a rolling deploy${skip}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let store: AccessStore

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new AccessStore(db.sql, undefined, ALL_TOOLS)
  })
  afterEach(async () => {
    await drop()
  })

  /** What an older replica's SettingsStore.set writes. */
  const oldWrite = async (key: string, value: unknown) => {
    await db.sql`
      INSERT INTO ai_settings (key, value) VALUES (${key}, ${db.sql.json(value as never)})
      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()`
  }
  const access = async () =>
    (await db.sql`SELECT source, item, access, updated_by FROM ai_access WHERE source IN ${db.sql(SOURCES)} ORDER BY source, item`).map(
      (r) => ({ ...r }),
    )
  const sorted = <T extends { source: string; item: string }>(rows: T[]): T[] =>
    [...rows].sort((a, b) => (a.source + '/' + a.item < b.source + '/' + b.item ? -1 : 1))
  const setting = async (key: string) => (await db.sql`SELECT value FROM ai_settings WHERE key = ${key}`)[0]?.value as unknown

  it('mirrors an old builtin_tools.scadbuddy write: tiers, disabled tools as off, browser tools to builtin:browser', async () => {
    await oldWrite('builtin_tools.scadbuddy', { tool_tiers: { get_model: 'outward', browser_click: 'outward' }, disabled_tools: ['list_models'] })
    expect(await access()).toEqual([
      { source: 'builtin:browser', item: 'browser_click', access: 'outward', updated_by: BY },
      { source: 'builtin:scadbuddy', item: 'get_model', access: 'outward', updated_by: BY },
      { source: 'builtin:scadbuddy', item: 'list_models', access: 'off', updated_by: BY },
    ])
    await oldWrite('builtin_tools.scadbuddy', { tool_tiers: { get_model: 'write' }, disabled_tools: [] })
    expect(await access()).toEqual([{ source: 'builtin:scadbuddy', item: 'get_model', access: 'write', updated_by: BY }])
  })

  it('mirrors an old printer_camera_enabled write; the camera off wins over a tier', async () => {
    await oldWrite('builtin_tools.scadbuddy', { tool_tiers: { get_printer_camera: 'outward' }, disabled_tools: [] })
    await oldWrite('printer_camera_enabled', false)
    expect(await access()).toEqual([{ source: 'builtin:scadbuddy', item: 'get_printer_camera', access: 'off', updated_by: BY }])
    await oldWrite('printer_camera_enabled', true)
    expect(await access()).toEqual([{ source: 'builtin:scadbuddy', item: 'get_printer_camera', access: 'outward', updated_by: BY }])
  })

  it('a camera turned off by the new code is turned on by an old replica', async () => {
    await store.setItem('builtin:scadbuddy', 'get_printer_camera', 'off', ctx)
    expect(await setting('printer_camera_enabled')).toBe(false)
    await oldWrite('printer_camera_enabled', true)
    expect(await access()).toEqual([])
  })

  it.each([
    ['http_request_enabled', 'builtin:http'],
    ['scadbuddy_plugin_enabled', 'builtin:own-plugin'],
  ])('mirrors an old %s write as the source switch', async (key, source) => {
    await oldWrite(key, false)
    expect(await access()).toEqual([{ source, item: '*', access: 'off', updated_by: BY }])
    await oldWrite(key, true)
    expect(await access()).toEqual([{ source, item: '*', access: 'on', updated_by: BY }])
  })

  it('a new-code write leaves both stores agreeing, with raised tiers only in the old key', async () => {
    const tools = new BuiltInTools(store)
    const set = tools.named(OWN_PLUGIN_NAME)!
    await tools.update(set, { tool_tiers: { get_model: 'outward', [OUTWARD]: 'read' }, disabled_tools: ['list_models', 'browser_click'] }, ctx)
    await store.setItem('builtin:scadbuddy', 'get_printer_camera', 'off', ctx)
    await store.setEnabled('builtin:http', false, ctx)
    await store.setEnabled('builtin:own-plugin', false, ctx)
    expect(await setting('builtin_tools.scadbuddy')).toEqual({ tool_tiers: { get_model: 'outward' }, disabled_tools: ['browser_click', 'list_models'] })
    expect(await setting('printer_camera_enabled')).toBe(false)
    expect(await setting('http_request_enabled')).toBe(false)
    expect(await setting('scadbuddy_plugin_enabled')).toBe(false)
    expect(await access()).toEqual(sorted([
      { source: 'builtin:browser', item: 'browser_click', access: 'off', updated_by: 'browser:browser' },
      { source: 'builtin:http', item: '*', access: 'off', updated_by: 'browser:browser' },
      { source: 'builtin:own-plugin', item: '*', access: 'off', updated_by: 'browser:browser' },
      { source: 'builtin:scadbuddy', item: OUTWARD, access: 'read', updated_by: 'browser:browser' },
      { source: 'builtin:scadbuddy', item: 'get_model', access: 'outward', updated_by: 'browser:browser' },
      { source: 'builtin:scadbuddy', item: 'get_printer_camera', access: 'off', updated_by: 'browser:browser' },
      { source: 'builtin:scadbuddy', item: 'list_models', access: 'off', updated_by: 'browser:browser' },
    ]))
    await store.resetItem('builtin:scadbuddy', 'get_printer_camera', ctx)
    await store.setEnabled('builtin:http', true, ctx)
    expect(await setting('printer_camera_enabled')).toBe(true)
    expect(await setting('http_request_enabled')).toBe(true)
  })

  it('switching builtin:browser off writes no browser tool into builtin_tools.scadbuddy', async () => {
    await store.setItem('builtin:browser', 'browser_click', 'outward', ctx)
    const before = await setting('builtin_tools.scadbuddy')
    expect(before).toEqual({ tool_tiers: { browser_click: 'outward' }, disabled_tools: [] })
    await store.setEnabled('builtin:browser', false, ctx)
    expect(await setting('builtin_tools.scadbuddy')).toEqual(before)
    expect(await access()).toEqual([
      { source: 'builtin:browser', item: '*', access: 'off', updated_by: 'browser:browser' },
      { source: 'builtin:browser', item: 'browser_click', access: 'outward', updated_by: 'browser:browser' },
    ])
    expect(await db.sql`SELECT 1 FROM ai_access WHERE updated_by = ${BY}`).toHaveLength(0)
  })

  it('a lowered tier the new code wrote is kept by the mirror of an old write', async () => {
    const tools = new BuiltInTools(store)
    const set = tools.named(OWN_PLUGIN_NAME)!
    await tools.update(set, { tool_tiers: { [OUTWARD]: 'read', get_model: 'outward' } }, ctx)
    // An older replica, reading the old key, disables a tool.
    await oldWrite('builtin_tools.scadbuddy', { tool_tiers: { get_model: 'outward' }, disabled_tools: ['list_models'] })
    expect(await access()).toEqual(sorted([
      { source: 'builtin:scadbuddy', item: OUTWARD, access: 'read', updated_by: 'browser:browser' },
      { source: 'builtin:scadbuddy', item: 'get_model', access: 'outward', updated_by: 'browser:browser' },
      { source: 'builtin:scadbuddy', item: 'list_models', access: 'off', updated_by: BY },
    ]))
    expect((await tools.overrides(set)).tool_tiers).toEqual({ [OUTWARD]: 'read', get_model: 'outward' })
  })
})
