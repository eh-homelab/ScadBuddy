import { randomBytes } from 'node:crypto'
import type { HookInput } from '@anthropic-ai/claude-agent-sdk'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { BUILTIN_SOURCES } from '../src/access/model.js'
import { createApp } from '../src/app.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { buildHarnessOptions } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { BuiltInTools } from '../src/plugins/builtInTools.js'
import { CAPABILITIES_PATH } from '../src/routes/capabilities.js'
import { FLOW_TOOLS_PATH } from '../src/routes/flowTools.js'
import { subagentServer } from '../src/routes/subagentMcp.js'
import { kekFromBase64 } from '../src/secrets.js'
import { mintSubagentToken, subagentKey, verifySubagentToken } from '../src/temporal/subagentToken.js'
import { toolActivities } from '../src/temporal/toolActivities.js'
import { harnessTools } from '../src/tools/harness.js'
import { TOOL_TIER_ACTIVITY } from '../src/temporal/toolTier.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { createExternalServer, refreshOfferedTools } from '../src/tools/projections.js'
import { defineTool, json, runToolWithOutcome, type Tool } from '../src/tools/registry.js'
import { services } from './helpers/mcp.js'
import { memoryAccess } from './support/memoryAccess.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// #2171 acceptance: access the user sets through /api/v1/ai/capabilities is what every gate
// reads. One memoryAccess sits behind the route and the gates. The stand-in has the name of
// one of ScadBuddy's own outward tools, so the route offers it, but runs nothing real. The
// durable run of a lowered tool is pinned by builtInTools.test.ts ("the durable activities
// run a lowered outward tool with no approval recorded") over the same store. A remote
// tool's tier at the classic seam is capabilities.gates.pg.test.ts.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }
const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const signal = new AbortController().signal

const real = ALL_TOOLS.find((t) => t.risk === 'outward')!

function rig() {
  const { access, rows } = memoryAccess()
  const handler = vi.fn(async () => json({ sent: true }))
  const standIn = defineTool({ name: real.name, label: real.label, description: 'stands in', input: z.object({}), risk: 'outward', routes: [], handler })
  const svc = services({ toolOverrides: new BuiltInTools(access).registryOverrides() })
  const peer = { address: '10.0.0.7' }
  const app = createApp({
    access: access as never,
    database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
    backend: () => Promise.resolve(true),
    kek: { ok: true, kek },
    credentials: new MemoryCredentials(),
    testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
    remoteAddress: () => peer.address,
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    flowTools: { tools: [standIn], services: svc },
  })
  const lower = async () => {
    peer.address = '10.0.0.7'
    const res = await app.request(`${CAPABILITIES_PATH}/${encodeURIComponent(BUILTIN_SOURCES.scadbuddy)}/items/${real.name}`, {
      method: 'PUT',
      headers: UI,
      body: JSON.stringify({ access: 'write' }),
    })
    expect(res.status).toBe(200)
  }
  return { app, peer, rows, access, standIn, handler, svc, lower }
}

