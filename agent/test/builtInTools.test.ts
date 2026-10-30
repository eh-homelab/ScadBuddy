import { randomBytes } from 'node:crypto'
import { MockActivityEnvironment } from '@temporalio/testing'
import { ApplicationFailure } from '@temporalio/common'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import type { HookInput } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { createApp } from '../src/app.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { buildHarnessOptions, type HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { BUILTIN_SOURCES, MIGRATED_BY, registrySource, type ToolAccess } from '../src/access/model.js'
import { BuiltInTools, builtInToolSets, type BuiltInToolSet } from '../src/plugins/builtInTools.js'
import { BUILT_INS } from '../src/plugins/packages/builtins.js'
import type { PluginRepo } from '../src/plugins/registry.js'
import type { SettingsRepo } from '../src/routes/module.js'
import { kekFromBase64 } from '../src/secrets.js'
import { toolActivities } from '../src/temporal/toolActivities.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { createExternalServer, refreshOfferedTools } from '../src/tools/projections.js'
import { defineTool, effectiveTool, json, runToolWithOutcome, type Tool } from '../src/tools/registry.js'
import { createBackendClient } from '../src/api/backend.js'
import { BACKEND, services } from './helpers/mcp.js'
import { memoryAccess, rowsFromOverrides } from './support/memoryAccess.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// #1953: ScadBuddy's own tool sets are listed and permissioned like a remote
// MCP plugin (a tier per tool, disabled tools), and can never be added, removed
// or pointed elsewhere. #2171: the access lives in `ai_access` and moves either
// way. The override applies on every gate: the harness seam and its tool list,
// /mcp, and the durable activities.

function memorySettings(initial: Record<string, unknown> = {}) {
  const values = new Map<string, unknown>(Object.entries(initial))
  const settings: SettingsRepo = {
    get: <T>(key: string) => Promise.resolve(values.get(key) as T),
    set: (key, value) => {
      values.set(key, value)
      return Promise.resolve()
    },
  }
  return { settings, values }
}

/** Takes no required argument, so a call with `{}` reaches the tier and the gate. */
function bare(t: Tool): boolean {
  try {
    t.parse({})
    return true
  } catch {
    return false
  }
}
const readTool = ALL_TOOLS.find((t) => t.risk === 'read' && bare(t))!
const otherReadTool = ALL_TOOLS.find((t) => t.risk === 'read' && bare(t) && t.name !== readTool.name)!
const writeTool = ALL_TOOLS.find((t) => t.risk === 'write')!
const outwardTool = ALL_TOOLS.find((t) => t.risk === 'outward')!
const OWN = (tool: string) => `mcp__scadbuddy__${tool}`

const OWN_SOURCE = BUILTIN_SOURCES.scadbuddy

/** Overrides as `ai_access` tool rows: disabled wins, as the migration copies them. */
function toolRows(tool_tiers: Record<string, string>, disabled_tools: string[]): Record<string, ToolAccess> {
  return Object.fromEntries(rowsFromOverrides(OWN_SOURCE, tool_tiers, disabled_tools).map((r) => [r.item, r.access as ToolAccess]))
}

/** Overrides stored in ai_access, for ScadBuddy's own set. */
function withOverrides(tool_tiers: Record<string, string>, disabled_tools: string[]) {
  return memoryAccess(rowsFromOverrides(OWN_SOURCE, tool_tiers, disabled_tools))
}

/** A one-set list with `tools` as ScadBuddy's own. */
function ownSet(tools: readonly Tool[]): BuiltInToolSet[] {
  return [
    {
      name: 'scadbuddy',
      tool_prefix: 'mcp__scadbuddy__',
      switchable: false,
      tools: tools.map((t) => ({ name: t.name, harness_name: `mcp__scadbuddy__${t.name}`, risk: t.risk })),
    },
  ]
}

