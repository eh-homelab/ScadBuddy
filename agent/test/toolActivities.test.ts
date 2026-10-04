import { ApplicationFailure, CancelledFailure } from '@temporalio/common'
import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import { createBackendClient } from '../src/api/backend.js'
import type { AuditEntry } from '../src/audit/log.js'
import { TabHub } from '../src/bridge/hub.js'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import type { Owner } from '../src/sessions/protocol.js'
import type { TouchedCall } from '../src/sessions/touched.js'
import { toolActivities, type ToolActivityDeps } from '../src/temporal/toolActivities.js'
import { AUTHOR_SESSION_HEADER } from '../src/tools/authorship.js'
import { browserTools } from '../src/tools/browser.js'
import { PendingActionStore } from '../src/tools/pending.js'
import { defineTool, json, runToolWithOutcome, ToolError, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'

// Every tool as an activity on `agent-tools` (spec 2026-10-01 §6.3, #1055), run in
// @temporalio/testing's MockActivityEnvironment (spec §8).

const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const BROWSER: Owner = { kind: 'browser', id: 'browser', label: 'You' }
const BEARER: Owner = { kind: 'bearer', id: 'token:7', label: 'MCP token' }

function seen(ctx: ToolContext) {
  return json({ principal: ctx.principal, session: ctx.session, gate: ctx.gate ?? null })
}

const whoami = defineTool({
  name: 'whoami',
  description: 'who runs this',
  input: z.object({ n: z.number().default(1) }),
  risk: 'read',
  routes: [],
  handler: async (_args, ctx) => {
    await ctx.backend.GET('/api/v1/settings')
    return seen(ctx)
  },
})
const boom = defineTool({
  name: 'boom',
  description: 'fails',
  input: z.object({}),
  risk: 'read',
  routes: [],
  handler: async () => {
    throw new ToolError('it broke')
  },
})
const send = defineTool({
  name: 'send',
  description: 'outward',
  input: z.object({ to: z.string() }),
  risk: 'outward',
  routes: [],
  handler: async (args, ctx) => json({ sent: args.to, gate: ctx.gate ?? null }),
})
const slow = defineTool({
  name: 'slow',
  description: 'waits for a cancel',
  input: z.object({}),
  risk: 'read',
  routes: [],
  handler: async (_args, ctx) => {
    await new Promise((resolve) => ctx.signal.addEventListener('abort', resolve, { once: true }))
    throw new ToolError('stopped')
  },
})
const TOOLS: readonly Tool[] = [whoami, boom, send, slow]

type Harness = {
  activities: Record<string, (input: unknown) => Promise<unknown>>
  audits: AuditEntry[]
  touched: TouchedCall[]
  headers: Headers[]
}

function harness(overrides: Partial<ToolActivityDeps> = {}, tools: readonly Tool[] = TOOLS, owner = BROWSER): Harness {
  const audits: AuditEntry[] = []
  const touched: TouchedCall[] = []
  const headers: Headers[] = []
  const backend = createBackendClient(BACKEND, async (request) => {
    headers.push((request as Request).headers)
    return Response.json({})
  })
  const deps: ToolActivityDeps = {
    services: services({ backend, touched: { record: async (call) => void touched.push(call) } }),
    sessions: { ownerOf: async (id) => (id === SESSION ? owner : undefined) },
    audit: {
      record: async (entry) => void audits.push(entry),
      hash: (tool) => `hash:${tool}`,
      summarise: (tool) => `summary:${tool}`,
    },
    ...overrides,
  }
  return { activities: toolActivities(tools, deps), audits, touched, headers }
}

function env(workflowId = `session-${SESSION}`, activityId = 'tool-toolu_01') {
  return new MockActivityEnvironment({
    activityId,
    workflowExecution: { workflowId, runId: 'run-1' },
    activityType: 'whoami',
    taskQueue: 'agent-tools',
  })
}

/** The tool's own text, out of its untrusted-data envelope. */
function text(content: unknown): string {
  const [block] = content as { type: string; text: string }[]
  return unwrapUntrusted(block!.text)
}

async function failure(promise: Promise<unknown>): Promise<ApplicationFailure> {
  const err = await promise.then(
    () => undefined,
    (e: unknown) => e,
  )
  expect(err).toBeInstanceOf(ApplicationFailure)
  return err as ApplicationFailure
}

describe('tool activities', () => {
  it('registers one activity per tool, under its name', () => {
    expect(Object.keys(harness().activities).sort()).toEqual(['boom', 'send', 'slow', 'whoami'])
  })

  it("runs the tool as the session's owner, in the session, and returns its content", async () => {
    const h = harness()
    const content = await env().run(h.activities.whoami!, {})
    expect(text(content)).toContain('"session": "' + SESSION)
    expect(text(content)).toContain('"gate": "workflow"')
    expect(text(content)).toContain('"id": "browser"')
    // The commit trailer names the session (authorship.ts, #252).
    expect(h.headers[0]!.get(AUTHOR_SESSION_HEADER)).toBe(SESSION)
    // What the session touched (#931), and the audit row the harness would write.
    expect(h.touched).toEqual([expect.objectContaining({ sessionId: SESSION, ok: true, input: { n: 1 } })])
    expect(h.audits).toEqual([
      expect.objectContaining({
        kind: 'tool_call',
        action: 'whoami',
        surface: 'harness',
        actor: BROWSER,
        sessionId: SESSION,
        toolUseId: 'toolu_01',
        tier: 'read',
        inputHash: 'hash:whoami',
        outcome: 'ok',
      }),
    ])
  })

  it('runs an outward tool without preparing an approval: the workflow decided it', async () => {
    const pending = new PendingActionStore()
    const h = harness({ services: services({ pending }) })
    const content = await env().run(h.activities.send!, { to: 'printer' })
    expect(text(content)).toContain('"sent": "printer"')
    // /mcp, with no gate, still only prepares it.
    const run = await runToolWithOutcome(send, { to: 'printer' }, {
      ...services({ pending }),
      principal: { id: 'browser', kind: 'browser', tiers: ['read', 'write', 'outward'] },
      progress: async () => {},
      signal: new AbortController().signal,
    })
    expect(run.outcome).toBe('refused')
    expect(JSON.stringify(run.result)).toContain('pending_approval')
  })

  it("fails without a retry when the tool's result is an error, a refusal or bad arguments", async () => {
    const h = harness()
    const broke = await failure(env().run(h.activities.boom!, {}))
    expect(broke.nonRetryable).toBe(true)
    expect(broke.type).toBe('ToolError')
    expect(broke.message).toContain('it broke')
    const invalid = await failure(env().run(h.activities.send!, { to: 3 }))
    expect(invalid.nonRetryable).toBe(true)
    expect(invalid.message).toContain('invalid arguments')
    // A token owner holds `read` only (harnessPrincipal), so an outward tool is refused.
    const refused = await failure(env().run(harness({}, TOOLS, BEARER).activities.send!, { to: 'x' }))
    expect(refused.nonRetryable).toBe(true)
    expect(refused.message).toContain('needs the "outward" tier')
    expect(h.audits.map((a) => a.outcome)).toEqual(['error', 'error'])
  })

  it('refuses a workflow that is not a known session, and runs nothing', async () => {
    const h = harness()
    for (const workflowId of [`session-${'1'.repeat(8)}-0000-4000-8000-000000000000`, 'flow-abc', 'session-not-a-uuid']) {
      const refused = await failure(env(workflowId).run(h.activities.whoami!, {}))
      expect(refused.nonRetryable).toBe(true)
      expect(refused.type).toBe('UnknownSession')
    }
    expect(h.headers).toEqual([])
    expect(h.audits).toEqual([])
  })

  it('leaves a failed owner lookup to the retry policy', async () => {
    const h = harness({
      sessions: {
        ownerOf: async () => {
          throw new Error('connection refused')
        },
      },
    })
    const err = await env()
      .run(h.activities.whoami!, {})
      .catch((e: unknown) => e)
    expect(err).toBeInstanceOf(Error)
    expect(err).not.toBeInstanceOf(ApplicationFailure)
  })

  it('fails a browser tool at once when the session has no paired tab', async () => {
    const tabs = new TabHub()
    try {
      const h = harness({ services: services({ browser: tabs }) }, browserTools)
      const name = 'browser_snapshot'
      const started = Date.now()
      const err = await failure(env().run(h.activities[name]!, {}))
      expect(err.message).toMatch(/no browser|paired/i)
      expect(Date.now() - started).toBeLessThan(2000)
    } finally {
      tabs.close()
    }
  })

  it("aborts the tool's signal when the activity is cancelled", async () => {
    const h = harness({ heartbeatMs: 5 })
    const e = env()
    const running = e.run(h.activities.slow!, {})
    setTimeout(() => e.cancel(), 20)
    await expect(running).rejects.toBeInstanceOf(CancelledFailure)
  })
})
