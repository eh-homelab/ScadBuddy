import { randomUUID } from 'node:crypto'
import { createServer, type IncomingMessage, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js'
import { ApplicationFailure } from '@temporalio/common'
import { MockActivityEnvironment } from '@temporalio/testing'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { forwardForRun, PluginForwarder } from '../src/plugins/forwarder.js'
import type { RemotePlugin } from '../src/plugins/registry.js'
import type { Owner } from '../src/sessions/protocol.js'
import { SessionClients } from '../src/temporal/sessionClients.js'
import { turnServers } from '../src/temporal/turnServers.js'
import {
  TURN_TOOL_TIER_ACTIVITY,
  TURN_TOOLS_ACTIVITY,
  TURN_TOOLS_CLOSE_ACTIVITY,
  turnToolActivities,
  turnToolActivity,
  type ListedTools,
  type TurnToolsDeps,
  type TurnToolsResult,
} from '../src/temporal/turnTools.js'

// #2581: a durable session keeps one MCP session per remote plugin on the replica that
// holds it, so a stateful plugin (Playwright MCP run --isolated: one browser context per
// MCP session) keeps its page from one call, and one turn, to the next.

const A = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const B = '1c7d2f5f-8e4b-4a6f-8b62-4a2d3e5f6a71'
const FLOW = '7c2d9a10-3b4e-4f5a-8b6c-1d2e3f4a5b6c'
const BROWSER: Owner = { kind: 'browser', id: 'browser', label: 'You' }
const HOST = 'agent-tools.host-1'

/** A stateful Streamable HTTP MCP server shaped like Playwright MCP --isolated: a page per MCP session. */
type BrowserMcp = {
  url: string
  created: string[]
  deleted: string[]
  calls: string[]
  /** The server forgets a session (a restart, its own expiry): its id now answers 404. */
  expire(id: string): void
  close(): Promise<void>
}

function readJson(req: IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    req.on('data', (c: Buffer) => chunks.push(c))
    req.on('end', () => {
      try {
        resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : undefined)
      } catch (err) {
        reject(err as Error)
      }
    })
    req.on('error', reject)
  })
}

async function startBrowserMcp(): Promise<BrowserMcp> {
  const sessions = new Map<string, { transport: StreamableHTTPServerTransport; page?: string }>()
  const created: string[] = []
  const deleted: string[] = []
  const calls: string[] = []
  const build = (state: { page?: string }) => {
    const server = new McpServer({ name: 'browser', version: '1' })
    server.registerTool('navigate', { description: 'opens a page', inputSchema: { url: z.string() } }, async ({ url }) => {
      calls.push(`navigate ${url}`)
      state.page = url
      return { content: [{ type: 'text', text: `at ${url}` }] }
    })
    server.registerTool('page', { description: 'the open page', inputSchema: {} }, async () => {
      calls.push('page')
      return { content: [{ type: 'text', text: `page ${state.page ?? 'about:blank'}` }] }
    })
    server.registerTool('crash', { description: 'dies mid-call', inputSchema: {} }, async () => ({ content: [] }))
    return server
  }
  const http: Server = createServer((req, res) => {
    void (async () => {
      const id = req.headers['mcp-session-id']
      const body = req.method === 'POST' ? await readJson(req) : undefined
      if (typeof id === 'string') {
        const held = sessions.get(id)
        if (!held) {
          res.writeHead(404, { 'content-type': 'application/json' }).end(JSON.stringify({ error: 'Session not found' }))
          return
        }
        if (req.method === 'DELETE') {
          deleted.push(id)
          sessions.delete(id)
        }
        const message = body as { method?: string; params?: { name?: string } } | undefined
        if (message?.method === 'tools/call' && message.params?.name === 'crash') {
          // The connection dies after the request arrived: it may have run.
          calls.push('crash')
          req.socket.destroy()
          return
        }
        await held.transport.handleRequest(req, res, body)
        return
      }
      const state: { page?: string } = {}
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (sid) => {
          created.push(sid)
          sessions.set(sid, Object.assign(state, { transport }))
        },
      })
      await build(state).connect(transport)
      await transport.handleRequest(req, res, body)
    })().catch(() => {
      if (!res.headersSent) res.writeHead(500).end()
    })
  })
  await new Promise<void>((resolve) => http.listen(0, '127.0.0.1', resolve))
  const { port } = http.address() as AddressInfo
  return {
    url: `http://127.0.0.1:${port}/mcp`,
    created,
    deleted,
    calls,
    expire: (sid) => void sessions.delete(sid),
    close: () =>
      new Promise<void>((resolve) => {
        http.closeAllConnections()
        http.close(() => resolve())
      }),
  }
}

let forwarder: PluginForwarder
let mcp: BrowserMcp
const replicas: SessionClients<ListedTools>[] = []

beforeAll(async () => {
  forwarder = await PluginForwarder.start()
})
afterAll(async () => {
  await forwarder.close()
})
afterEach(async () => {
  for (const clients of replicas.splice(0)) await clients.closeAll()
  await mcp?.close()
})