describe('the built-in tool sets', () => {
  it('come from the registry and the built-ins list', () => {
    const sets = builtInToolSets()
    expect(sets.map((s) => s.name)).toEqual(BUILT_INS.map((b) => b.name))
    const own = sets.find((s) => s.name === 'scadbuddy')!
    expect(own.tools.map((t) => [t.name, t.risk])).toEqual(ALL_TOOLS.map((t) => [t.name, t.risk]))
    expect(own.tools[0]!.harness_name).toBe(OWN(ALL_TOOLS[0]!.name))
  })

  it('ignores a stored row naming no tool, and keeps a lowered tier', async () => {
    const { access } = withOverrides({ [writeTool.name]: 'read', nope: 'outward', [readTool.name]: 'write' }, ['nope'])
    const tools = new BuiltInTools(access)
    expect(await tools.overrides(tools.named('scadbuddy')!)).toEqual({
      tool_tiers: { [writeTool.name]: 'read', [readTool.name]: 'write' },
      disabled_tools: [],
    })
  })

  it('a migrated row that lowers is ignored by every gate', async () => {
    const { access } = memoryAccess([{ source: registrySource(outwardTool.name), item: outwardTool.name, access: 'write', updated_by: MIGRATED_BY }])
    const tools = new BuiltInTools(access)
    expect((await effectiveTool(outwardTool, { toolOverrides: tools.registryOverrides() })).risk).toBe('outward')
    expect((await tools.policy()).tierOf(OWN(outwardTool.name), 'outward')).toBe('outward')
  })
})

// ---------------------------------------------------------------- the routes

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const UI = {
  host: 'scadbuddy.example',
  origin: 'https://scadbuddy.example',
  'x-forwarded-proto': 'https',
  'content-type': 'application/json',
}
const READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

function remoteRepo(): PluginRepo {
  const reject = () => Promise.reject(new Error('not called'))
  return {
    list: () =>
      Promise.resolve([
        {
          name: 'hindsight',
          kind: 'remote_mcp',
          url: 'https://hs.example/mcp/',
          enabled: false,
          auth_header: null,
          secret_last4: null,
          tool_tiers: {},
          disabled_tools: [],
          created_at: '2026-10-10T00:00:00.000Z',
          updated_at: '2026-10-10T00:00:00.000Z',
          kekId: null,
        },
      ]),
    get: () => Promise.resolve(undefined),
    create: vi.fn(reject),
    update: vi.fn(reject),
    setToolAccess: vi.fn(reject),
    delete: vi.fn(() => Promise.resolve(true)),
    reveal: reject,
  }
}

function app(settings: SettingsRepo, plugins: PluginRepo = remoteRepo(), access = memoryAccess().access) {
  return createApp({
    access: access as never,
    database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
    backend: () => Promise.resolve(true),
    kek: { ok: true, kek },
    credentials: new MemoryCredentials(),
    plugins,
    settings,
    testPlugin: vi.fn(),
    testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    resolveHost: () => Promise.resolve(['203.0.113.10']),
  })
}

const patch = (a: ReturnType<typeof app>, name: string, body: unknown) =>
  a.request(`/api/v1/ai/plugins/${name}`, { method: 'PATCH', headers: UI, body: JSON.stringify(body) })

