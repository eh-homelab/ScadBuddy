import { randomBytes } from 'node:crypto'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { createApp } from '../src/app.js'
import { originPolicy } from '../src/http/origins.js'
import { SETTING_DURABLE_SUBAGENTS } from '../src/routes/durableSubagents.js'
import { BUILTIN_SOURCES, type ToolAccess } from '../src/access/model.js'
import { BuiltInTools } from '../src/plugins/builtInTools.js'
import type { SettingsRepo } from '../src/routes/httpRequest.js'
import { ASK_THE_MAIN_AGENT, SUBAGENT_MCP_PATH, type SubagentMcpDeps, subagentServer, TURN_OVER } from '../src/routes/subagentMcp.js'
import { kekFromBase64 } from '../src/secrets.js'
import type { Owner } from '../src/sessions/protocol.js'
import { mintSubagentToken, subagentKey, verifySubagentToken } from '../src/temporal/subagentToken.js'
import { defineTool, json, type Tool } from '../src/tools/registry.js'
import { services } from './helpers/mcp.js'
import { memoryAccess, rowsFromOverrides } from './support/memoryAccess.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// A durable segment's subagents' ScadBuddy tools (#2320, routes/subagentMcp.ts): every
// call decided from the session as it is when the call runs, as the main path's
// (toolActivities.ts), never from the token (security review of the subagents commit).

const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const OTHER = '9d1f0e2a-3b4c-4d5e-8f60-718293a4b5c6'
const TURN = '6a1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d'
const KEK = kekFromBase64(randomBytes(32).toString('base64'))
const KEY = subagentKey(KEK)
const ALICE: Owner = { kind: 'bearer', id: 'token:7', label: 'Alice token' }
const BOB: Owner = { kind: 'bearer', id: 'token:8', label: 'Bob token' }

const tool = (name: string, risk: 'read' | 'write' | 'outward'): Tool =>
  defineTool({ name, description: name, input: z.object({}), risk, routes: [], handler: async () => json({ ran: name }) })
const TOOLS = [tool('look', 'read'), tool('change', 'write'), tool('send', 'outward')]

function rig() {
  const { access } = memoryAccess()
  const sets = [
    {
      name: 'scadbuddy',
      tool_prefix: 'mcp__scadbuddy__',
      switchable: false,
      tools: TOOLS.map((t) => ({ name: t.name, harness_name: `mcp__scadbuddy__${t.name}`, risk: t.risk })),
    },
  ]
  const state = {
    owner: ALICE as Owner,
    sent: null as string[] | null,
    sentBy: ALICE as Owner,
    running: true,
    now: undefined as string[] | undefined,
  }
  const deps: SubagentMcpDeps = {
    sessions: {
      ownerOf: async (id) =>
        id === SESSION
          ? {
              ...state.owner,
              // As PgSessionOwners: the sender's tiers only while it still owns the session.
              turnTiers: state.sent && state.sentBy.id === state.owner.id ? (state.sent as never) : null,
            }
          : undefined,
    },
    running: async (session, turn) => state.running && session === SESSION && turn === TURN,
    currentTiers: async () => state.now as never,
    keys: () => [KEY],
    tools: TOOLS,
    services: services({ toolOverrides: new BuiltInTools(access, sets).registryOverrides() }),
    audit: { record: vi.fn(async () => {}), hash: () => 'h', summarise: () => 's' } as never,
  }
  const token = mintSubagentToken(KEY, { session: SESSION, turn: TURN, owner: ALICE }, Date.now() / 1000 + 600)
  const set = (tool_tiers: Record<string, string>, disabled_tools: string[]) =>
    access.replaceTools(
      BUILTIN_SOURCES.scadbuddy,
      Object.fromEntries(rowsFromOverrides(BUILTIN_SOURCES.scadbuddy, tool_tiers, disabled_tools).map((r) => [r.item, r.access as ToolAccess])),
    )
  return { deps, state, token, set }
}

async function client(deps: SubagentMcpDeps, token: string): Promise<Client> {
  const claim = verifySubagentToken(token, deps.keys())
  if (!claim) throw new Error('token did not verify')
  const [c, s] = InMemoryTransport.createLinkedPair()
  await subagentServer(deps, claim).connect(s)
  const cl = new Client({ name: 't', version: '1' })
  await cl.connect(c)
  return cl
}

async function call(cl: Client, name: string): Promise<{ error: boolean; text: string }> {
  const r = (await cl.callTool({ name, arguments: {} })) as { isError?: boolean; content: { text: string }[] }
  return { error: r.isError === true, text: r.content.map((c) => c.text).join('') }
}

