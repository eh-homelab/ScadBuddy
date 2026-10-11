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
import { answerResult } from '../src/gate/answers.js'
import { answeredText, timedOutText } from '../src/harness/attention.js'
import { answersText } from '../src/harness/questions.js'
import type { FlowRuns } from '../src/temporal/flowRuns.js'
import { DESCRIBE_CALL_ACTIVITY, DURABLE_IMAGE_NOTE, gateActivities, toolActivities, type ToolActivityDeps } from '../src/temporal/toolActivities.js'
import { DURABLE_ONLY_TOOLS } from '../src/tools/answerTools.js'
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
    approvals: { approved: async (requestId) => requestId.endsWith(':toolu_approved') },
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

/** The tool's own text, out of its untrusted-data envelope (the activity returns the result's text). */
function text(content: unknown): string {
  expect(typeof content).toBe('string')
  return unwrapUntrusted(content as string)
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

  it('runs an approved outward tool without preparing an approval: the workflow decided it', async () => {
    const pending = new PendingActionStore()
    const h = harness({ services: services({ pending }) })
    const content = await env(undefined, 'tool-toolu_approved').run(h.activities.send!, { to: 'printer' })
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
    const invalid = await failure(env(undefined, 'tool-toolu_approved').run(h.activities.send!, { to: 3 }))
    expect(invalid.nonRetryable).toBe(true)
    expect(invalid.message).toContain('invalid arguments')
    // A token owner holds `read` only (harnessPrincipal), so an outward tool is refused.
    const refused = await failure(env(undefined, 'tool-toolu_approved').run(harness({}, TOOLS, BEARER).activities.send!, { to: 'x' }))
    expect(refused.nonRetryable).toBe(true)
    expect(refused.message).toContain('needs the "outward" tier')
    expect(h.audits.map((a) => a.outcome)).toEqual(['error', 'error'])
  })

  // Security review of 5b (missing-authorization): `gate: 'workflow'` must not be
  // borrowed by any workflow named after a durable session. A gated call runs only
  // with an `approved` outcome recorded for its own request id (session, run, call).
  it('refuses a gated call with no approval recorded for its request id, and runs nothing', async () => {
    const asked: string[] = []
    const h = harness({
      approvals: {
        approved: async (requestId) => {
          asked.push(requestId)
          return false
        },
      },
    })
    const refused = await failure(env(undefined, 'tool-toolu_07').run(h.activities.send!, { to: 'printer' }))
    expect(refused.nonRetryable).toBe(true)
    expect(refused.type).toBe('NotApproved')
    expect(asked).toEqual([`durable:${SESSION}:run-1:toolu_07`])
    expect(h.audits).toEqual([expect.objectContaining({ action: 'send', outcome: 'refused', requestId: `durable:${SESSION}:run-1:toolu_07` })])
    // An ungated tool never asks.
    await env().run(h.activities.whoami!, {})
    expect(asked).toHaveLength(1)
    // Without the record store, a gated call cannot be checked, so it is refused.
    const none = await failure(env(undefined, 'tool-toolu_approved').run(harness({ approvals: undefined }).activities.send!, { to: 'x' }))
    expect(none.type).toBe('NotApproved')
  })

  it('joins a result into the text the plugin hands the model, and names an image it cannot', async () => {
    const pic = defineTool({
      name: 'pic',
      description: 'an image and words',
      input: z.object({}),
      risk: 'read',
      routes: [],
      handler: async () => ({
        content: [
          { type: 'text', text: 'first' },
          { type: 'image', data: 'iVBORw0KGgo=', mimeType: 'image/png' },
          { type: 'text', text: 'second' },
        ],
      }),
    })
    const out = await env().run(harness({}, [pic]).activities.pic!, {})
    expect(typeof out).toBe('string')
    expect(out).toContain('first')
    expect(out).toContain('second')
    expect(out).toContain(DURABLE_IMAGE_NOTE)
    expect(out).not.toContain('iVBORw0KGgo')
  })

  it('refuses a workflow that is not a known session, and runs nothing', async () => {
    const h = harness()
    for (const workflowId of [`session-${'1'.repeat(8)}-0000-4000-8000-000000000000`, 'flow-abc', 'session-not-a-uuid', `session-${SESSION.toUpperCase()}`]) {
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

// #1057 (plan 2026-10-09 Task D1): a flow run's call, from ProjectWorkflow
// (`flow-<run id>`), runs as the run's starter, and an outward one only with a person's
// approval recorded for `flow:<run>:<workflow run>:<call>`.
describe('flow runs in tool activities', () => {
  const RUN = '7e2a1c3b-4d5e-4f60-8a9b-0c1d2e3f4a5b'
  const FLOW = `flow-${RUN}`

  function flowRuns(owner: Owner | undefined = BROWSER, asked: string[] = []): FlowRuns {
    return {
      startedBy: async (id) => (id === RUN ? owner : undefined),
      approved: async (requestId) => {
        asked.push(requestId)
        return requestId.endsWith(':call_ok')
      },
    }
  }

  it("runs a flow's read tool as the run's starter, in no session, and asks no session owner", async () => {
    const owners: string[] = []
    const h = harness({ flows: flowRuns(), sessions: { ownerOf: async (id) => void owners.push(id) } })
    const content = await env(FLOW, 'tool-call_1').run(h.activities.whoami!, {})
    expect(text(content)).toContain('"id": "browser"')
    expect(text(content)).toContain('"gate": "workflow"')
    expect(text(content)).not.toContain('"session"')
    expect(owners).toEqual([])
    expect(h.headers[0]!.get(AUTHOR_SESSION_HEADER)).toBeNull()
    expect(h.audits).toEqual([
      expect.objectContaining({
        action: 'whoami',
        actor: BROWSER,
        sessionId: null,
        toolUseId: 'call_1',
        requestId: `flow:${RUN}:run-1:call_1`,
        outcome: 'ok',
      }),
    ])
    expect(h.touched).toEqual([])
  })

  it('refuses a gated call with no approval recorded for it, and runs it once one is', async () => {
    const asked: string[] = []
    const h = harness({ flows: flowRuns(BROWSER, asked) })
    const refused = await failure(env(FLOW, 'tool-call_2').run(h.activities.send!, { to: 'printer' }))
    expect(refused.nonRetryable).toBe(true)
    expect(refused.type).toBe('NotApproved')
    expect(asked).toEqual([`flow:${RUN}:run-1:call_2`])
    expect(h.audits).toEqual([
      expect.objectContaining({ action: 'send', outcome: 'refused', sessionId: null, requestId: `flow:${RUN}:run-1:call_2` }),
    ])
    const content = await env(FLOW, 'tool-call_ok').run(h.activities.send!, { to: 'printer' })
    expect(text(content)).toContain('"sent": "printer"')
  })

  it('holds a token starter to its read tier, as its durable sessions are', async () => {
    const h = harness({ flows: flowRuns(BEARER) })
    const refused = await failure(env(FLOW, 'tool-call_ok').run(h.activities.send!, { to: 'x' }))
    expect(refused.message).toContain('needs the "outward" tier')
  })

  it('refuses a flow with no run recorded, or with no store to read it from, and runs nothing', async () => {
    const other = `flow-${'1'.repeat(8)}-0000-4000-8000-000000000000`
    const unknown = await failure(env(other, 'tool-call_1').run(harness({ flows: flowRuns() }).activities.whoami!, {}))
    expect(unknown.nonRetryable).toBe(true)
    expect(unknown.type).toBe('UnknownFlow')
    const unread = await failure(env(FLOW, 'tool-call_1').run(harness().activities.whoami!, {}))
    expect(unread.type).toBe('UnknownFlow')
  })

  it('refuses the browser and answer tools: a flow has no tab and no session', async () => {
    const tools = [...browserTools, ...DURABLE_ONLY_TOOLS]
    const h = harness({ flows: flowRuns() }, tools)
    for (const name of ['browser_click', DURABLE_ONLY_TOOLS[0]!.name]) {
      const refused = await failure(env(FLOW, 'tool-call_1').run(h.activities[name]!, {}))
      expect(refused.nonRetryable).toBe(true)
      expect(refused.type).toBe('NotForFlows')
    }
    expect(h.audits).toEqual([])
  })
})

// spec §6.6: a durable call's gate entry. Every call's audit row names its request id,
// an `answer` tool's result is the recorded answer, and gate.describe_call gives
// DurableSession an approval's summary and hash.
describe('the gate in tool activities', () => {
  const REQUEST = `durable:${SESSION}:run-1:toolu_01`

  it("names the call's request id on its audit row", async () => {
    const h = harness()
    await env().run(h.activities.whoami!, {})
    expect(h.audits[0]).toMatchObject({ requestId: REQUEST, toolUseId: 'toolu_01' })
  })

  it("returns an answer tool's recorded answer, never its handler's, and fails a call nobody answered", async () => {
    const asked: string[] = []
    const answers = {
      result: async (requestId: string, tool: string, input: unknown) => {
        asked.push(requestId)
        return answerResult(
          requestId === REQUEST ? { outcome: 'answered', response: { answers: ['Red', 'Both'] }, reason: null } : undefined,
          tool,
          input,
        )
      },
    }
    const h = harness({ answers }, [...TOOLS, ...DURABLE_ONLY_TOOLS])
    const questions = [
      { question: 'Which colour?', header: 'Colour', options: [{ label: 'Red', description: 'r' }, { label: 'Blue', description: 'b' }], multiSelect: false },
      { question: 'Which parts?', header: 'Parts', options: [{ label: 'Both', description: 'x' }, { label: 'Lid', description: 'y' }], multiSelect: false },
    ]
    const content = await env().run(h.activities.ask_user!, { questions })
    expect(content).toBe(answersText({ 'Which colour?': 'Red', 'Which parts?': 'Both' }))
    expect(asked).toEqual([REQUEST])
    expect(h.audits).toEqual([expect.objectContaining({ action: 'ask_user', requestId: REQUEST, outcome: 'ok' })])
    const failed = await failure(env(undefined, 'tool-toolu_02').run(h.activities.wait_for_user!, { reason: 'blocked', message: 'stuck' }))
    expect(failed.nonRetryable).toBe(true)
    expect(failed.message).toMatch(/did not answer/)
    // Without the database, an answer tool refuses.
    await failure(env().run(harness({}, DURABLE_ONLY_TOOLS).activities.ask_user!, { questions }))
  })

  it('describes a call with the summary and hash a classic approval would carry', async () => {
    const audit = { record: async () => {}, hash: (tool: string) => `hash:${tool}`, summarise: (tool: string) => `summary:${tool}` }
    const sessions = { ownerOf: async (id: string) => (id === SESSION ? BROWSER : undefined) }
    const describeCall = gateActivities({ audit, sessions })[DESCRIBE_CALL_ACTIVITY]!
    expect(await env().run(describeCall, { tool: 'print_output', input: { output: 'box' } })).toEqual({
      summary: 'summary:print_output',
      input_hash: 'hash:print_output',
    })
    await failure(env().run(describeCall, { tool: 'print_output', input: [] }))
    await failure(env().run(gateActivities({ sessions })[DESCRIBE_CALL_ACTIVITY]!, { tool: 'x', input: {} }))
    // Security review of 5b: the HMAC key's hashes are given only to a durable session's workflow.
    for (const workflowId of ['flow-abc', `session-${'1'.repeat(8)}-0000-4000-8000-000000000000`]) {
      const refused = await failure(env(workflowId).run(describeCall, { tool: 'print_output', input: {} }))
      expect(refused.type).toBe('UnknownSession')
    }
  })
})

describe('answerResult', () => {
  it('turns a recorded outcome into the tool result the model reads', () => {
    expect(answerResult({ outcome: 'answered', response: { answers: ['Print it'] }, reason: null }, 'wait_for_user', {})).toEqual({
      ok: true,
      text: answeredText('Print it'),
    })
    expect(answerResult({ outcome: 'timed_out', response: null, reason: null }, 'wait_for_user', { timeout_s: 60 })).toEqual({
      ok: true,
      text: timedOutText(60),
    })
    // A timer never answers a question, and a cancel is an error.
    expect(answerResult({ outcome: 'timed_out', response: null, reason: null }, 'ask_user', {}).ok).toBe(false)
    expect(answerResult({ outcome: 'cancelled', response: null, reason: 'a new turn' }, 'ask_user', {})).toEqual({
      ok: false,
      text: 'The user did not answer: a new turn.',
    })
    expect(answerResult({ outcome: 'answered', response: { answers: [] }, reason: null }, 'ask_user', {}).ok).toBe(false)
  })
})