describe('/api/v1/ai/plugins with built-ins', () => {
  it('lists the built-in sets first, from the registry, then the remote plugins', async () => {
    const res = await app(memorySettings().settings).request('/api/v1/ai/plugins', { headers: READ })
    expect(res.status).toBe(200)
    const list = (await res.json()) as {
      name: string
      built_in: boolean
      tools?: { name: string; risk: string }[]
      enabled: boolean
    }[]
    expect(list.map((p) => [p.name, p.built_in])).toEqual([
      ['scadbuddy', true],
      ['hindsight', false],
    ])
    expect(list[0]!.tools!.map((t) => t.name)).toEqual(ALL_TOOLS.map((t) => t.name))
    expect(list[0]!.enabled).toBe(true)
  })

  it('accepts a raised tier and a disabled tool, and stores them in ai_access', async () => {
    const { settings } = memorySettings()
    const { access, rows } = memoryAccess()
    const res = await patch(app(settings, remoteRepo(), access), 'scadbuddy', {
      tool_tiers: { [readTool.name]: 'outward', [writeTool.name]: 'write' },
      disabled_tools: [otherReadTool.name],
    })
    expect(res.status).toBe(200)
    const view = (await res.json()) as { tool_tiers: Record<string, string>; disabled_tools: string[]; built_in: boolean }
    expect(view.built_in).toBe(true)
    // A tool's own tier is no override.
    expect(view.tool_tiers).toEqual({ [readTool.name]: 'outward' })
    expect(view.disabled_tools).toEqual([otherReadTool.name])
    const stored = rows.map(({ source, item, access }) => ({ source, item, access }))
    expect(stored).toHaveLength(2)
    expect(stored).toEqual(
      expect.arrayContaining([
        { source: registrySource(otherReadTool.name), item: otherReadTool.name, access: 'off' },
        { source: registrySource(readTool.name), item: readTool.name, access: 'outward' },
      ]),
    )
    const one = await app(settings, remoteRepo(), access).request('/api/v1/ai/plugins/scadbuddy', { headers: READ })
    expect(((await one.json()) as { tool_tiers: unknown }).tool_tiers).toEqual({ [readTool.name]: 'outward' })
  })

  it.each([
    ['a tier that is none', { tool_tiers: { [readTool.name]: 'none' } }],
    ['a tool the set does not have', { tool_tiers: { no_such_tool: 'outward' } }],
    ['disabling a tool the set does not have', { disabled_tools: ['no_such_tool'] }],
  ])('answers 400 for %s, and stores nothing', async (_label, body) => {
    const { settings, values } = memorySettings()
    const { access, rows } = memoryAccess()
    expect((await patch(app(settings, remoteRepo(), access), 'scadbuddy', body)).status).toBe(400)
    expect(values.size).toBe(0)
    expect(rows).toEqual([])
  })

  it('accepts a lowered tier and stores it in ai_access', async () => {
    const { settings } = memorySettings()
    const { access, rows } = memoryAccess()
    const res = await patch(app(settings, remoteRepo(), access), 'scadbuddy', { tool_tiers: { [outwardTool.name]: 'write' } })
    expect(res.status).toBe(200)
    const view = (await res.json()) as { tool_tiers: Record<string, string> }
    expect(view.tool_tiers).toEqual({ [outwardTool.name]: 'write' })
    expect(rows).toContainEqual(expect.objectContaining({ source: 'builtin:scadbuddy', item: outwardTool.name, access: 'write' }))
  })

  it('answers 409 built_in to add, remove, test, re-point or switch off a built-in', async () => {
    const { settings, values } = memorySettings()
    const { access, rows } = memoryAccess()
    const repo = remoteRepo()
    const a = app(settings, repo, access)
    const answers = [
      await a.request('/api/v1/ai/plugins', {
        method: 'POST',
        headers: UI,
        body: JSON.stringify({ name: 'scadbuddy', url: 'https://x.example/mcp' }),
      }),
      await a.request('/api/v1/ai/plugins/scadbuddy', { method: 'DELETE', headers: UI }),
      await a.request('/api/v1/ai/plugins/scadbuddy/test', { method: 'POST', headers: UI }),
      await patch(a, 'scadbuddy', { url: 'https://x.example/mcp' }),
      await patch(a, 'scadbuddy', { secret: 'abcd' }),
      // ScadBuddy's own tools have no switch as a set: they are switched one by one.
      await patch(a, 'scadbuddy', { enabled: false }),
    ]
    for (const res of answers) {
      expect(res.status).toBe(409)
      expect(await res.json()).toMatchObject({ built_in: true })
    }
    expect(repo.create).not.toHaveBeenCalled()
    expect(repo.delete).not.toHaveBeenCalled()
    expect(values.size).toBe(0)
    expect(rows).toEqual([])
  })
})

// ---------------------------------------------------------------- the gates

async function listVia(server: McpServer) {
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
  await server.connect(serverSide)
  const client = new Client({ name: 'built-in-test', version: '0' })
  await client.connect(clientSide)
  return client
}

const signal = new AbortController().signal

function preToolUse(toolName: string): HookInput {
  return {
    hook_event_name: 'PreToolUse',
    tool_name: toolName,
    tool_input: {},
    tool_use_id: 'toolu_1',
    session_id: 's',
    transcript_path: '/dev/null',
    cwd: '/tmp',
  } as HookInput
}