describe("a durable segment's subagents' tools", () => {
  it('runs read and write calls as the session runs, and refuses an outward one to the main agent', async () => {
    const { deps, state, token } = rig()
    // A bearer owner holds read by default (harnessPrincipal); its turn was sent with write too.
    state.sent = ['read', 'write', 'outward']
    const cl = await client(deps, token)
    expect((await call(cl, 'look')).error).toBe(false)
    expect((await call(cl, 'change')).error).toBe(false)
    expect(await call(cl, 'send')).toEqual({ error: true, text: ASK_THE_MAIN_AGENT })
  })

  it('refuses every call once the session changed hands mid-turn', async () => {
    const { deps, state, token } = rig()
    const cl = await client(deps, token)
    expect((await call(cl, 'look')).error).toBe(false)
    state.owner = BOB
    expect(await call(cl, 'look')).toEqual({ error: true, text: TURN_OVER })
  })

  it('refuses a token once its turn is over (an interrupt, or the turn ended)', async () => {
    const { deps, state, token } = rig()
    const cl = await client(deps, token)
    state.running = false
    expect(await call(cl, 'look')).toEqual({ error: true, text: TURN_OVER })
  })

  it("refuses another session's token, and one altered to name another session", async () => {
    const { deps, token } = rig()
    const other = mintSubagentToken(KEY, { session: OTHER, turn: TURN, owner: ALICE }, Date.now() / 1000 + 600)
    expect(await call(await client(deps, other), 'look')).toEqual({ error: true, text: TURN_OVER })
    expect(verifySubagentToken(token.replace(SESSION, OTHER), [KEY])).toBeUndefined()
    // Expired, or under another key.
    expect(verifySubagentToken(mintSubagentToken(KEY, { session: SESSION, turn: TURN, owner: ALICE }, 1), [KEY])).toBeUndefined()
    expect(verifySubagentToken(token, [randomBytes(32)])).toBeUndefined()
  })

  it('applies Settings → Plugins on every call: disabled mid-turn, or raised to outward', async () => {
    const { deps, token, set } = rig()
    const cl = await client(deps, token)
    expect((await call(cl, 'look')).error).toBe(false)
    await set({}, ['look'])
    expect((await call(cl, 'look')).text).toMatch(/disabled in Settings/)
    await set({ change: 'outward' }, [])
    expect(await call(cl, 'change')).toEqual({ error: true, text: ASK_THE_MAIN_AGENT })
  })

  it("holds a call to the tiers the turn's sender holds, cut to what its credential holds now", async () => {
    const { deps, state, token } = rig()
    const cl = await client(deps, token)
    state.sent = ['read']
    expect((await call(cl, 'look')).error).toBe(false)
    expect((await call(cl, 'change')).text).toMatch(/needs the "write" tier/)
    // Sent with read and write, but the token holds read only now.
    state.sent = ['read', 'write']
    state.now = ['read']
    expect((await call(cl, 'change')).text).toMatch(/needs the "write" tier/)
  })

  it('answers only a loopback peer that names no forwarded client, and only with a live token', async () => {
    const { deps, state, token } = rig()
    const app = (peer: string) =>
      createApp({
        database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
        backend: () => Promise.resolve(true),
        kek: { ok: true, kek: KEK },
        credentials: new MemoryCredentials(),
        remoteAddress: () => peer,
        origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
        testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
        subagentMcp: deps,
      })
    const post = (peer: string, headers: Record<string, string>) =>
      app(peer).request(SUBAGENT_MCP_PATH, {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream', ...headers },
        body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }),
      })
    expect((await post('10.0.0.7', { authorization: `Bearer ${token}` })).status).toBe(404)
    expect((await post('127.0.0.1', { authorization: `Bearer ${token}`, 'x-forwarded-for': '10.0.0.7' })).status).toBe(404)
    expect((await post('127.0.0.1', {})).status).toBe(401)
    state.running = false
    expect((await post('127.0.0.1', { authorization: `Bearer ${token}` })).status).toBe(401)
    state.running = true
    expect((await post('127.0.0.1', { authorization: `Bearer ${token}` })).status).not.toBe(401)
  })

  it('answers a network peer only in the ranges Settings names, still never through a proxy or without a token', async () => {
    const { deps, token } = rig()
    const values = new Map<string, unknown>()
    const settings: SettingsRepo = {
      get: <T>(key: string) => Promise.resolve(values.get(key) as T),
      set: () => Promise.resolve(),
    }
    const post = (peer: string, headers: Record<string, string>) =>
      createApp({
        database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
        backend: () => Promise.resolve(true),
        kek: { ok: true, kek: KEK },
        credentials: new MemoryCredentials(),
        remoteAddress: () => peer,
        origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
        testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
        settings,
        subagentMcp: deps,
      }).request(SUBAGENT_MCP_PATH, {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream', ...headers },
        body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }),
      })
    const auth = { authorization: `Bearer ${token}` }
    expect((await post('10.42.1.5', auth)).status).toBe(404)
    values.set(SETTING_DURABLE_SUBAGENTS, { url: null, peers: ['10.42.0.0/16'] })
    expect((await post('10.42.1.5', auth)).status).not.toBe(404)
    expect((await post('10.42.1.5', auth)).status).not.toBe(401)
    expect((await post('10.42.1.5', {})).status).toBe(401)
    expect((await post('10.42.1.5', { ...auth, 'x-forwarded-for': '10.42.1.6' })).status).toBe(404)
    expect((await post('10.43.0.1', auth)).status).toBe(404)
    // A stored range the route could not have written answers no network peer.
    values.set(SETTING_DURABLE_SUBAGENTS, { url: null, peers: ['10.42.0.0/16', 'bogus'] })
    expect((await post('10.42.1.5', auth)).status).toBe(404)
  })
})