function plugin(overrides: Partial<RemotePlugin> = {}): RemotePlugin {
  return { name: 'browser', url: mcp.url, toolTiers: { navigate: 'read', page: 'read', crash: 'read' }, disabledTools: [], ...overrides }
}

/** One agent replica: its own kept clients, its own host queue. */
function replica(options: { plugin?: () => RemotePlugin; idleMs?: number; host?: string } = {}) {
  const clients = new SessionClients<ListedTools>(options.idleMs === undefined ? {} : { idleMs: options.idleMs })
  replicas.push(clients)
  const deps: TurnToolsDeps = {
    sessions: { ownerOf: async (id) => ([A, B].includes(id) ? (BROWSER as never) : undefined) },
    flows: { startedBy: async (run) => (run === FLOW ? BROWSER : undefined), approved: async () => false },
    servers: turnServers({
      remotePlugins: async () =>
        forwardForRun({ plugins: [{ plugin: (options.plugin ?? plugin)(), address: '127.0.0.1' }], problems: [] }, forwarder),
    }),
    hostQueue: options.host ?? HOST,
    clients,
  }
  const activities = turnToolActivities(deps)
  const call = turnToolActivity(deps)
  const env = (activityType: string, session: string, workflowId = `session-${session}`) =>
    new MockActivityEnvironment({
      activityId: `tool-${randomUUID()}`,
      activityType,
      workflowExecution: { workflowId, runId: 'run-1' },
      taskQueue: HOST,
    })
  return {
    clients,
    list: (session: string) =>
      env(TURN_TOOLS_ACTIVITY, session).run(activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>) as Promise<TurnToolsResult>,
    tier: (session: string, tool: string) =>
      env(TURN_TOOL_TIER_ACTIVITY, session).run(activities[TURN_TOOL_TIER_ACTIVITY]!, { tool, input: {} }) as Promise<unknown> as Promise<{
        tier: string
        task_queue?: string
      }>,
    call: (session: string, tool: string, input: unknown = {}) =>
      env(`turn:browser__${tool}`, session).run(call, input) as Promise<unknown> as Promise<string>,
    flowCall: (tool: string) => env(`turn:browser__${tool}`, FLOW, `flow-${FLOW}`).run(call, {}) as Promise<unknown> as Promise<string>,
    close: (session: string) => env(TURN_TOOLS_CLOSE_ACTIVITY, session).run(activities[TURN_TOOLS_CLOSE_ACTIVITY] as () => Promise<void>),
  }
}

async function failure(promise: Promise<unknown>): Promise<ApplicationFailure> {
  const err = await promise.then(
    () => undefined,
    (e: unknown) => e,
  )
  expect(err).toBeInstanceOf(ApplicationFailure)
  return err as ApplicationFailure
}

const until = async (check: () => boolean) => {
  for (let i = 0; i < 200 && !check(); i++) await new Promise((r) => setTimeout(r, 10))
  expect(check()).toBe(true)
}