describe('the harness', () => {
  const base: HarnessRun = {
    paths: { stateDir: '/var/lib/scadbuddy-agent' },
    credential: { kind: 'anthropic_api_key', secret: 'sk-ant-api03-unit-test-key-000011112222' },
    prompt: 'hi',
  }

  it('does not offer a disabled tool, in its tool list or to the model', async () => {
    const { access } = withOverrides({}, [readTool.name])
    const policy = await new BuiltInTools(access).policy()
    const tools = harnessTools(services())
    const servers = tools.mcpServers({ owner: { kind: 'browser', id: 'b', label: 'You' } }, undefined, { builtIn: policy })
    const client = await listVia(servers.scadbuddy!.instance)
    const names = (await client.listTools()).tools.map((t) => t.name)
    await client.close()
    expect(names).not.toContain(readTool.name)
    expect(names).toContain(otherReadTool.name)
    const options = buildHarnessOptions({ ...base, tierOf: tools.tierOf, builtInPolicy: policy })
    expect(options.disallowedTools).toContain(OWN(readTool.name))
  })

  it('enforces a raised tier at the seam: canUseTool and PreToolUse', async () => {
    const { access } = withOverrides({ [readTool.name]: 'outward' }, [])
    const policy = await new BuiltInTools(access).policy()
    const { tierOf } = harnessTools(services())
    const options = buildHarnessOptions({ ...base, tierOf, builtInPolicy: policy })
    const ask = (name: string) => options.canUseTool!(name, {}, { signal, toolUseID: 't', requestId: 'r' })
    // Raised to outward: with no approval gate it is denied as needing approval.
    expect(await ask(OWN(readTool.name))).toMatchObject({ behavior: 'deny', message: expect.stringMatching(/approval/) })
    expect(await ask(OWN(otherReadTool.name))).toMatchObject({ behavior: 'allow' })
    const hook = options.hooks!.PreToolUse![0]!.hooks[0]!
    expect(await hook(preToolUse(OWN(readTool.name)), 'toolu_1', { signal })).toMatchObject({
      hookSpecificOutput: { permissionDecision: 'deny' },
    })
    expect(await hook(preToolUse(OWN(otherReadTool.name)), 'toolu_1', { signal })).toEqual({})
  })

  // #2100: the seam reads the policy once per turn and the in-process server
  // trusts it, so a tool raised to outward mid-turn would otherwise run unasked.
  it('refuses a call raised to outward after the turn read its policy, and runs one the seam parked', async () => {
    const { access } = withOverrides({}, [])
    let calls = 0
    const backend = createBackendClient(BACKEND, async () => {
      calls++
      return Response.json({})
    })
    const svc = services({ backend, toolOverrides: new BuiltInTools(access).registryOverrides() })
    const owner = { kind: 'browser' as const, id: 'b', label: 'You' }
    const call = async (policy: Awaited<ReturnType<BuiltInTools['policy']>>) => {
      const servers = harnessTools(svc).mcpServers({ owner }, undefined, { builtIn: policy })
      const client = await listVia(servers.scadbuddy!.instance)
      const result = await client.callTool({ name: readTool.name, arguments: {} })
      await client.close()
      return JSON.stringify(result)
    }

    const atTurnStart = await new BuiltInTools(access).policy()
    await access.replaceTools(OWN_SOURCE, toolRows({ [readTool.name]: 'outward' }, []))
    expect(await call(atTurnStart)).toMatch(/raised to \\"outward\\" during this turn/)
    expect(calls).toBe(0)

    // Raised before the turn: the seam parked the call and a human approved it.
    await call(await new BuiltInTools(access).policy())
    expect(calls).toBeGreaterThan(0)
  })

  it('the harness seam allows a lowered outward tool', async () => {
    const { access } = memoryAccess(rowsFromOverrides(registrySource(outwardTool.name), { [outwardTool.name]: 'write' }, []))
    const policy = await new BuiltInTools(access).policy()
    const { tierOf } = harnessTools(services())
    const options = buildHarnessOptions({ ...base, tierOf, builtInPolicy: policy })
    const ask = (name: string) => options.canUseTool!(name, {}, { signal, toolUseID: 't', requestId: 'r' })
    expect(await ask(OWN(outwardTool.name))).toMatchObject({ behavior: 'allow' })
  })
})