describe('access set in Settings → Tools & plugins, at every gate (#2171)', () => {
  it('a tool lowered in Settings runs over /mcp without a pending approval', async () => {
    const { standIn, handler, svc, lower } = rig()
    const ctx = { ...svc, principal: { id: 'token:1', kind: 'bearer' as const, tiers: tiersUpTo('outward') }, progress: async () => {}, signal }
    // Before: it asks first.
    const before = await runToolWithOutcome(standIn, {}, ctx)
    expect(JSON.stringify(before.result)).toMatch(/pending_approval/)
    expect(handler).not.toHaveBeenCalled()

    await lower()
    const run = await runToolWithOutcome(standIn, {}, ctx)
    expect(run.outcome).not.toBe('refused')
    expect(JSON.stringify(run.result)).not.toMatch(/pending_approval/)
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('a tool lowered in Settings is allowed at the classic seam: canUseTool and PreToolUse', async () => {
    const { access, svc, lower } = rig()
    const seam = async () => {
      const options = buildHarnessOptions({
        paths: { stateDir: '/var/lib/scadbuddy-agent' },
        credential: { kind: 'anthropic_api_key', secret: 'sk-ant-api03-unit-test-key-000011112222' },
        prompt: 'hi',
        tierOf: harnessTools(svc).tierOf,
        builtInPolicy: await new BuiltInTools(access).policy(),
      })
      const name = `mcp__scadbuddy__${real.name}`
      const input = { hook_event_name: 'PreToolUse', tool_name: name, tool_input: {}, tool_use_id: 'toolu_1', session_id: 's', transcript_path: '/dev/null', cwd: '/tmp' }
      return {
        canUseTool: await options.canUseTool!(name, {}, { signal, toolUseID: 't', requestId: 'r' }),
        preToolUse: await options.hooks!.PreToolUse![0]!.hooks[0]!(input as HookInput, 'toolu_1', { signal }),
      }
    }
    // Before: outward, so with no approval gate it is denied as needing approval.
    expect(await seam()).toMatchObject({
      canUseTool: { behavior: 'deny', message: expect.stringMatching(/approval/) as unknown },
      preToolUse: { hookSpecificOutput: { permissionDecision: 'deny' } },
    })
    await lower()
    expect(await seam()).toEqual({ canUseTool: expect.objectContaining({ behavior: 'allow' }) as unknown, preToolUse: {} })
  })

  it('a tool lowered in Settings answers write from /internal/flows/tools', async () => {
    const { app, peer, lower } = rig()
    const tiers = async () => {
      peer.address = '127.0.0.1'
      const res = await app.request(FLOW_TOOLS_PATH)
      expect(res.status).toBe(200)
      return ((await res.json()) as { tools: { name: string; tier: string }[] }).tools
    }
    expect(await tiers()).toEqual([expect.objectContaining({ name: real.name, tier: 'outward' })])
    await lower()
    expect(await tiers()).toEqual([expect.objectContaining({ name: real.name, tier: 'write', disabled: false })])
  })

  it('a tool lowered in Settings answers write from the durable TOOL_TIER_ACTIVITY', async () => {
    const { standIn, svc, lower } = rig()
    const activities = toolActivities([standIn], {
      services: svc,
      sessions: { ownerOf: async () => ({ kind: 'browser', id: 'browser', label: 'You' }) },
      approvals: { approved: async () => false },
    })
    const env = new MockActivityEnvironment({
      activityId: 'tier-1',
      workflowExecution: { workflowId: `session-${SESSION}`, runId: 'run-1' },
      activityType: TOOL_TIER_ACTIVITY,
      taskQueue: 'agent-tools',
    })
    const ask = () => env.run(activities[TOOL_TIER_ACTIVITY]!, { tool: real.name })
    expect(await ask()).toEqual({ tier: 'outward', disabled: false })
    await lower()
    expect(await ask()).toEqual({ tier: 'write', disabled: false })
  })
})

// #4757: the browser's `*` switch, at the same gates.
describe('builtin:browser switched off, at every gate (#4757)', () => {
  const browser = ALL_TOOLS.find((t) => t.name.startsWith('browser_'))!
  const other = ALL_TOOLS.find((t) => !t.name.startsWith('browser_'))!
  const standIn = (t: Tool) =>
    defineTool({ name: t.name, label: t.label, description: 'stands in', input: z.object({}), risk: t.risk, routes: [], handler: async () => json({ ran: t.name }) })

  function browserRig() {
    const { access } = memoryAccess()
    const tools = [standIn(browser), standIn(other)]
    const svc = services({ toolOverrides: new BuiltInTools(access).registryOverrides() })
    const off = () => access.setEnabled(BUILTIN_SOURCES.browser, false)
    return { access, tools, svc, off }
  }

  it('builtin:browser switched off hides browser tools from /mcp', async () => {
    const { tools, svc, off } = browserRig()
    const server = createExternalServer(tools, svc)
    const [c, s] = InMemoryTransport.createLinkedPair()
    await server.connect(s)
    const client = new Client({ name: 't', version: '1' })
    await client.connect(c)
    const listed = async () => {
      await refreshOfferedTools(server)
      return (await client.listTools()).tools.map((t) => t.name).sort()
    }
    expect(await listed()).toEqual([browser.name, other.name].sort())
    await off()
    expect(await listed()).toEqual([other.name])
  })

  // A flow has no tab, so the route never lists a browser tool (temporal/flowTools.ts forFlows):
  // switched off, it stays unlisted, and nothing else changes.
  it('builtin:browser switched off answers disabled from /internal/flows/tools', async () => {
    const { tools, svc, off, access } = browserRig()
    const app = createApp({
      access: access as never,
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: true, kek },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
      remoteAddress: () => '127.0.0.1',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      flowTools: { tools, services: svc },
    })
    const disabled = async () => {
      const res = await app.request(FLOW_TOOLS_PATH)
      expect(res.status).toBe(200)
      return Object.fromEntries(((await res.json()) as { tools: { name: string; disabled: boolean }[] }).tools.map((t) => [t.name, t.disabled]))
    }
    expect(await disabled()).toEqual({ [other.name]: false })
    await off()
    expect(await disabled()).toEqual({ [other.name]: false })
  })

  it('builtin:browser switched off answers disabled from the durable TOOL_TIER_ACTIVITY', async () => {
    const { tools, svc, off } = browserRig()
    const activities = toolActivities(tools, {
      services: svc,
      sessions: { ownerOf: async () => ({ kind: 'browser', id: 'browser', label: 'You' }) },
      approvals: { approved: async () => false },
    })
    const env = new MockActivityEnvironment({
      activityId: 'tier-1',
      workflowExecution: { workflowId: `session-${SESSION}`, runId: 'run-1' },
      activityType: TOOL_TIER_ACTIVITY,
      taskQueue: 'agent-tools',
    })
    const ask = (tool: string) => env.run(activities[TOOL_TIER_ACTIVITY]!, { tool })
    expect(await ask(browser.name)).toEqual({ tier: browser.risk, disabled: false })
    await off()
    expect(await ask(browser.name)).toEqual({ tier: browser.risk, disabled: true })
    expect(await ask(other.name)).toEqual({ tier: other.risk, disabled: false })
  })

  it('builtin:browser switched off answers disabled from the subagent loopback', async () => {
    const { tools, svc, off } = browserRig()
    const key = subagentKey(kek)
    const owner = { kind: 'browser' as const, id: 'browser', label: 'You' }
    const turn = '6a1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d'
    const server = subagentServer(
      {
        sessions: { ownerOf: async (id) => (id === SESSION ? { ...owner, turnTiers: null } : undefined) },
        running: async () => true,
        currentTiers: async () => undefined,
        keys: () => [key],
        tools,
        services: svc,
        audit: { record: vi.fn(async () => {}), hash: () => 'h', summarise: () => 's' } as never,
      },
      verifySubagentToken(mintSubagentToken(key, { session: SESSION, turn, owner }, Date.now() / 1000 + 600), [key])!,
    )
    const [c, s] = InMemoryTransport.createLinkedPair()
    await server.connect(s)
    const client = new Client({ name: 't', version: '1' })
    await client.connect(c)
    const call = async () => {
      const r = (await client.callTool({ name: browser.name, arguments: {} })) as { isError?: boolean; content: { text: string }[] }
      return { error: r.isError === true, text: r.content.map((x) => x.text).join('') }
    }
    expect(await call()).toMatchObject({ error: false })
    await off()
    expect(await call()).toEqual({ error: true, text: expect.stringMatching(/disabled in Settings/) as unknown })
  })
})
