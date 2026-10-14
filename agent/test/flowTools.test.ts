import { randomBytes } from 'node:crypto'
import { ApplicationFailure } from '@temporalio/common'
import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import { createApp } from '../src/app.js'
import { originPolicy } from '../src/http/origins.js'
import { BuiltInTools, builtInToolsSetting } from '../src/plugins/builtInTools.js'
import { FLOW_TOOLS_PATH } from '../src/routes/flowTools.js'
import type { SettingsRepo } from '../src/routes/httpRequest.js'
import { kekFromBase64 } from '../src/secrets.js'
import { FLOW_TOOL_TIER_ACTIVITY, flowToolTiers } from '../src/temporal/flowTools.js'
import { toolActivities } from '../src/temporal/toolActivities.js'
import { defineTool, json, type Tool } from '../src/tools/registry.js'
import { services } from './helpers/mcp.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// #2704: a flow gets its tools' tiers as the agent's own turns do, live from
// `effectiveTool` (the tool's definition, Settings' raised tiers, disabled tools), never
// from a file baked into the backend's image: per call through the `flow_tool_tier`
// activity, and for the API (grants, the editor) through the loopback route.

const RUN = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const SET = builtInToolsSetting('scadbuddy')
const tool = (name: string, risk: 'read' | 'write' | 'outward'): Tool =>
  defineTool({ name, description: name, input: z.object({}), risk, routes: [], handler: async () => json({ ran: name }) })
const TOOLS = [tool('look', 'read'), tool('change', 'write'), tool('send', 'outward'), tool('browser_click', 'write')]

function rig() {
  const values = new Map<string, unknown>([[SET, { tool_tiers: {}, disabled_tools: [] }]])
  const settings: SettingsRepo = {
    get: <T>(key: string) => Promise.resolve(values.get(key) as T),
    set: (key, value) => Promise.resolve(void values.set(key, value)),
  }
  const sets = [
    {
      name: 'scadbuddy',
      tool_prefix: 'mcp__scadbuddy__',
      switchable: false,
      tools: TOOLS.map((t) => ({ name: t.name, harness_name: `mcp__scadbuddy__${t.name}`, risk: t.risk })),
    },
  ]
  const svc = services({ toolOverrides: new BuiltInTools(settings, sets).registryOverrides() })
  const set = (tool_tiers: Record<string, string>, disabled_tools: string[]) => values.set(SET, { tool_tiers, disabled_tools })
  return { svc, set }
}

const env = (workflowId: string) =>
  new MockActivityEnvironment({
    activityId: 'tier-1',
    workflowExecution: { workflowId, runId: 'run-1' },
    activityType: FLOW_TOOL_TIER_ACTIVITY,
    taskQueue: 'agent-tools',
  })

async function failure(p: Promise<unknown>): Promise<ApplicationFailure> {
  const err = await p.then(
    () => undefined,
    (e: unknown) => e,
  )
  expect(err).toBeInstanceOf(ApplicationFailure)
  return err as ApplicationFailure
}

describe("a flow's tool tiers (#2704)", () => {
  it('lists every tool a flow may call at its tier as Settings raises it, with what is disabled', async () => {
    const { svc, set } = rig()
    set({ look: 'outward' }, ['change'])
    expect(await flowToolTiers(TOOLS, svc)).toEqual([
      { name: 'look', tier: 'outward', disabled: false },
      { name: 'change', tier: 'write', disabled: true },
      { name: 'send', tier: 'outward', disabled: false },
    ])
  })

  it("answers a flow's call with the tool's tier as it is now", async () => {
    const { svc, set } = rig()
    const tier = toolActivities(TOOLS, { services: svc, sessions: { ownerOf: async () => undefined } })[FLOW_TOOL_TIER_ACTIVITY]!
    const flow = env(`flow-${RUN}`)
    expect(await flow.run(tier, { tool: 'look' })).toEqual({ tier: 'read', disabled: false })
    set({ look: 'outward' }, ['send'])
    expect(await flow.run(tier, { tool: 'look' })).toEqual({ tier: 'outward', disabled: false })
    expect(await flow.run(tier, { tool: 'send' })).toEqual({ tier: 'outward', disabled: true })
  })

  it('refuses a tool the agent does not have, one a flow cannot call, and any other caller', async () => {
    const { svc } = rig()
    const tier = toolActivities(TOOLS, { services: svc, sessions: { ownerOf: async () => undefined } })[FLOW_TOOL_TIER_ACTIVITY]!
    const flow = env(`flow-${RUN}`)
    const unknown = await failure(flow.run(tier, { tool: 'no_such_tool' }))
    expect([unknown.type, unknown.nonRetryable]).toEqual(['ToolError', true])
    expect((await failure(flow.run(tier, { tool: 'browser_click' }))).type).toBe('NotForFlows')
    expect((await failure(flow.run(tier, {}))).type).toBe('ToolError')
    expect((await failure(env(`session-${RUN}`).run(tier, { tool: 'look' }))).type).toBe('UnknownFlow')
  })

  describe('the loopback route', () => {
    const KEK = kekFromBase64(randomBytes(32).toString('base64'))
    const app = (peer: string, svc: ReturnType<typeof rig>['svc'] | undefined) =>
      createApp({
        database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
        backend: () => Promise.resolve(true),
        kek: { ok: true, kek: KEK },
        credentials: new MemoryCredentials(),
        remoteAddress: () => peer,
        origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
        testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
        ...(svc ? { flowTools: { tools: TOOLS, services: svc } } : {}),
      })

    it('answers the pod-local API with the tools as Settings has them now', async () => {
      const { svc, set } = rig()
      set({ change: 'outward' }, ['look'])
      const res = await app('127.0.0.1', svc).request(FLOW_TOOLS_PATH)
      expect(res.status).toBe(200)
      expect(await res.json()).toEqual({
        tools: [
          { name: 'look', tier: 'read', disabled: true },
          { name: 'change', tier: 'outward', disabled: false },
          { name: 'send', tier: 'outward', disabled: false },
        ],
      })
    })

    it('is not found from any other peer, through a proxy, or without its deps', async () => {
      const { svc } = rig()
      expect((await app('10.0.0.7', svc).request(FLOW_TOOLS_PATH)).status).toBe(404)
      const proxied = await app('127.0.0.1', svc).request(FLOW_TOOLS_PATH, { headers: { 'x-forwarded-for': '203.0.113.9' } })
      expect(proxied.status).toBe(404)
      expect((await app('127.0.0.1', undefined).request(FLOW_TOOLS_PATH)).status).toBe(404)
    })
  })
})
