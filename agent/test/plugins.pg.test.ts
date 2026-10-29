import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { randomBytes } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { migrate, MIGRATIONS } from '../src/db/migrations.js'
import type { HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { forwardForRun, PluginForwarder } from '../src/plugins/forwarder.js'
import { loadEnabledPlugins, PluginError, PluginStore } from '../src/plugins/registry.js'
import { Capabilities } from '../src/access/capabilities.js'
import { ToolListings } from '../src/access/listings.js'
import { remoteProvider } from '../src/access/providers.js'
import type { PluginTest } from '../src/plugins/testConnection.js'
import type { PluginView } from '../src/routes/plugins.js'
import { kekFromBase64, SealError } from '../src/secrets.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// The plugin registry (#297) against real Postgres: the store, the routes on
// top of it, and the session manager loading enabled plugins into a run.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const otherKek = kekFromBase64(randomBytes(32).toString('base64'))
const TOKEN = 'hs-pg-test-token-9999888877776666'
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
/** A same-origin GET through the ingress: browsers send no Origin on it. */
const READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const PUBLIC = () => Promise.resolve(['203.0.113.10'])

describe.skipIf(!TEST_DATABASE_URL)(
  `plugin registry on Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    let store: PluginStore

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      store = new PluginStore(db.sql)
    })
    afterEach(async () => {
      await drop()
    })

    describe('the store', () => {
      it('creates, lists, gets, updates and deletes', async () => {
        const created = await store.create(
          {
            name: 'hindsight',
            url: 'https://hs.example/mcp/bank-1/',
            secret: `Bearer ${TOKEN}`,
            tool_tiers: { recall: 'read' },
            disabled_tools: ['delete_bank'],
          },
          kek,
        )
        expect(created).toMatchObject({
          name: 'hindsight',
          kind: 'remote_mcp',
          enabled: false, // off until reviewed
          auth_header: 'Authorization',
          secret_last4: TOKEN.slice(-4),
          tool_tiers: { recall: 'read' },
          disabled_tools: ['delete_bank'],
          kekId: kek.id,
        })
        await store.create({ name: 'another', url: 'https://other.example/mcp' }, undefined)
        expect((await store.list()).map((p) => p.name)).toEqual(['another', 'hindsight'])

        const updated = await store.update('hindsight', { enabled: true, tool_tiers: { recall: 'read', reflect: 'read' } }, kek)
        expect(updated).toMatchObject({ enabled: true, tool_tiers: { recall: 'read', reflect: 'read' } })
        expect(updated.secret_last4).toBe(TOKEN.slice(-4)) // kept

        expect(await store.delete('another')).toBe(true)
        expect(await store.delete('another')).toBe(false)
        expect(await store.get('another')).toBeUndefined()
      })

      it('refuses a duplicate name, and an update of a missing plugin', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp' }, kek)
        await expect(store.create({ name: 'mem', url: 'https://hs.example/mcp' }, kek)).rejects.toMatchObject({ status: 409 })
        await expect(store.update('nope', { enabled: true }, kek)).rejects.toMatchObject({ status: 404 })
      })

      it('stores the secret sealed, never in the clear, and never in a summary', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp', secret: `Bearer ${TOKEN}` }, kek)
        const [raw] = await db.sql<{ secret_sealed: Buffer; dek_sealed: Buffer }[]>`
          SELECT secret_sealed, dek_sealed FROM ai_plugins WHERE name = 'mem'`
        expect(raw!.secret_sealed.toString('latin1')).not.toContain(TOKEN)
        const dump = await db.sql`SELECT row_to_json(p)::text AS j FROM ai_plugins p`
        expect(JSON.stringify(dump)).not.toContain(TOKEN)
        expect(JSON.stringify(await store.list())).not.toContain(TOKEN)
        expect(JSON.stringify(await store.get('mem'))).not.toContain(TOKEN)
        expect((await store.reveal('mem', kek))?.header).toEqual({ name: 'Authorization', value: `Bearer ${TOKEN}` })
      })

      it('needs a key to save a secret', async () => {
        await expect(
          store.create({ name: 'mem', url: 'https://hs.example/mcp', secret: TOKEN }, undefined),
        ).rejects.toMatchObject({ status: 503 })
      })

      it('binds the secret to the URL: an edited row fails to open instead of sending it elsewhere', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp', secret: TOKEN }, kek)
        await db.sql`UPDATE ai_plugins SET url = 'https://attacker.example/mcp' WHERE name = 'mem'`
        await expect(store.reveal('mem', kek)).rejects.toThrow(SealError)
        await db.sql`UPDATE ai_plugins SET url = 'https://hs.example/mcp' WHERE name = 'mem'`
        await store.update('mem', { enabled: true }, kek)
        await expect(store.reveal('mem', otherKek)).rejects.toThrow(SealError)
        // A plugin that cannot be opened is reported and left out, not loaded without its header.
        const loaded = await store.enabled(otherKek)
        expect(loaded.plugins).toEqual([])
        expect(loaded.problems[0]).toMatch(/plugin mem was not loaded/)
      })

      it('needs the secret again to change the URL or the header name; secret: null removes it', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp', secret: TOKEN }, kek)
        await expect(store.update('mem', { url: 'https://new.example/mcp' }, kek)).rejects.toMatchObject({ status: 409 })
        await expect(store.update('mem', { auth_header: 'X-Api-Key' }, kek)).rejects.toMatchObject({ status: 409 })
        const moved = await store.update('mem', { url: 'https://new.example/mcp', auth_header: 'X-Api-Key', secret: TOKEN }, kek)
        expect(moved).toMatchObject({ url: 'https://new.example/mcp', auth_header: 'X-Api-Key' })
        expect((await store.reveal('mem', kek))?.header).toEqual({ name: 'X-Api-Key', value: TOKEN })
        const cleared = await store.update('mem', { secret: null }, kek)
        expect(cleared).toMatchObject({ auth_header: null, secret_last4: null, kekId: null })
        expect((await store.reveal('mem', kek))?.header).toBeUndefined()
        // With no secret, the URL can change freely.
        expect(await store.update('mem', { url: 'https://third.example/mcp' }, kek)).toMatchObject({
          url: 'https://third.example/mcp',
        })
        await expect(store.update('mem', { auth_header: 'X-Api-Key' }, kek)).rejects.toThrow(/needs a secret/)
      })

      it('validates in the store too, and the table refuses a bad name', async () => {
        await expect(store.create({ name: 'Bad_Name', url: 'https://hs.example/mcp' }, kek)).rejects.toThrow(PluginError)
        await expect(store.create({ name: 'mem', url: 'https://hs.example/mcp?t=1' }, kek)).rejects.toThrow(PluginError)
        await expect(
          db.sql`INSERT INTO ai_plugins (name, kind, url) VALUES ('mem__x', 'remote_mcp', 'https://x')`,
        ).rejects.toThrow(/check constraint/)
      })

      it('re-wraps plugin secrets on key rotation', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp', secret: TOKEN }, kek)
        expect(await store.rewrapFrom(kek, otherKek)).toEqual({ rewrapped: 1, failed: 0 })
        expect((await store.reveal('mem', otherKek))?.header?.value).toBe(TOKEN)
        await expect(store.reveal('mem', kek)).rejects.toThrow(SealError)
      })

      it('loads only enabled plugins whose endpoint passes the egress check now', async () => {
        for (const name of ['good', 'moved', 'off']) {
          await store.create({ name, url: `https://${name}.example/mcp` }, kek)
        }
        await store.update('good', { enabled: true }, kek)
        await store.update('moved', { enabled: true }, kek)
        const loaded = await loadEnabledPlugins(store, kek, (host) =>
          Promise.resolve(host === 'moved.example' ? ['169.254.169.254'] : ['203.0.113.10']),
        )
        // With the address the check passed, for the forwarder to connect to.
        expect(loaded.plugins.map((p) => [p.plugin.name, p.address])).toEqual([['good', '203.0.113.10']])
        expect(loaded.problems).toEqual([expect.stringMatching(/plugin moved was not loaded: .*169\.254\.169\.254/)])
      })

      const accessRows = async (name: string) =>
        db.sql<{ item: string; access: string }[]>`
          SELECT item, access FROM ai_access WHERE source = ${`remote:${name}`} ORDER BY item`

      it('stores tool_tiers, disabled_tools and enabled in ai_access, and keeps writing them to ai_plugins for one release', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp', tool_tiers: { c: 'write' } }, kek)
        expect(await accessRows('mem')).toEqual([
          { item: '*', access: 'off' },
          { item: 'c', access: 'write' },
        ])
        await store.update('mem', { enabled: true, tool_tiers: { a: 'read' }, disabled_tools: ['b'] }, kek)
        expect(await accessRows('mem')).toEqual([
          { item: '*', access: 'on' },
          { item: 'a', access: 'read' },
          { item: 'b', access: 'off' },
        ])
        const [old] = await db.sql<{ enabled: boolean; tool_tiers: unknown; disabled_tools: string[] }[]>`
          SELECT enabled, tool_tiers, disabled_tools FROM ai_plugins WHERE name = 'mem'`
        expect(old).toEqual({ enabled: true, tool_tiers: { a: 'read' }, disabled_tools: ['b'] })
        // Disabled wins over a tier for the same tool.
        await store.update('mem', { tool_tiers: { a: 'read', b: 'read' } }, kek)
        expect(await accessRows('mem')).toEqual([
          { item: '*', access: 'on' },
          { item: 'a', access: 'read' },
          { item: 'b', access: 'off' },
        ])
      })

      it('enabled() lists a plugin whose ai_access says on, with its tiers and disabled tools from ai_access', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp' }, kek)
        await store.create({ name: 'other', url: 'https://other.example/mcp' }, kek)
        await store.update('mem', { enabled: true, tool_tiers: { a: 'read' }, disabled_tools: ['b'] }, kek)
        // The dual-written columns disagree (written past the mirror trigger): ai_access wins, either way.
        await db.sql`ALTER TABLE ai_plugins DISABLE TRIGGER ai_plugins_mirror_access`
        await db.sql`UPDATE ai_plugins SET enabled = false, tool_tiers = '{"a":"outward"}', disabled_tools = '{}' WHERE name = 'mem'`
        await db.sql`UPDATE ai_plugins SET enabled = true WHERE name = 'other'`
        const { plugins } = await store.enabled(kek)
        expect(plugins.map((p) => [p.name, p.toolTiers, p.disabledTools])).toEqual([['mem', { a: 'read' }, ['b']]])
        expect(await store.get('mem')).toMatchObject({ enabled: true, tool_tiers: { a: 'read' }, disabled_tools: ['b'] })
        expect(await store.get('other')).toMatchObject({ enabled: false })
      })

      it('a plugin an older replica creates after the copy reads as on with its tiers', async () => {
        // An older replica writes only the old columns.
        await db.sql`
          INSERT INTO ai_plugins (name, kind, url, enabled, tool_tiers, disabled_tools)
          VALUES ('mem', 'remote_mcp', 'https://hs.example/mcp', true, '{"a":"read","b":"write"}', ${['b']})`
        const { plugins } = await store.enabled(kek)
        expect(plugins.map((p) => [p.name, p.toolTiers, p.disabledTools])).toEqual([['mem', { a: 'read' }, ['b']]])
      })

      it("an older replica's switch-on is seen, and a new replica's partial update keeps it", async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp' }, kek)
        await db.sql`UPDATE ai_plugins SET enabled = true, disabled_tools = ${['x']} WHERE name = 'mem'`
        expect(await store.get('mem')).toMatchObject({ enabled: true, disabled_tools: ['x'] })
        await store.update('mem', { tool_tiers: { a: 'read' } }, kek)
        const [old] = await db.sql<{ enabled: boolean; tool_tiers: unknown; disabled_tools: string[] }[]>`
          SELECT enabled, tool_tiers, disabled_tools FROM ai_plugins WHERE name = 'mem'`
        expect(old).toEqual({ enabled: true, tool_tiers: { a: 'read' }, disabled_tools: ['x'] })
        expect(await accessRows('mem')).toEqual([
          { item: '*', access: 'on' },
          { item: 'a', access: 'read' },
          { item: 'x', access: 'off' },
        ])
      })

      it('delete forgets the plugin\'s access rows', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp', tool_tiers: { a: 'read' } }, kek)
        await store.update('mem', { enabled: true }, kek)
        await db.sql`INSERT INTO ai_tool_listings (source, tools) VALUES ('remote:mem', '[]')`
        expect(await store.delete('mem')).toBe(true)
        expect(await accessRows('mem')).toEqual([])
        expect(await db.sql`SELECT 1 FROM ai_tool_listings WHERE source = 'remote:mem'`).toHaveLength(0)
      })

      it("a URL change drops the old server's listing; any other change keeps it (#4994)", async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp' }, kek)
        await db.sql`INSERT INTO ai_tool_listings (source, tools) VALUES ('remote:mem', '[]')`
        await store.update('mem', { enabled: true, tool_tiers: { a: 'read' } }, kek)
        await store.update('mem', { url: 'https://hs.example/mcp' }, kek)
        expect(await db.sql`SELECT 1 FROM ai_tool_listings WHERE source = 'remote:mem'`).toHaveLength(1)
        await store.update('mem', { url: 'https://new.example/mcp' }, kek)
        expect(await db.sql`SELECT 1 FROM ai_tool_listings WHERE source = 'remote:mem'`).toHaveLength(0)
      })

      const UI_CONTEXT = { actor: { kind: 'browser', id: 'browser', label: 'Browser' }, surface: 'http' } as const
      const rowsBy = (name: string) =>
        db.sql<{ item: string; access: string; updated_by: string }[]>`
          SELECT item, access, updated_by FROM ai_access WHERE source = ${`remote:${name}`} ORDER BY item`
      const oldColumns = async (name: string) => {
        const [row] = await db.sql<{ enabled: boolean; tool_tiers: unknown; disabled_tools: string[] }[]>`
          SELECT enabled, tool_tiers, disabled_tools FROM ai_plugins WHERE name = ${name}`
        return row
      }

      it('setToolAccess writes ai_plugins and ai_access, so an older replica reads the same', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp' }, kek)
        await store.setToolAccess('mem', 'b', 'off', UI_CONTEXT)
        const summary = await store.setToolAccess('mem', 'a', 'read', UI_CONTEXT)
        expect(summary).toMatchObject({ tool_tiers: { a: 'read' }, disabled_tools: ['b'], enabled: false })
        expect(await oldColumns('mem')).toEqual({ enabled: false, tool_tiers: { a: 'read' }, disabled_tools: ['b'] })
        expect(await rowsBy('mem')).toEqual([
          { item: '*', access: 'off', updated_by: 'system' },
          { item: 'a', access: 'read', updated_by: 'browser:browser' },
          { item: 'b', access: 'off', updated_by: 'browser:browser' },
        ])
        // A tier over a disabled tool moves it out of disabled_tools.
        await store.setToolAccess('mem', 'b', 'write', UI_CONTEXT)
        expect(await oldColumns('mem')).toEqual({ enabled: false, tool_tiers: { a: 'read', b: 'write' }, disabled_tools: [] })
        await expect(store.setToolAccess('gone', 'a', 'off', UI_CONTEXT)).rejects.toMatchObject({ name: 'PluginError', status: 404 })
        // A tier needs a name Claude Code keeps; off takes any.
        await expect(store.setToolAccess('mem', 'files.list', 'read', UI_CONTEXT)).rejects.toMatchObject({ status: 400 })
        await store.setToolAccess('mem', 'files.list', 'off', UI_CONTEXT)
        expect((await oldColumns('mem'))?.disabled_tools).toEqual(['files.list'])
      })

      it('setToolAccess refuses the 257th change of one kind in the page\'s words, not a field the page never shows (final review M7)', async () => {
        const names = Array.from({ length: 256 }, (_, i) => `t${i}`)
        await store.create(
          { name: 'mem', url: 'https://hs.example/mcp', disabled_tools: names, tool_tiers: Object.fromEntries(names.map((n) => [`r${n}`, 'read'])) },
          kek,
        )
        const off = store.setToolAccess('mem', 'x', 'off', UI_CONTEXT)
        await expect(off).rejects.toMatchObject({ name: 'PluginError', status: 400 })
        await expect(off).rejects.toThrow('At most 256 tools of plugin "mem" can be off: reset one of them first')
        const read = store.setToolAccess('mem', 'x', 'read', UI_CONTEXT)
        await expect(read).rejects.toThrow('At most 256 tools of plugin "mem" can have an access other than off or the default: reset one of them first')
        // A change to a tool already counted, or a reset, still goes through.
        await store.setToolAccess('mem', 'rt0', 'write', UI_CONTEXT)
        await store.setToolAccess('mem', 't0', null, UI_CONTEXT)
        await store.setToolAccess('mem', 'x', 'off', UI_CONTEXT)
      })

      it("setToolAccess attributes only the changed row: the others keep their author and time", async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp', tool_tiers: { a: 'read' }, disabled_tools: ['b'] }, kek)
        await db.sql`UPDATE ai_access SET updated_by = 'migration:2171', updated_at = now() - interval '1 day'
          WHERE source = 'remote:mem' AND item = 'a'`
        const times = () =>
          db.sql<{ item: string; updated_by: string; t: string }[]>`
            SELECT item, updated_by, updated_at::text AS t FROM ai_access WHERE source = 'remote:mem' ORDER BY item`
        const [, a0, b0] = await times()
        await store.setToolAccess('mem', 'c', 'write', UI_CONTEXT)
        const [, a1, b1, c1] = await times()
        expect(a1).toEqual(a0)
        expect(a1?.updated_by).toBe('migration:2171')
        expect(b1).toEqual(b0)
        expect(c1).toMatchObject({ item: 'c', updated_by: 'browser:browser' })
      })

      it('setToolAccess keeps a tool named __proto__ as its own key', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp' }, kek)
        await store.setToolAccess('mem', '__proto__', 'read', UI_CONTEXT)
        const [row] = await db.sql<{ j: string }[]>`SELECT tool_tiers::text AS j FROM ai_plugins WHERE name = 'mem'`
        expect(row?.j).toBe('{"__proto__": "read"}')
        expect(await rowsBy('mem')).toContainEqual({ item: '__proto__', access: 'read', updated_by: 'browser:browser' })
        // A write to another tool keeps it, and the summary names it.
        const summary = await store.setToolAccess('mem', 'other', 'write', UI_CONTEXT)
        expect(Object.hasOwn(summary.tool_tiers, '__proto__')).toBe(true)
        expect(summary.tool_tiers['__proto__']).toBe('read')
        const [after] = await db.sql<{ j: string }[]>`SELECT tool_tiers::text AS j FROM ai_plugins WHERE name = 'mem'`
        expect(after?.j).toBe('{"other": "write", "__proto__": "read"}')
        expect(await rowsBy('mem')).toContainEqual({ item: '__proto__', access: 'read', updated_by: 'browser:browser' })
      })

      it("a source switch through the tree records the UI's actor", async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp' }, kek)
        const caps = new Capabilities([
          remoteProvider({ plugins: store, listings: new ToolListings(db.sql, db.lockClient), refresh: () => Promise.reject(new Error('not called')) }),
        ])
        expect((await caps.setEnabled('remote:mem', true, UI_CONTEXT)).enabled).toBe(true)
        expect(await rowsBy('mem')).toEqual([{ item: '*', access: 'on', updated_by: 'browser:browser' }])
      })

      it('setToolAccess null removes the tool from both', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp', tool_tiers: { a: 'read' }, disabled_tools: ['b'] }, kek)
        await store.setToolAccess('mem', 'a', null, UI_CONTEXT)
        const summary = await store.setToolAccess('mem', 'b', null, UI_CONTEXT)
        expect(summary).toMatchObject({ tool_tiers: {}, disabled_tools: [] })
        expect(await oldColumns('mem')).toEqual({ enabled: false, tool_tiers: {}, disabled_tools: [] })
        expect(await rowsBy('mem')).toEqual([{ item: '*', access: 'off', updated_by: 'system' }])
      })

      it('registers plugins disabled: enabled: true is refused at create', async () => {
        await expect(
          store.create({ name: 'mem', url: 'https://hs.example/mcp', enabled: true }, kek),
        ).rejects.toMatchObject({ status: 400, message: expect.stringMatching(/review its tools, then enable/) as unknown })
        expect(await store.list()).toEqual([])
      })
    })

    describe('the routes', () => {
      function app(testPlugin = vi.fn((): Promise<PluginTest> => Promise.resolve({ ok: true, detail: 'connected', duration_ms: 1, server: null, tools: [], truncated: false, stored: [] }))) {
        return {
          testPlugin,
          app: createApp({
            database: { ping: () => Promise.resolve(true), ready: () => db.ready() },
            backend: () => Promise.resolve(true),
            kek: { ok: true, kek },
            credentials: new MemoryCredentials(),
            plugins: store,
            testPlugin,
            testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
            remoteAddress: () => '10.0.0.7',
            origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
            resolveHost: PUBLIC,
          }),
        }
      }
      const json = (method: string, body: unknown) => ({
        method,
        headers: { ...UI, 'content-type': 'application/json' },
        body: JSON.stringify(body),
      })

      it('registers, lists, updates, tests and removes, never returning the secret', async () => {
        const { app: a, testPlugin } = app()
        const bodies: string[] = []
        const created = await a.request(
          '/api/v1/ai/plugins',
          json('POST', {
            name: 'hindsight',
            url: 'https://hs.example/mcp/bank-1/',
            secret: `Bearer ${TOKEN}`,
            tool_tiers: { recall: 'read' },
          }),
        )
        expect(created.status).toBe(201)
        bodies.push(await created.clone().text())
        const view = (await created.json()) as PluginView
        expect(view).toMatchObject({
          name: 'hindsight',
          enabled: false,
          tool_prefix: 'mcp__hindsight__',
          auth: { header: 'Authorization', last4: TOKEN.slice(-4) },
          usable: true,
          problem: null,
          tool_tiers: { recall: 'read' },
        })

        const list = await a.request('/api/v1/ai/plugins', { headers: READ })
        expect(list.status).toBe(200)
        bodies.push(await list.text())
        const one = await a.request('/api/v1/ai/plugins/hindsight', { headers: READ })
        bodies.push(await one.text())

        const patched = await a.request('/api/v1/ai/plugins/hindsight', json('PATCH', { enabled: true }))
        expect(patched.status).toBe(200)
        bodies.push(await patched.text())
        expect(JSON.parse(bodies.at(-1)!)).toMatchObject({ enabled: true })

        const tested = await a.request('/api/v1/ai/plugins/hindsight/test', { method: 'POST', headers: UI })
        expect(tested.status).toBe(200)
        bodies.push(await tested.text())
        // The test got the opened plugin (with its header), the route did not return it.
        expect(testPlugin).toHaveBeenCalledWith(
          expect.objectContaining({ name: 'hindsight', header: { name: 'Authorization', value: `Bearer ${TOKEN}` } }),
          '203.0.113.10',
        )

        for (const body of bodies) expect(body).not.toContain(TOKEN)

        expect((await a.request('/api/v1/ai/plugins/hindsight', { method: 'DELETE', headers: UI })).status).toBe(204)
        expect((await a.request('/api/v1/ai/plugins/hindsight', { headers: READ })).status).toBe(404)
        expect((await a.request('/api/v1/ai/plugins/hindsight', { method: 'DELETE', headers: UI })).status).toBe(404)
      })

      it('answers 400 / 409 with the reason', async () => {
        const { app: a } = app()
        const post = (body: unknown) => a.request('/api/v1/ai/plugins', json('POST', body))
        expect((await post({ name: 'x', url: 'https://hs.example/mcp' })).status).toBe(400)
        expect((await post({ name: 'mem', url: 'https://hs.example/mcp', extra: 1 })).status).toBe(400)
        expect((await post({ name: 'mem', url: 'https://hs.example/mcp', tool_tiers: { a: 'root' } })).status).toBe(400)
        expect((await post({ name: 'mem', url: 'https://hs.example/mcp', auth_header: 'X-Key' })).status).toBe(400)
        expect((await post({ name: 'mem', url: 'https://hs.example/mcp' })).status).toBe(201)
        expect((await post({ name: 'mem', url: 'https://hs.example/mcp' })).status).toBe(409)
        const res = await a.request('/api/v1/ai/plugins/mem', json('PATCH', { url: 'nope' }))
        expect(res.status).toBe(400)
      })

      it('refuses an endpoint that is link-local, metadata, or plain http off loopback; stores nothing', async () => {
        const refused = async (url: string, resolved: string[]) => {
          const a = createApp({
            database: { ping: () => Promise.resolve(true), ready: () => db.ready() },
            backend: () => Promise.resolve(true),
            kek: { ok: true, kek },
            credentials: new MemoryCredentials(),
            plugins: store,
            testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
            remoteAddress: () => '10.0.0.7',
            origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
            resolveHost: () => Promise.resolve(resolved),
          })
          const res = await a.request('/api/v1/ai/plugins', json('POST', { name: 'mem', url }))
          return { status: res.status, detail: ((await res.json()) as { detail: string }).detail }
        }
        expect(await refused('https://169.254.169.254/mcp', [])).toMatchObject({ status: 400 })
        expect(await refused('https://hs.example/mcp', ['169.254.169.254'])).toMatchObject({
          status: 400,
          detail: expect.stringMatching(/resolves to 169\.254\.169\.254/) as unknown,
        })
        expect(await refused('http://hs.example/mcp', ['203.0.113.5'])).toMatchObject({
          status: 400,
          detail: expect.stringMatching(/must be https/) as unknown,
        })
        expect(await store.list()).toEqual([])
      })

      it('checks the endpoint again before a test', async () => {
        await store.create({ name: 'mem', url: 'https://hs.example/mcp' }, kek)
        const testPlugin = vi.fn()
        const a = createApp({
          database: { ping: () => Promise.resolve(true), ready: () => db.ready() },
          backend: () => Promise.resolve(true),
          kek: { ok: true, kek },
          credentials: new MemoryCredentials(),
          plugins: store,
          testPlugin,
          testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
          remoteAddress: () => '10.0.0.7',
          origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
          resolveHost: () => Promise.resolve(['169.254.169.254']),
        })
        const res = await a.request('/api/v1/ai/plugins/mem/test', { method: 'POST', headers: UI })
        expect(res.status).toBe(400)
        expect(testPlugin).not.toHaveBeenCalled()
      })
    })

    describe('the session manager', () => {
      let forwarder: PluginForwarder
      beforeEach(async () => {
        forwarder = await PluginForwarder.start()
      })
      afterEach(async () => {
        await forwarder.close()
      })

      it('loads the enabled plugins into each turn through the forwarder, reports the unavailable ones, and redacts secrets', async () => {
        await store.create(
          { name: 'hindsight', url: 'https://hs.example/mcp/', secret: `Bearer ${TOKEN}`, tool_tiers: { recall: 'read' } },
          kek,
        )
        await store.update('hindsight', { enabled: true }, kek)
        await store.create({ name: 'moved', url: 'https://moved.example/mcp/' }, kek)
        await store.update('moved', { enabled: true }, kek)
        await store.create({ name: 'off', url: 'https://off.example/mcp/' }, kek)
        // The "model" echoes the BARE token, without its Bearer prefix.
        const scripted = scriptedRunner(() => ({ reply: `the token is ${TOKEN}` }))
        const runs: HarnessRun[] = []
        const runner = (run: HarnessRun): AsyncIterable<SDKMessage> =>
          (async function* () {
            runs.push(run)
            // Claude Code's init: the hindsight server failed to connect.
            yield {
              type: 'system',
              subtype: 'init',
              mcp_servers: [{ name: 'hindsight', status: 'failed' }],
            } as unknown as SDKMessage
            yield* scripted.runner(run)
          })()
        const resolve = (host: string) => Promise.resolve(host === 'moved.example' ? ['169.254.169.254'] : ['203.0.113.10'])
        const m = manager({
          sql: db.sql,
          paths: await tempPaths(),
          run: runner,
          remotePlugins: async () => forwardForRun(await loadEnabledPlugins(store, kek, resolve), forwarder),
        })
        const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
        await turn!.done
        const run = runs[0]
        expect(run?.remotePlugins?.map((p) => [p.name, p.toolTiers])).toEqual([['hindsight', { recall: 'read' }]])
        // Claude Code gets the forwarder's loopback URL, never the endpoint or the secret.
        expect(run?.remotePlugins?.[0]?.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/p\//)
        expect(JSON.stringify(run?.remotePlugins)).not.toContain(TOKEN)
        // Held while the process is warm (spec 2026-10-13 §5.3): released with it.
        expect(forwarder.size).toBe(1)
        await m.warm.evict(session.id, 'the test')
        expect(forwarder.size).toBe(0)

        const logged = (
          await db.sql<{ event: string }[]>`
            SELECT event FROM ai_session_events WHERE session_id = ${session.id} ORDER BY seq`
        ).map((e) => JSON.parse(e.event) as { type: string; code?: string; message?: string })
        const unavailable = logged.filter((e) => e.type === 'error' && e.code === 'plugin_unavailable')
        expect(unavailable.map((e) => e.message)).toEqual([
          expect.stringMatching(/plugin moved was not loaded: .*169\.254\.169\.254/),
          expect.stringMatching(/plugin hindsight is not available in this turn: its MCP server is failed/),
        ])
        expect(JSON.stringify(logged)).toContain('[redacted]')
        expect(JSON.stringify(logged)).not.toContain(TOKEN)
      })
    })

    describe('before the access migration', () => {
      let empty: Database
      let dropEmpty: () => Promise<void>
      beforeEach(async () => {
        ;({ db: empty, drop: dropEmpty } = await throwawayDatabase({ empty: true }))
      })
      afterEach(async () => {
        await dropEmpty()
      })

      it('a plugin switched on before the migration stays on', async () => {
        await migrate(
          empty.sql,
          MIGRATIONS.filter((m) => m.id < '20261026T2200Z_access'),
        )
        await empty.sql`
          INSERT INTO ai_plugins (name, kind, url, enabled, tool_tiers, disabled_tools)
          VALUES ('mem', 'remote_mcp', 'https://hs.example/mcp', true, '{"a":"read"}', ${['b']})`
        await migrate(empty.sql, MIGRATIONS)
        const { plugins } = await new PluginStore(empty.sql).enabled(kek)
        expect(plugins.map((p) => [p.name, p.toolTiers, p.disabledTools])).toEqual([['mem', { a: 'read' }, ['b']]])
      })
    })
  },
)
