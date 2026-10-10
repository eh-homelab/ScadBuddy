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
import { BROWSER_TOOL_TIERS, SETTING_HEADLESS_BROWSER, TOOL_PREFIX as BROWSER_PREFIX } from '../src/harness/headlessBrowser.js'
import { buildHarnessOptions, type HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { BuiltInTools, builtInToolSets, builtInToolsSetting } from '../src/plugins/builtInTools.js'
import { BUILT_INS } from '../src/plugins/packages/builtins.js'
import type { PluginRepo } from '../src/plugins/registry.js'
import type { SettingsRepo } from '../src/routes/headlessBrowser.js'
import { kekFromBase64 } from '../src/secrets.js'
import { toolActivities } from '../src/temporal/toolActivities.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { createExternalServer, refreshOfferedTools } from '../src/tools/projections.js'
import { defineTool, json, runToolWithOutcome, type Tool } from '../src/tools/registry.js'
import { services } from './helpers/mcp.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// #1953: ScadBuddy's own tool sets are listed and permissioned like a remote
// MCP plugin (a tier per tool, disabled tools), tighten-only, and can never be
// added, removed or pointed elsewhere. The override applies on every gate: the
// harness seam and its tool list, /mcp, and the durable activities.

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

/** Overrides stored as Settings would store them, for ScadBuddy's own set. */
function withOverrides(tool_tiers: Record<string, string>, disabled_tools: string[]) {
  return memorySettings({ [builtInToolsSetting('scadbuddy')]: { tool_tiers, disabled_tools } })
}

describe('the built-in tool sets', () => {
  it('come from the registry and the built-ins list', () => {
    const sets = builtInToolSets()
    expect(sets.map((s) => s.name)).toEqual(BUILT_INS.map((b) => b.name))
    const own = sets.find((s) => s.name === 'scadbuddy')!
    expect(own.tools.map((t) => [t.name, t.risk])).toEqual(ALL_TOOLS.map((t) => [t.name, t.risk]))
    expect(own.tools[0]!.harness_name).toBe(OWN(ALL_TOOLS[0]!.name))
    const browser = sets.find((s) => s.name === 'playwright')!
    expect(Object.fromEntries(browser.tools.map((t) => [t.name, t.risk]))).toEqual(BROWSER_TOOL_TIERS)
    expect(browser.tools[0]!.harness_name.startsWith(BROWSER_PREFIX)).toBe(true)
  })

  it('ignore a stored entry that would lower a tier or names no tool', async () => {
    const { settings } = withOverrides({ [writeTool.name]: 'read', nope: 'outward', [readTool.name]: 'write' }, ['nope'])
    const tools = new BuiltInTools(settings)
    expect(await tools.overrides(tools.named('scadbuddy')!)).toEqual({
      tool_tiers: { [readTool.name]: 'write' },
      disabled_tools: [],
    })
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
    delete: vi.fn(() => Promise.resolve(true)),
    reveal: reject,
  }
}

function app(settings: SettingsRepo, plugins: PluginRepo = remoteRepo()) {
  return createApp({
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
      ['playwright', true],
      ['hindsight', false],
    ])
    expect(list[0]!.tools!.map((t) => t.name)).toEqual(ALL_TOOLS.map((t) => t.name))
    expect(list[0]!.enabled).toBe(true)
    // The headless browser's switch: off until stored true.
    expect(list[1]!.enabled).toBe(false)
  })

  it('accepts a raised tier and a disabled tool, and stores them in ai_settings', async () => {
    const { settings, values } = memorySettings()
    const res = await patch(app(settings), 'scadbuddy', {
      tool_tiers: { [readTool.name]: 'outward', [writeTool.name]: 'write' },
      disabled_tools: [otherReadTool.name],
    })
    expect(res.status).toBe(200)
    const view = (await res.json()) as { tool_tiers: Record<string, string>; disabled_tools: string[]; built_in: boolean }
    expect(view.built_in).toBe(true)
    // A tool's own tier is no override.
    expect(view.tool_tiers).toEqual({ [readTool.name]: 'outward' })
    expect(view.disabled_tools).toEqual([otherReadTool.name])
    expect(values.get(builtInToolsSetting('scadbuddy'))).toEqual({
      tool_tiers: { [readTool.name]: 'outward' },
      disabled_tools: [otherReadTool.name],
    })
    const one = await app(settings).request('/api/v1/ai/plugins/scadbuddy', { headers: READ })
    expect(((await one.json()) as { tool_tiers: unknown }).tool_tiers).toEqual({ [readTool.name]: 'outward' })
  })

  it.each([
    ['a write tool lowered to read', { tool_tiers: { [writeTool.name]: 'read' } }],
    ['an outward tool lowered to write', { tool_tiers: { [outwardTool.name]: 'write' } }],
    ['a tier that is none', { tool_tiers: { [readTool.name]: 'none' } }],
    ['a tool the set does not have', { tool_tiers: { no_such_tool: 'outward' } }],
    ['disabling a tool the set does not have', { disabled_tools: ['no_such_tool'] }],
  ])('answers 400 for %s, and stores nothing', async (_label, body) => {
    const { settings, values } = memorySettings()
    expect((await patch(app(settings), 'scadbuddy', body)).status).toBe(400)
    expect(values.size).toBe(0)
  })

  it('raises a headless-browser tool and switches the browser with its existing setting', async () => {
    const { settings, values } = memorySettings()
    const res = await patch(app(settings), 'playwright', {
      tool_tiers: { browser_navigate: 'write' },
      enabled: true,
    })
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({ enabled: true, tool_tiers: { browser_navigate: 'write' } })
    expect(values.get(SETTING_HEADLESS_BROWSER)).toBe(true)
    expect((await patch(app(settings), 'playwright', { tool_tiers: { browser_click: 'read' } })).status).toBe(400)
  })

  it('answers 409 built_in to add, remove, test, re-point or switch off a built-in', async () => {
    const { settings, values } = memorySettings()
    const repo = remoteRepo()
    const a = app(settings, repo)
    const answers = [
      await a.request('/api/v1/ai/plugins', {
        method: 'POST',
        headers: UI,
        body: JSON.stringify({ name: 'scadbuddy', url: 'https://x.example/mcp' }),
      }),
      await a.request('/api/v1/ai/plugins/playwright', { method: 'DELETE', headers: UI }),
      await a.request('/api/v1/ai/plugins/scadbuddy', { method: 'DELETE', headers: UI }),
      await a.request('/api/v1/ai/plugins/scadbuddy/test', { method: 'POST', headers: UI }),
      await patch(a, 'scadbuddy', { url: 'https://x.example/mcp' }),
      await patch(a, 'playwright', { secret: 'abcd' }),
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
    const { settings } = withOverrides({}, [readTool.name])
    const policy = await new BuiltInTools(settings).policy()
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
    const { settings } = withOverrides({ [readTool.name]: 'outward' }, [])
    const policy = await new BuiltInTools(settings).policy()
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

  it('raises a headless-browser tool and disables one', async () => {
    const { settings } = memorySettings({
      [builtInToolsSetting('playwright')]: { tool_tiers: { browser_snapshot: 'outward' }, disabled_tools: ['browser_click'] },
    })
    const policy = await new BuiltInTools(settings).policy()
    expect(policy.tierOf(`${BROWSER_PREFIX}browser_snapshot`, 'read')).toBe('outward')
    expect(policy.tierOf(`${BROWSER_PREFIX}browser_navigate`, 'read')).toBe('read')
    const options = buildHarnessOptions({ ...base, builtInPolicy: policy })
    expect(options.disallowedTools).toContain(`${BROWSER_PREFIX}browser_click`)
  })
})

describe('/mcp', () => {
  it('lists no disabled tool and answers a raised one with a pending approval', async () => {
    const { settings, values } = withOverrides({ [readTool.name]: 'outward' }, [otherReadTool.name])
    const svc = services({ toolOverrides: new BuiltInTools(settings).registryOverrides() })
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
    values.set(builtInToolsSetting('scadbuddy'), { tool_tiers: {}, disabled_tools: [] })
    await refreshOfferedTools(server)
    expect((await client.listTools()).tools.map((t) => t.name)).toContain(otherReadTool.name)
    await client.close()
  })

  it('refuses a caller whose tiers stop below the raised tier', async () => {
    const { settings } = withOverrides({ [readTool.name]: 'write' }, [])
    const ctx = {
      ...services({ toolOverrides: new BuiltInTools(settings).registryOverrides() }),
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
        toolOverrides: new BuiltInTools({ get: () => Promise.reject(new Error('db down')), set: vi.fn() }).registryOverrides(),
      }),
      principal: { id: 'token:3', kind: 'bearer' as const, tiers: tiersUpTo('outward') },
      progress: async () => {},
      signal,
    }
    expect((await runToolWithOutcome(readTool, {}, ctx)).detail).toMatch(/cannot be read/)
  })
})

describe('the durable activities', () => {
  const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
  const whoami: Tool = defineTool({
    name: 'whoami',
    description: 'who runs this',
    input: z.object({}),
    risk: 'read',
    routes: [],
    handler: async () => json({ ok: true }),
  })
  const env = (activityId: string) =>
    new MockActivityEnvironment({
      activityId,
      workflowExecution: { workflowId: `session-${SESSION}`, runId: 'run-1' },
      activityType: 'whoami',
      taskQueue: 'agent-tools',
    })

  function activities(tool_tiers: Record<string, string>, disabled_tools: string[]) {
    const { settings } = memorySettings({ [builtInToolsSetting('scadbuddy')]: { tool_tiers, disabled_tools } })
    const sets = [{ name: 'scadbuddy', tool_prefix: 'mcp__scadbuddy__', switchable: false, tools: [{ name: 'whoami', harness_name: 'mcp__scadbuddy__whoami', risk: 'read' as const }] }]
    return toolActivities([whoami], {
      services: services({ toolOverrides: new BuiltInTools(settings, sets).registryOverrides() }),
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
})
