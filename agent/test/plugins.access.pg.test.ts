import { randomBytes } from 'node:crypto'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { forwardForRun, PluginForwarder } from '../src/plugins/forwarder.js'
import { loadEnabledPlugins, PluginStore } from '../src/plugins/registry.js'
import { kekFromBase64 } from '../src/secrets.js'
import { turnServers } from '../src/temporal/turnServers.js'
import { type FakeMcp, startFakeMcp } from './support/fakeMcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA } from './support/sessions.js'

// #2171: a remote plugin's enabled flag, tool tiers and disabled tools are its
// `remote:<name>` rows in ai_access, and every gate downstream of the store follows them.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const skip = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

describe.skipIf(skip !== '')(`remote plugins on ai_access${skip}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let store: PluginStore
  let forwarder: PluginForwarder
  let mcp: FakeMcp

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    store = new PluginStore(db.sql)
    forwarder = await PluginForwarder.start()
    mcp = await startFakeMcp({ extraTools: ['a', 'b'] })
    await store.create({ name: 'mem', url: mcp.url }, kek)
  })
  afterEach(async () => {
    await mcp.close()
    await forwarder.close()
    await drop()
  })

  it('a tool set off in ai_access is refused by the forwarder', async () => {
    await store.update('mem', { enabled: true, disabled_tools: ['b'] }, kek)
    // The old column says nothing is disabled (written past the mirror trigger): only ai_access does.
    await db.sql`ALTER TABLE ai_plugins DISABLE TRIGGER ai_plugins_mirror_access`
    await db.sql`UPDATE ai_plugins SET disabled_tools = '{}' WHERE name = 'mem'`
    const [plugin] = (await store.enabled(kek)).plugins
    const registration = forwarder.register(plugin!, '127.0.0.1')
    const client = new Client({ name: 't', version: '0' })
    try {
      await client.connect(new StreamableHTTPClientTransport(new URL(registration.url)))
      const listed = (await client.listTools()).tools.map((t) => t.name)
      expect(listed).toContain('a')
      expect(listed).not.toContain('b')
      await expect(client.callTool({ name: 'b', arguments: { text: 'x' } })).rejects.toThrow(/ScadBuddy: tool "b" is disabled/)
      await client.callTool({ name: 'a', arguments: { text: 'x' } })
      expect(mcp.calls).toEqual(['a:x'])
    } finally {
      await client.close()
      registration.release()
    }
  })

  it('turn_tool_tier answers outward for an unlisted tool and the stored tier for a listed one', async () => {
    await store.update('mem', { enabled: true, tool_tiers: { a: 'read' } }, kek)
    await db.sql`ALTER TABLE ai_plugins DISABLE TRIGGER ai_plugins_mirror_access`
    await db.sql`UPDATE ai_plugins SET tool_tiers = '{}' WHERE name = 'mem'`
    const loopback = () => Promise.resolve(['127.0.0.1'])
    const build = turnServers({ remotePlugins: async () => forwardForRun(await loadEnabledPlugins(store, kek, loopback), forwarder) })
    const { servers } = await build({ session: 's', owner: agentA, signal: new AbortController().signal })
    const [server] = servers
    expect(server?.name).toBe('mem')
    expect(server?.tierOf('mcp__mem__a', {})).toBe('read')
    expect(server?.tierOf('mcp__mem__recall', {})).toBe('outward')
  })
})
