import { ApplicationFailure } from '@temporalio/common'
import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import { BUILTIN_SOURCES, type ToolAccess } from '../src/access/model.js'
import { BuiltInTools } from '../src/plugins/builtInTools.js'
import { TOOL_TIER_ACTIVITY } from '../src/temporal/toolTier.js'
import { toolActivities } from '../src/temporal/toolActivities.js'
import { defineTool, json, type Tool } from '../src/tools/registry.js'
import { services } from './helpers/mcp.js'
import { memoryAccess, rowsFromOverrides } from './support/memoryAccess.js'

// #2171: a durable session asks, per call, the effective tier and disabled flag of one of
// ScadBuddy's own tools, from the same `effectiveTool` read as every other gate.

const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const tool = (name: string, risk: 'read' | 'write' | 'outward'): Tool =>
  defineTool({ name, description: name, input: z.object({}), risk, routes: [], handler: async () => json({ ran: name }) })
const TOOLS = [tool('look', 'read'), tool('send', 'outward'), tool('ask_user', 'read')]

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
  const svc = services({ toolOverrides: new BuiltInTools(access, sets).registryOverrides() })
  const set = (tool_tiers: Record<string, string>, disabled_tools: string[]) =>
    access.replaceTools(
      BUILTIN_SOURCES.scadbuddy,
      Object.fromEntries(rowsFromOverrides(BUILTIN_SOURCES.scadbuddy, tool_tiers, disabled_tools).map((r) => [r.item, r.access as ToolAccess])),
    )
  const activity = toolActivities(TOOLS, { services: svc, sessions: { ownerOf: async () => undefined } })[TOOL_TIER_ACTIVITY]!
  return { set, activity }
}

const env = (workflowId: string) =>
  new MockActivityEnvironment({
    activityId: 'tier-1',
    workflowExecution: { workflowId, runId: 'run-1' },
    activityType: TOOL_TIER_ACTIVITY,
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

describe("a durable session's tool tier (#2171)", () => {
  it('answers a lowered outward tool as write and a raised read tool as outward', async () => {
    const { set, activity } = rig()
    const session = env(`session-${SESSION}`)
    expect(await session.run(activity, { tool: 'send' })).toEqual({ tier: 'outward', disabled: false })
    await set({ send: 'write', look: 'outward' }, [])
    expect(await session.run(activity, { tool: 'send' })).toEqual({ tier: 'write', disabled: false })
    expect(await session.run(activity, { tool: 'look' })).toEqual({ tier: 'outward', disabled: false })
  })

  it('answers an off tool as disabled', async () => {
    const { set, activity } = rig()
    await set({}, ['look'])
    expect(await env(`session-${SESSION}`).run(activity, { tool: 'look' })).toEqual({ tier: 'read', disabled: true })
  })

  it("refuses a flow run's workflow and an unknown tool", async () => {
    const { activity } = rig()
    expect((await failure(env(`flow-${SESSION}`).run(activity, { tool: 'look' }))).type).toBe('UnknownSession')
    const session = env(`session-${SESSION}`)
    const unknown = await failure(session.run(activity, { tool: 'no_such_tool' }))
    expect([unknown.type, unknown.nonRetryable]).toEqual(['ToolError', true])
    expect((await failure(session.run(activity, { tool: 'ask_user' }))).type).toBe('ToolError')
    expect((await failure(session.run(activity, {}))).type).toBe('ToolError')
  })
})