describe('/mcp', () => {
  it('lists no disabled tool and answers a raised one with a pending approval', async () => {
    const { access } = withOverrides({ [readTool.name]: 'outward' }, [otherReadTool.name])
    const svc = services({ toolOverrides: new BuiltInTools(access).registryOverrides() })
    const server = createExternalServer(ALL_TOOLS, svc)
    await refreshOfferedTools(server)
    const client = await listVia(server)
    const names = (await client.listTools()).tools.map((t) => t.name)
    expect(names).not.toContain(otherReadTool.name)
    expect(names).toContain(readTool.name)

    const principal = { id: 'token:1', kind: 'bearer' as const, tiers: tiersUpTo('outward') }
    const ctx = { ...svc, principal, progress: async () => {}, signal }
    const raised = await runToolWithOutcome(readTool, {}, ctx)
    expect(raised.outcome).toBe('refused')
    expect(JSON.stringify(raised.result)).toMatch(/pending_approval/)
    const disabled = await runToolWithOutcome(otherReadTool, {}, ctx)
    expect(disabled.detail).toMatch(/disabled in Settings/)

    // Turned back on: offered again from the next request.
    await access.replaceTools(OWN_SOURCE, {})
    await refreshOfferedTools(server)
    expect((await client.listTools()).tools.map((t) => t.name)).toContain(otherReadTool.name)
    await client.close()
  })

  it('refuses a caller whose tiers stop below the raised tier', async () => {
    const { access } = withOverrides({ [readTool.name]: 'write' }, [])
    const ctx = {
      ...services({ toolOverrides: new BuiltInTools(access).registryOverrides() }),
      principal: { id: 'token:2', kind: 'bearer' as const, tiers: tiersUpTo('read') },
      progress: async () => {},
      signal,
    }
    const run = await runToolWithOutcome(readTool, {}, ctx)
    expect(run.outcome).toBe('refused')
    expect(run.detail).toMatch(/needs the "write" tier/)
  })

  it('fails closed when the overrides cannot be read', async () => {
    const ctx = {
      ...services({
        toolOverrides: new BuiltInTools({ rows: () => Promise.reject(new Error('db down')) }).registryOverrides(),
      }),
      principal: { id: 'token:3', kind: 'bearer' as const, tiers: tiersUpTo('outward') },
      progress: async () => {},
      signal,
    }
    expect((await runToolWithOutcome(readTool, {}, ctx)).detail).toMatch(/cannot be read/)
  })

  it('a lowered outward tool runs over /mcp without a pending approval', async () => {
    const handler = vi.fn(async () => json({ sent: true }))
    const send = defineTool({ name: 'send_it', label: 'send_it', description: 'sends', input: z.object({}), risk: 'outward', routes: [], handler })
    const { access } = memoryAccess(rowsFromOverrides(OWN_SOURCE, { send_it: 'write' }, []))
    const ctx = {
      ...services({ toolOverrides: new BuiltInTools(access, ownSet([send])).registryOverrides() }),
      principal: { id: 'token:4', kind: 'bearer' as const, tiers: tiersUpTo('outward') },
      progress: async () => {},
      signal,
    }
    const run = await runToolWithOutcome(send, {}, ctx)
    expect(run.outcome).not.toBe('refused')
    expect(JSON.stringify(run.result)).not.toMatch(/pending_approval/)
    expect(handler).toHaveBeenCalledTimes(1)
  })
})

