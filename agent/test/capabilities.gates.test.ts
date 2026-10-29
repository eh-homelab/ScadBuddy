import { randomBytes } from 'node:crypto'
import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { BUILTIN_SOURCES } from '../src/access/model.js'
import { createApp } from '../src/app.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { originPolicy } from '../src/http/origins.js'
import { BuiltInTools } from '../src/plugins/builtInTools.js'
import { CAPABILITIES_PATH } from '../src/routes/capabilities.js'
import { FLOW_TOOLS_PATH } from '../src/routes/flowTools.js'
import { kekFromBase64 } from '../src/secrets.js'
import { toolActivities } from '../src/temporal/toolActivities.js'
import { TOOL_TIER_ACTIVITY } from '../src/temporal/toolTier.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { defineTool, json, runToolWithOutcome } from '../src/tools/registry.js'
import { services } from './helpers/mcp.js'
import { memoryAccess } from './support/memoryAccess.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// #2171 acceptance: access the user sets through /api/v1/ai/capabilities is what every gate
// reads. One memoryAccess sits behind the route and the gates. The stand-in has the name of
// one of ScadBuddy's own outward tools, so the route offers it, but runs nothing real. The
// classic seam and the durable run of a lowered tool are pinned by builtInTools.test.ts
// ("the harness seam allows a lowered outward tool", "the durable activities run a lowered
// outward tool with no approval recorded") over the same store; they are not repeated.

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
  return { app, peer, rows, standIn, handler, svc, lower }
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
