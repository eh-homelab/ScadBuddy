import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { randomBytes } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import type { HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { forwardForRun, PluginForwarder } from '../src/plugins/forwarder.js'
import { loadEnabledPlugins, PluginError, PluginStore } from '../src/plugins/registry.js'
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
        await db.sql`UPDATE ai_plugins SET url = 'https://hs.example/mcp', enabled = true WHERE name = 'mem'`
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

      it('registers plugins disabled: enabled: true is refused at create', async () => {
        await expect(
          store.create({ name: 'mem', url: 'https://hs.example/mcp', enabled: true }, kek),
        ).rejects.toMatchObject({ status: 400, message: expect.stringMatching(/review its tools, then enable/) as unknown })
        expect(await store.list()).toEqual([])
      })
    })

    describe('the routes', () => {
      function app(testPlugin = vi.fn((): Promise<PluginTest> => Promise.resolve({ ok: true, detail: 'connected', duration_ms: 1, server: null, tools: [], truncated: false }))) {
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
        expect(await refused('http://hs.example/mcp', ['10.0.0.5'])).toMatchObject({
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
        // Released when the turn ended.
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
  },
)