describe('the durable activities', () => {
  const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
  const whoamiSpec = {
    name: 'whoami',
    label: 'whoami',
    description: 'who runs this',
    input: z.object({}),
    risk: 'read' as const,
    routes: [],
    handler: async () => json({ ok: true }),
  }
  const whoami: Tool = defineTool(whoamiSpec)
  const env = (activityId: string) =>
    new MockActivityEnvironment({
      activityId,
      workflowExecution: { workflowId: `session-${SESSION}`, runId: 'run-1' },
      activityType: 'whoami',
      taskQueue: 'agent-tools',
    })

  function activities(tool_tiers: Record<string, string>, disabled_tools: string[], tool: Tool = whoami) {
    const { access } = memoryAccess(rowsFromOverrides(OWN_SOURCE, tool_tiers, disabled_tools))
    return toolActivities([tool], {
      services: services({ toolOverrides: new BuiltInTools(access, ownSet([tool])).registryOverrides() }),
      sessions: { ownerOf: async () => ({ kind: 'browser', id: 'browser', label: 'You' }) },
      approvals: { approved: async (requestId) => requestId.endsWith(':toolu_approved') },
    })
  }

  it('need an approval for a tool raised to outward', async () => {
    const a = activities({ whoami: 'outward' }, [])
    const err = await env('tool-toolu_01').run(a.whoami!, {}).then(
      () => undefined,
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(ApplicationFailure)
    expect((err as ApplicationFailure).type).toBe('NotApproved')
    expect(await env('tool-toolu_approved').run(a.whoami!, {})).toMatch(/ok/)
  })

  it('refuse a disabled tool', async () => {
    const a = activities({}, ['whoami'])
    const err = await env('tool-toolu_02').run(a.whoami!, {}).then(
      () => undefined,
      (e: unknown) => e,
    )
    expect((err as ApplicationFailure).message).toMatch(/disabled in Settings/)
  })

  it('refuse a disabled tool raised to outward as disabled, not as unapproved (#2088)', async () => {
    // The durable gate never parks a disabled tool, so no approval is ever recorded for it.
    const a = activities({ whoami: 'outward' }, ['whoami'])
    const err = await env('tool-toolu_03').run(a.whoami!, {}).then(
      () => undefined,
      (e: unknown) => e,
    )
    expect((err as ApplicationFailure).type).toBe('ToolError')
    expect((err as ApplicationFailure).message).toMatch(/disabled in Settings/)
  })

  it('the durable activities run a lowered outward tool with no approval recorded', async () => {
    const outwardWhoami = defineTool({ ...whoamiSpec, risk: 'outward' })
    const a = activities({ whoami: 'write' }, [], outwardWhoami)
    expect(await env('tool-toolu_04').run(a.whoami!, {})).toMatch(/ok/)
  })

  // #2088: the workflow decides whether to park from the overrides as its turn started;
  // the activity decides whether to run from the overrides as the call runs, once.
  describe('decide from Settings as the call runs, in one read', () => {
    function rig(approve: (requestId: string, call?: { inputHash: string }) => boolean) {
      const { access, reads } = memoryAccess()
      const handler = vi.fn(async () => json({ ok: true }))
      const counted = defineTool({ name: 'counted', label: 'counted', description: 'counts', input: z.object({ n: z.number().optional() }), risk: 'read', routes: [], handler })
      const a = toolActivities([counted], {
        services: services({ toolOverrides: new BuiltInTools(access, ownSet([counted])).registryOverrides() }),
        sessions: { ownerOf: async () => ({ kind: 'browser', id: 'browser', label: 'You' }) },
        approvals: { approved: async (requestId, call) => approve(requestId, call) },
        audit: { record: vi.fn(async () => {}), hash: (t, i) => `${t}:${JSON.stringify(i)}`, summarise: () => '' },
      })
      const set = (tool_tiers: Record<string, string>, disabled_tools: string[]) => access.replaceTools(OWN_SOURCE, toolRows(tool_tiers, disabled_tools))
      const run = (id: string, input: Record<string, unknown> = {}) =>
        env(`tool-${id}`).run(a.counted!, input).then(
          (v: unknown) => v,
          (e: unknown) => e,
        )
      return { set, run, handler, reads }
    }

    it('refuse a call raised to outward after the workflow let it through unparked', async () => {
      const r = rig(() => false)
      // The turn read no override, so the workflow scheduled the call without parking it;
      // Settings raises the tool before the activity runs.
      await r.set({ counted: 'outward' }, [])
      const err = await r.run('toolu_raised')
      expect((err as ApplicationFailure).type).toBe('NotApproved')
      expect(r.handler).not.toHaveBeenCalled()
    })

    it('refuse a call disabled between its approval and its run', async () => {
      const r = rig(() => true)
      await r.set({ counted: 'outward' }, ['counted'])
      const err = await r.run('toolu_approved_then_disabled')
      expect((err as ApplicationFailure).type).toBe('ToolError')
      expect((err as ApplicationFailure).message).toMatch(/disabled in Settings/)
      expect(r.handler).not.toHaveBeenCalled()
    })

    it('read the overrides once per call, so the check and the run cannot disagree', async () => {
      const r = rig(() => true)
      await r.set({ counted: 'outward' }, [])
      expect(await r.run('toolu_once')).toMatch(/ok/)
      expect(r.handler).toHaveBeenCalledTimes(1)
      expect(r.reads.n).toBe(1)
    })

    it('match the approval to the call it was given for: its tool and its input', async () => {
      const seen: (string | undefined)[] = []
      const r = rig((_, call) => {
        seen.push(call?.inputHash)
        return call?.inputHash === 'counted:{"n":1}'
      })
      await r.set({ counted: 'outward' }, [])
      expect(await r.run('toolu_same', { n: 1 })).toMatch(/ok/)
      const err = await r.run('toolu_other', { n: 2 })
      expect((err as ApplicationFailure).type).toBe('NotApproved')
      expect(seen).toEqual(['counted:{"n":1}', 'counted:{"n":2}'])
      expect(r.handler).toHaveBeenCalledTimes(1)
    })
  })
})