describe('a durable session keeps one MCP session per remote plugin (#2581)', () => {
  it("reuses one MCP session for the turn's listing and every call, so the page stays open", async () => {
    mcp = await startBrowserMcp()
    const r = replica()
    await r.list(A)
    expect(await r.call(A, 'navigate', { url: 'https://example.com/' })).toContain('at https://example.com/')
    await r.tier(A, 'browser__page')
    expect(await r.call(A, 'page')).toContain('page https://example.com/')
    // The next turn's listing, and its calls, on the same session.
    await r.list(A)
    expect(await r.call(A, 'page')).toContain('page https://example.com/')
    expect(mcp.created).toHaveLength(1)
    expect(mcp.deleted).toEqual([])
    expect(r.clients.size).toBe(1)
  })

  it('keeps each durable session on its own MCP session', async () => {
    mcp = await startBrowserMcp()
    const r = replica()
    await r.call(A, 'navigate', { url: 'https://a.example/' })
    await r.call(B, 'navigate', { url: 'https://b.example/' })
    expect(await r.call(A, 'page')).toContain('page https://a.example/')
    expect(await r.call(B, 'page')).toContain('page https://b.example/')
    expect(mcp.created).toHaveLength(2)
  })

  it("ends the session's MCP sessions with a DELETE when the session ends, and frees their forwarder routes", async () => {
    mcp = await startBrowserMcp()
    const routes = forwarder.size
    const r = replica()
    await r.call(A, 'navigate', { url: 'https://a.example/' })
    await r.call(B, 'page')
    expect(forwarder.size).toBe(routes + 2)
    await r.close(A)
    expect(mcp.deleted).toEqual([mcp.created[0]])
    expect(r.clients.size).toBe(1)
    // The process stopping ends the rest.
    await r.clients.closeAll()
    expect(mcp.deleted).toEqual(mcp.created)
    expect(forwarder.size).toBe(routes)
  })

  it('ends a client left idle, and the next call opens a new one', async () => {
    mcp = await startBrowserMcp()
    const r = replica({ idleMs: 50 })
    await r.call(A, 'navigate', { url: 'https://a.example/' })
    await until(() => mcp.deleted.length === 1)
    expect(r.clients.size).toBe(0)
    expect(await r.call(A, 'page')).toContain('page about:blank')
    expect(mcp.created).toHaveLength(2)
  })

  it("starts a new MCP session when the plugin's config changes, ending the old one", async () => {
    mcp = await startBrowserMcp()
    let config = plugin()
    const r = replica({ plugin: () => config })
    await r.call(A, 'navigate', { url: 'https://a.example/' })
    config = plugin({ header: { name: 'authorization', value: 'Bearer rotated' } })
    expect(await r.call(A, 'page')).toContain('page about:blank')
    expect(mcp.deleted).toEqual([mcp.created[0]])
    expect(mcp.created).toHaveLength(2)
  })

  it('reconnects once and retries a call the server refused for an unknown session, and says the state was reset', async () => {
    mcp = await startBrowserMcp()
    const r = replica()
    await r.call(A, 'navigate', { url: 'https://a.example/' })
    mcp.expire(mcp.created[0]!)
    const text = await r.call(A, 'page')
    expect(text).toContain('page about:blank')
    expect(text).toMatch(/MCP session .*was reset/)
    expect(mcp.created).toHaveLength(2)
    // Ran once, on the new session.
    expect(mcp.calls.filter((c) => c === 'page')).toHaveLength(1)
    // And the new session is kept.
    expect(await r.call(A, 'page')).not.toMatch(/was reset/)
  })

  it('never retries a call whose connection broke after it was sent; the session is reset for the next call', async () => {
    mcp = await startBrowserMcp()
    const r = replica()
    await r.call(A, 'navigate', { url: 'https://a.example/' })
    const err = await failure(r.call(A, 'crash'))
    expect(err.nonRetryable).toBe(true)
    expect(err.message).toMatch(/not retried/)
    expect(err.message).toMatch(/was reset/)
    expect(mcp.calls.filter((c) => c === 'crash')).toHaveLength(1)
    expect(await r.call(A, 'page')).toContain('page about:blank')
  })

  it("pins the plugin's calls to the replica that holds its client; another replica starts afresh", async () => {
    mcp = await startBrowserMcp()
    const one = replica({ host: 'agent-tools.host-1' })
    const { tools } = await one.list(A)
    expect(tools.find((t) => t.name === 'browser__page')?.task_queue).toBe('agent-tools.host-1')
    expect((await one.tier(A, 'browser__page')).task_queue).toBe('agent-tools.host-1')
    await one.call(A, 'navigate', { url: 'https://a.example/' })
    // The pinned replica is gone; the next turn lists on another, whose calls start a fresh session.
    const two = replica({ host: 'agent-tools.host-2' })
    expect((await two.list(A)).tools.find((t) => t.name === 'browser__page')?.task_queue).toBe('agent-tools.host-2')
    expect(await two.call(A, 'page')).toContain('page about:blank')
  })

  it('keeps no client for a tier check on a replica that holds none, and ends the one it listed with', async () => {
    mcp = await startBrowserMcp()
    const r = replica()
    await r.tier(A, 'browser__page')
    expect(r.clients.size).toBe(0)
    expect(mcp.deleted).toEqual(mcp.created)
  })

  it("keeps nothing for a flow run's call, and ends its MCP session", async () => {
    mcp = await startBrowserMcp()
    const r = replica()
    expect(await r.flowCall('page')).toContain('page about:blank')
    expect(r.clients.size).toBe(0)
    expect(mcp.created).toHaveLength(1)
    expect(mcp.deleted).toEqual(mcp.created)
  })
})

describe('SessionClients', () => {
  const fake = () => {
    const ended: string[] = []
    const connect = (name: string) => async () => {
      const client = new Client({ name, version: '1' })
      client.close = async () => void ended.push(name)
      return { client, release: () => {} }
    }
    return { ended, connect }
  }

  it('ends the least recently used idle client past its bound', async () => {
    const clients = new SessionClients({ max: 2 })
    const { ended, connect } = fake()
    await clients.open('s1', 'p', 'f', connect('one'))
    await clients.open('s2', 'p', 'f', connect('two'))
    await clients.open('s1', 'p', 'f', connect('unused'))
    await clients.open('s3', 'p', 'f', connect('three'))
    await until(() => ended.length === 1)
    expect(ended).toEqual(['two'])
    expect(clients.size).toBe(2)
    await clients.closeAll()
  })

  it('does not end a client for idleness while a call is using it', async () => {
    const clients = new SessionClients({ idleMs: 20 })
    const { ended, connect } = fake()
    const kept = await clients.open('s1', 'p', 'f', connect('one'))
    await clients.use(kept, () => new Promise((r) => setTimeout(r, 80)))
    expect(ended).toEqual([])
    await until(() => ended.length === 1)
  })
})
