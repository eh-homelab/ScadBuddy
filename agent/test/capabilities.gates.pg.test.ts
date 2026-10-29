import { randomBytes } from 'node:crypto'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { toStoredTools, ToolListings } from '../src/access/listings.js'
import { AccessStore } from '../src/access/store.js'
import { createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { buildHarnessOptions } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { PluginForwarder } from '../src/plugins/forwarder.js'
import { PluginStore } from '../src/plugins/registry.js'
import { CAPABILITIES_PATH } from '../src/routes/capabilities.js'
import { kekFromBase64 } from '../src/secrets.js'
import { type FakeMcp, startFakeMcp } from './support/fakeMcp.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #2171, #778 acceptance: a remote tool set off through /api/v1/ai/capabilities is what the
// forwarder enforces: gone from tools/list and refused on tools/call. A tier set there is
// what the classic seam reads.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }
const skip = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

describe.skipIf(skip !== '')(`remote access set through the capabilities routes${skip}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let forwarder: PluginForwarder
  let mcp: FakeMcp

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    forwarder = await PluginForwarder.start()
    mcp = await startFakeMcp({ extraTools: ['a', 'b'] })
  })
  afterEach(async () => {
    await mcp.close()
    await forwarder.close()
    await drop()
  })

  async function rig() {
    const store = new PluginStore(db.sql)
    const listings = new ToolListings(db.sql, db.lockClient)
    const { url } = await store.create({ name: 'hs', url: mcp.url }, kek)
    await listings.exclusive('hs', (put) => put(url, toStoredTools([{ name: 'a' }, { name: 'b' }]), {}))
    const app = createApp({
      access: new AccessStore(db.sql),
      database: { ping: () => Promise.resolve(true), ready: () => db.ready() },
      backend: () => Promise.resolve(true),
      kek: { ok: true, kek },
      credentials: new MemoryCredentials(),
      plugins: store,
      listings,
      testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    const put = (path: string, body: unknown) =>
      app.request(`${CAPABILITIES_PATH}/${encodeURIComponent('remote:hs')}${path}`, { method: 'PUT', headers: UI, body: JSON.stringify(body) })
    expect((await put('', { enabled: true })).status).toBe(200)
    return { store, put }
  }

  it("a remote tool set off in Settings is absent from the forwarder's tools/list and refused on tools/call", async () => {
    const { store, put } = await rig()
    const off = await put('/items/b', { access: 'off' })
    expect(off.status).toBe(200)

    const [plugin] = (await store.enabled(kek)).plugins
    const registration = forwarder.register(plugin!, '127.0.0.1')
    const client = new Client({ name: 't', version: '0' })
    try {
      await client.connect(new StreamableHTTPClientTransport(new URL(registration.url)))
      const listed = (await client.listTools()).tools.map((t) => t.name)
      expect(listed).toContain('a')
      expect(listed).not.toContain('b')
      await expect(client.callTool({ name: 'b', arguments: { text: 'x' } })).rejects.toThrow(/tool "b" is disabled/)
      await client.callTool({ name: 'a', arguments: { text: 'x' } })
      expect(mcp.calls).toEqual(['a:x'])
    } finally {
      await client.close()
      registration.release()
    }
  })

  it('a remote tool given read in Settings is allowed at the classic seam; one left at the default asks first', async () => {
    const { store, put } = await rig()
    expect((await put('/items/a', { access: 'read' })).status).toBe(200)
    const [plugin] = (await store.enabled(kek)).plugins
    const registration = forwarder.register(plugin!, '127.0.0.1')
    try {
      const options = buildHarnessOptions({
        paths: { stateDir: '/var/lib/scadbuddy-agent' },
        credential: { kind: 'anthropic_api_key', secret: 'sk-ant-api03-unit-test-key-000011112222' },
        prompt: 'hi',
        remotePlugins: [{ name: 'hs', url: registration.url, toolTiers: plugin!.toolTiers, disabledTools: plugin!.disabledTools }],
      })
      const ask = (name: string) => options.canUseTool!(name, {}, { signal: new AbortController().signal, toolUseID: 't', requestId: 'r' })
      expect(await ask('mcp__hs__a')).toMatchObject({ behavior: 'allow' })
      expect(await ask('mcp__hs__b')).toMatchObject({ behavior: 'deny', message: expect.stringMatching(/approval/) as unknown })
    } finally {
      registration.release()
    }
  })
})
