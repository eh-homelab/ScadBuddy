import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AccessError, AccessStore } from '../src/access/store.js'
import { BuiltInTools } from '../src/plugins/builtInTools.js'
import { OWN_PLUGIN_NAME } from '../src/plugins/packages/vet.js'
import type { Database } from '../src/db.js'
import type { AuditContext } from '../src/audit/log.js'
import { MemoryAudit } from './support/memoryAudit.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

const ctx: AuditContext = { actor: { kind: 'browser', id: 'browser', label: 'You' }, surface: 'http' }
const SCAD = 'builtin:scadbuddy'

describe.skipIf(!TEST_DATABASE_URL)(`AccessStore${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let audit: MemoryAudit
  let store: AccessStore

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    audit = new MemoryAudit()
    store = new AccessStore(db.sql, audit)
  })
  afterEach(async () => {
    await drop()
  })

  it('stores an override and reads it back by source', async () => {
    await store.setItem(SCAD, 'get_model', 'read', ctx)
    await store.setItem('builtin:http', 'x', 'write', ctx)
    expect(await store.rows([SCAD])).toEqual([{ source: SCAD, item: 'get_model', access: 'read', updated_by: 'browser:browser' }])
    expect(await store.rows([])).toEqual([])
    await store.setItem(SCAD, 'get_model', 'outward')
    expect(await store.rows([SCAD])).toEqual([{ source: SCAD, item: 'get_model', access: 'outward', updated_by: 'system' }])
  })

  it('resetItem deletes the override, so the default applies', async () => {
    await store.setItem(SCAD, 'get_model', 'off', ctx)
    await store.resetItem(SCAD, 'get_model', ctx)
    expect(await store.rows([SCAD])).toEqual([])
  })

  it('resetItemIf deletes the row only while it holds that access', async () => {
    await store.setItem(SCAD, 'get_printer_camera', 'outward', ctx)
    await store.resetItemIf(SCAD, 'get_printer_camera', 'off', ctx)
    expect(await store.rows([SCAD])).toMatchObject([{ item: 'get_printer_camera', access: 'outward' }])
    await store.setItem(SCAD, 'get_printer_camera', 'off', ctx)
    await store.resetItemIf(SCAD, 'get_printer_camera', 'off', ctx)
    expect(await store.rows([SCAD])).toEqual([])
  })

  it('resetItemIf records an audit row only when it removed a row (review of #4838)', async () => {
    await store.setItem(SCAD, 'get_printer_camera', 'outward', ctx)
    const before = audit.entries.length
    await store.resetItemIf(SCAD, 'get_printer_camera', 'off', ctx)
    await store.resetItemIf(SCAD, 'no_row_here', 'off', ctx)
    expect(audit.entries).toHaveLength(before)
    await store.setItem(SCAD, 'get_printer_camera', 'off', ctx)
    await store.resetItemIf(SCAD, 'get_printer_camera', 'off', ctx)
    expect(audit.entries).toHaveLength(before + 2)
    expect(audit.entries.at(-1)).toMatchObject({ outcome: 'ok', detail: 'builtin:scadbuddy/get_printer_camera reset if "off"' })
  })

  it("setEnabled writes '*' as on or off", async () => {
    await store.setEnabled('remote:mem', false, ctx)
    expect(await store.rows(['remote:mem'])).toMatchObject([{ item: '*', access: 'off' }])
    await store.setEnabled('remote:mem', true, ctx)
    expect(await store.rows(['remote:mem'])).toMatchObject([{ item: '*', access: 'on' }])
  })

  it("refuses a '*' row for builtin:scadbuddy, which has no switch (400)", async () => {
    await expect(store.setEnabled(SCAD, false, ctx)).rejects.toBeInstanceOf(AccessError)
    expect(await store.rows([SCAD])).toEqual([])
  })

  it("replaceTools replaces every tool row of the source and leaves '*'", async () => {
    await store.setEnabled('remote:mem', false, ctx)
    await store.setItem('remote:mem', 'a', 'read', ctx)
    await store.setItem('remote:mem', 'b', 'off', ctx)
    await store.replaceTools('remote:mem', { b: 'write', c: 'outward' }, ctx)
    const rows = await store.rows(['remote:mem'])
    expect(rows.map((r) => [r.item, r.access]).sort()).toEqual([
      ['*', 'off'],
      ['b', 'write'],
      ['c', 'outward'],
    ])
  })

  it("forget deletes the source's rows and its ai_tool_listings row", async () => {
    await store.setItem('remote:mem', 'a', 'read', ctx)
    await store.setItem('remote:other', 'a', 'read', ctx)
    await db.sql`INSERT INTO ai_tool_listings (source, tools) VALUES ('remote:mem', '[]'::jsonb), ('remote:other', '[]'::jsonb)`
    await store.forget('remote:mem')
    expect(await store.rows(['remote:mem'])).toEqual([])
    expect(await store.rows(['remote:other'])).toHaveLength(1)
    const listings = await db.sql<{ source: string }[]>`SELECT source FROM ai_tool_listings`
    expect(listings.map((l) => l.source)).toEqual(['remote:other'])
  })

  it("refuses on for a tool, a tier for '*', and an unknown source with AccessError", async () => {
    const refusals = [
      () => store.setItem(SCAD, 'get_model', 'on', ctx),
      () => store.setItem(SCAD, '*', 'read', ctx),
      () => store.setItem('bogus:x', 'a', 'read', ctx),
      () => store.setEnabled('remote:Bad Name', true, ctx),
      () => store.replaceTools('bogus:x', { a: 'read' }, ctx),
      () => store.replaceTools(SCAD, { a: 'on' as never }, ctx),
      () => store.resetItem('bogus:x', 'a', ctx),
    ]
    for (const refuse of refusals) {
      const err = await refuse().catch((e: unknown) => e)
      expect(err).toBeInstanceOf(AccessError)
      expect((err as AccessError).status).toBe(400)
    }
    expect(await db.sql`SELECT 1 FROM ai_access`).toHaveLength(0)
  })

  it('stores on for a package skill and the own plugin\'s subagent', async () => {
    await store.setItem('package:pk', 'skill:review', 'on', ctx)
    await store.setItem('builtin:own-plugin', 'subagent:printer', 'on', ctx)
    await store.setItem('package:pk', 'hook:PreToolUse:0123456789abcdef', 'off', ctx)
    await store.setItem('package:pk', 'builtin:Bash', 'write', ctx)
    expect(await store.rows(['package:pk', 'builtin:own-plugin'])).toEqual([
      { source: 'builtin:own-plugin', item: 'subagent:printer', access: 'on', updated_by: 'browser:browser' },
      { source: 'package:pk', item: 'builtin:Bash', access: 'write', updated_by: 'browser:browser' },
      { source: 'package:pk', item: 'hook:PreToolUse:0123456789abcdef', access: 'off', updated_by: 'browser:browser' },
      { source: 'package:pk', item: 'skill:review', access: 'on', updated_by: 'browser:browser' },
    ])
  })

  it('refuses on for a tool, a tier for a hook, and on outside a package (400)', async () => {
    const refusals = [
      () => store.setItem(SCAD, 'get_model', 'on', ctx),
      () => store.setItem('package:pk/mcp:srv', 'search', 'on', ctx),
      () => store.setItem('package:pk', 'hook:PreToolUse:0123456789abcdef', 'read', ctx),
      () => store.setItem('package:pk', 'skill:review', 'outward', ctx),
      () => store.setItem('builtin:own-plugin', 'subagent:printer', 'write', ctx),
      () => store.setItem('package:pk', 'builtin:Bash', 'on', ctx),
      () => store.setItem(SCAD, 'skill:review', 'on', ctx),
      () => store.setItem('remote:hs', 'skill:x', 'on', ctx),
      // Review M2: a named item needs its name, and a hook its 16 hex digits.
      () => store.setItem('package:pk', 'skill:', 'on', ctx),
      () => store.setItem('package:pk', 'builtin:', 'read', ctx),
      () => store.setItem('package:pk', 'hook:PreToolUse:xyz', 'off', ctx),
      () => store.setItem('package:pk', 'hook::0123456789abcdef', 'off', ctx),
    ]
    for (const refuse of refusals) {
      const err = await refuse().catch((e: unknown) => e)
      expect(err).toBeInstanceOf(AccessError)
      expect((err as AccessError).status).toBe(400)
    }
    expect(await db.sql`SELECT 1 FROM ai_access`).toHaveLength(0)
  })

  // Review I1: only a package's own source and the own plugin hold named items; elsewhere a
  // name with a prefix is a tool's, and takes a tool's access.
  it('a tool named like an item, outside a package, takes a tool access', async () => {
    await store.setItem('remote:hs', 'skill:x', 'off', ctx)
    await store.setItem('remote:hs', 'subagent:y', 'outward', ctx)
    await store.setItem('package:pk/mcp:srv', 'skill:x', 'read', ctx)
    expect((await store.rows(['remote:hs', 'package:pk/mcp:srv'])).map((r) => `${r.source}/${r.item}=${r.access}`)).toEqual([
      'package:pk/mcp:srv/skill:x=read',
      'remote:hs/skill:x=off',
      'remote:hs/subagent:y=outward',
    ])
  })

  it('audits each write as a settings row', async () => {
    await store.setItem(SCAD, 'get_model', 'read', ctx)
    expect(audit.entries).toHaveLength(1)
    expect(audit.entries[0]).toMatchObject({
      kind: 'settings',
      action: 'ai_access',
      outcome: 'ok',
      actor: ctx.actor,
      detail: 'builtin:scadbuddy/get_model = "read"',
    })
  })
  it('replaceToolsOf writes every source in one transaction: a failure on the second leaves the first unchanged', async () => {
    await store.setItem(SCAD, 'get_model', 'outward', ctx)
    await db.sql.unsafe(`
      CREATE FUNCTION test_refuse_browser() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'refused for the test'; END $$;
      CREATE TRIGGER test_refuse_browser BEFORE INSERT ON ai_access
        FOR EACH ROW WHEN (NEW.source = 'builtin:browser') EXECUTE FUNCTION test_refuse_browser();`)
    await expect(store.replaceToolsOf({ [SCAD]: { list_models: 'off' }, 'builtin:browser': { browser_click: 'off' } }, ctx)).rejects.toThrow(
      /refused for the test/,
    )
    expect(await store.rows([SCAD, 'builtin:browser'])).toMatchObject([{ source: SCAD, item: 'get_model', access: 'outward' }])
    expect(audit.entries.at(-1)).toMatchObject({ outcome: 'error' })
  })

  it('replaceToolsOf skips a source whose rows do not change: no audit row names it', async () => {
    await store.replaceToolsOf({ [SCAD]: { get_model: 'outward' }, 'builtin:browser': { browser_click: 'off' } }, ctx)
    expect(audit.entries).toHaveLength(1)
    await store.replaceToolsOf({ [SCAD]: { get_model: 'outward' }, 'builtin:browser': {} }, ctx)
    expect(audit.entries).toHaveLength(2)
    expect(audit.entries[1]!.detail).not.toContain(SCAD)
    expect(audit.entries[1]!.detail).toContain('builtin:browser')
    await store.replaceToolsOf({ [SCAD]: { get_model: 'outward' }, 'builtin:browser': {} }, ctx)
    expect(audit.entries).toHaveLength(2)
    expect(await store.rows([SCAD, 'builtin:browser'])).toMatchObject([{ source: SCAD, item: 'get_model', access: 'outward' }])
  })

  it('BuiltInTools.update writes both registry sources in one transaction', async () => {
    const tools = new BuiltInTools(store)
    const set = tools.named(OWN_PLUGIN_NAME)!
    await tools.update(set, { tool_tiers: { get_model: 'outward' } }, ctx)
    await db.sql.unsafe(`
      CREATE FUNCTION test_refuse_browser() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'refused for the test'; END $$;
      CREATE TRIGGER test_refuse_browser BEFORE INSERT ON ai_access
        FOR EACH ROW WHEN (NEW.source = 'builtin:browser') EXECUTE FUNCTION test_refuse_browser();`)
    await expect(tools.update(set, { tool_tiers: {}, disabled_tools: ['list_models', 'browser_click'] }, ctx)).rejects.toThrow(/refused for the test/)
    expect(await store.rows([SCAD, 'builtin:browser'])).toMatchObject([{ source: SCAD, item: 'get_model', access: 'outward' }])
  })

  it('an access write writes none of the old ai_settings keys (#4828)', async () => {
    const withTools = new AccessStore(db.sql, audit)
    const tools = new BuiltInTools(withTools)
    await tools.update(tools.named(OWN_PLUGIN_NAME)!, { tool_tiers: { get_model: 'outward' }, disabled_tools: ['list_models'] }, ctx)
    await withTools.setItem(SCAD, 'get_printer_camera', 'off', ctx)
    await withTools.setEnabled('builtin:http', false, ctx)
    await withTools.setEnabled('builtin:own-plugin', false, ctx)
    expect(
      await db.sql`SELECT key FROM ai_settings WHERE key IN ('builtin_tools.scadbuddy', 'printer_camera_enabled', 'http_request_enabled', 'scadbuddy_plugin_enabled')`,
    ).toEqual([])
  })

  it('a built-in patch while builtin:browser is off writes no browser tool rows', async () => {
    await store.setItem('builtin:browser', 'browser_click', 'outward', ctx)
    await store.setEnabled('builtin:browser', false, ctx)
    const tools = new BuiltInTools(store)
    await tools.update(tools.named(OWN_PLUGIN_NAME)!, { disabled_tools: ['list_models'] }, ctx)
    expect(await store.rows([SCAD, 'builtin:browser'])).toEqual([
      { source: 'builtin:browser', item: '*', access: 'off', updated_by: 'browser:browser' },
      { source: 'builtin:browser', item: 'browser_click', access: 'outward', updated_by: 'browser:browser' },
      { source: SCAD, item: 'list_models', access: 'off', updated_by: 'browser:browser' },
    ])
  })
})
