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

  it("setEnabled writes '*' as on or off", async () => {
    await store.setEnabled('remote:mem', false, ctx)
    expect(await store.rows(['remote:mem'])).toMatchObject([{ item: '*', access: 'off' }])
    await store.setEnabled('remote:mem', true, ctx)
    expect(await store.rows(['remote:mem'])).toMatchObject([{ item: '*', access: 'on' }])
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
      () => store.setItem(SCAD, 'get_model', 'on' as never, ctx),
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
  it("replaceToolsOf takes the old keys' lock before it locks rows, so it cannot deadlock with another write (review m1)", async () => {
    await store.setItem(SCAD, 'get_model', 'outward', ctx)
    const other = await db.sql.reserve()
    try {
      // Another writer, in the store's order: the lock first, then the rows.
      await other`BEGIN`
      await other`SELECT pg_advisory_xact_lock(hashtext('ai_access legacy keys'))`
      const replaced = store.replaceToolsOf({ [SCAD]: { get_model: 'read' } }, ctx)
      await new Promise((r) => setTimeout(r, 300))
      await other`UPDATE ai_access SET updated_at = now() WHERE source = ${SCAD} AND item = 'get_model'`
      await other`COMMIT`
      await replaced
    } finally {
      other.release()
    }
    expect(await store.rows([SCAD])).toMatchObject([{ item: 'get_model', access: 'read' }])
  })
})
