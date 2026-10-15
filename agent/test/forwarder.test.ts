import { createServer, type IncomingHttpHeaders, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, describe, expect, it } from 'vitest'
import { PluginForwarder, type SessionLedger } from '../src/plugins/forwarder.js'
import type { RemotePlugin } from '../src/plugins/registry.js'

// The session DELETE the forwarder sends for a process that ended (spec 2026-10-13 §5.3 E7).

type Seen = { method: string; headers: IncomingHttpHeaders }

let servers: Server[] = []
let forwarders: PluginForwarder[] = []
afterEach(async () => {
  await Promise.all(forwarders.map((f) => f.close()))
  await Promise.all(servers.map((s) => new Promise<void>((r) => (s.closeAllConnections(), s.close(() => r())))))
  servers = []
  forwarders = []
})

/** An upstream that issues session `s-1` to a POST and records every request; `hang` never answers a DELETE. */
async function upstream(options: { hang?: boolean } = {}) {
  const seen: Seen[] = []
  const server = createServer((req, res) => {
    seen.push({ method: req.method ?? '', headers: req.headers })
    if (req.method === 'DELETE') {
      if (!options.hang) res.writeHead(200).end()
      return
    }
    req.resume()
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'application/json', 'mcp-session-id': 's-1' })
      res.end(JSON.stringify({ jsonrpc: '2.0', id: 1, result: {} }))
    })
  })
  servers.push(server)
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r))
  const plugin: RemotePlugin = {
    name: 'memory',
    url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/mcp`,
    header: { name: 'X-Api-Key', value: 'plugin-secret-1' },
    toolTiers: {},
    disabledTools: [],
  }
  return { seen, plugin }
}

function ledger() {
  const events: string[] = []
  const sessions: SessionLedger = {
    seen: async (plugin, id) => void events.push(`seen ${plugin} ${id}`),
    ended: async (plugin, id) => void events.push(`ended ${plugin} ${id}`),
  }
  return { events, sessions }
}

const post = (url: string, headers: Record<string, string> = {}) =>
  fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream', ...headers },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} }),
  })

describe('Registration.terminate', () => {
  it("terminate sends the plugin's header", async () => {
    const { seen, plugin } = await upstream()
    const { events, sessions } = ledger()
    const forwarder = await PluginForwarder.start({ sessions })
    forwarders.push(forwarder)
    const registration = forwarder.register(plugin, '127.0.0.1', { record: true })
    expect((await post(registration.url)).status).toBe(200)
    expect(events).toEqual(['seen memory s-1'])

    await registration.terminate()
    const deletes = seen.filter((s) => s.method === 'DELETE')
    expect(deletes).toHaveLength(1)
    expect(deletes[0]?.headers['mcp-session-id']).toBe('s-1')
    expect(deletes[0]?.headers['x-api-key']).toBe('plugin-secret-1')
    expect(events).toEqual(['seen memory s-1', 'ended memory s-1'])
    // The route is released.
    expect((await post(registration.url)).status).toBe(404)
  })

  it('a registration that is not recorded leaves no row, and is still terminated', async () => {
    const { seen, plugin } = await upstream()
    const { events, sessions } = ledger()
    const forwarder = await PluginForwarder.start({ sessions })
    forwarders.push(forwarder)
    const registration = forwarder.register(plugin, '127.0.0.1')
    await post(registration.url)
    await registration.terminate()
    expect(events).toEqual([])
    expect(seen.filter((s) => s.method === 'DELETE')).toHaveLength(1)
  })

  it("sends the DELETE to the pinned address, never resolving the plugin's name", async () => {
    const { seen, plugin } = await upstream()
    const pinned = { ...plugin, url: plugin.url.replace('127.0.0.1', 'hostname.invalid') }
    const forwarder = await PluginForwarder.start()
    forwarders.push(forwarder)
    const registration = forwarder.register(pinned, '127.0.0.1')
    await post(registration.url)
    await registration.terminate()
    const deletes = seen.filter((s) => s.method === 'DELETE')
    expect(deletes).toHaveLength(1)
    expect(deletes[0]?.headers.host).toMatch(/^hostname\.invalid:/)
  })

  it('records a session once however many responses carry it', async () => {
    const { seen, plugin } = await upstream()
    const { events, sessions } = ledger()
    const forwarder = await PluginForwarder.start({ sessions })
    forwarders.push(forwarder)
    const registration = forwarder.register(plugin, '127.0.0.1', { record: true })
    await post(registration.url)
    await post(registration.url, { 'mcp-session-id': 's-1' })
    await registration.terminate()
    expect(events).toEqual(['seen memory s-1', 'ended memory s-1'])
    expect(seen.filter((s) => s.method === 'DELETE')).toHaveLength(1)
  })

  it('does not send a DELETE for a session the client already ended', async () => {
    const { seen, plugin } = await upstream()
    const { events, sessions } = ledger()
    const forwarder = await PluginForwarder.start({ sessions })
    forwarders.push(forwarder)
    const registration = forwarder.register(plugin, '127.0.0.1', { record: true })
    await post(registration.url)
    expect((await fetch(registration.url, { method: 'DELETE', headers: { 'mcp-session-id': 's-1' } })).status).toBe(200)
    expect(events).toEqual(['seen memory s-1', 'ended memory s-1'])
    await registration.terminate()
    expect(seen.filter((s) => s.method === 'DELETE')).toHaveLength(1)
  })

  it('gives up on an upstream that never answers within 5 s, and still releases the route', async () => {
    const { plugin } = await upstream({ hang: true })
    const logs: string[] = []
    const forwarder = await PluginForwarder.start({ log: (m) => logs.push(m) })
    forwarders.push(forwarder)
    const registration = forwarder.register(plugin, '127.0.0.1', { record: true })
    await post(registration.url)
    const started = Date.now()
    await registration.terminate()
    expect(Date.now() - started).toBeLessThan(7_000)
    expect(logs.join('\n')).toContain("ending memory's MCP session failed")
    expect(logs.join('\n')).not.toContain('plugin-secret-1')
    expect(forwarder.size).toBe(0)
  }, 15_000)
})
