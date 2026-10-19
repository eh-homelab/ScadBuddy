import { ApplicationFailure, CancelledFailure } from '@temporalio/common'
import { MockActivityEnvironment } from '@temporalio/testing'
import { createHash } from 'node:crypto'
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
import { decidedCalls, DESCRIBE_CALL_ACTIVITY, FLOW_IMAGES_FULL, gateActivities, TOOL_IMAGES_TOOL, toolActivities, type ToolActivityDeps } from '../src/temporal/toolActivities.js'
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
  return json({ principal: ctx.principal, session: ctx.session, gate: ctx.gate ?? null, flowTurn: ctx.flowTurn ?? null })
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

function env(workflowId = `session-${SESSION}`, activityId = 'tool-toolu_01', runId = 'run-1') {
  return new MockActivityEnvironment({
    activityId,
    workflowExecution: { workflowId, runId },
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
  it("registers one activity per tool, under its name, and a flow's tier activity (#2704)", () => {
    expect(Object.keys(harness().activities).sort()).toEqual(['boom', 'flow_tool_tier', 'send', 'slow', 'whoami'])
  })

  it("runs the tool as the session's owner, in the session, and returns its content", async () => {
    const h = harness()
    const content = await env().run(h.activities.whoami!, {})
    expect(text(content)).toContain('"session": "' + SESSION)
    expect(text(content)).toContain('"gate": "workflow"')
    expect(text(content)).toContain('"id": "browser"')
    // A session's own call is no flow's: a turn it sends is never marked a flow turn (#2481).
    expect(JSON.parse(text(content)).flowTurn).toBeNull()
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

  it("keeps a result's images in the session's blobs and names them for view_tool_images (#2320)", async () => {
    const PNG = 'iVBORw0KGgo='
    const pic = defineTool({
      name: 'pic',
      description: 'an image and words',
      input: z.object({}),
      risk: 'read',
      routes: [],
      handler: async () => ({
        content: [
          { type: 'text', text: 'first' },
          { type: 'image', data: PNG, mimeType: 'image/png' },
          { type: 'text', text: 'second' },
          // Not the type it claims: never stored, and said so.
          { type: 'image', data: PNG, mimeType: 'image/jpeg' },
        ],
      }),
    })
    const stored: { session: string; names: string[]; toolUseId: string | undefined }[] = []
    const blobs = {
      put: async (session: string, images: readonly { name: string }[], toolUseId?: string) =>
        void stored.push({ session, names: images.map((i) => i.name), toolUseId }),
    }
    const out = String(await env().run(harness({ blobs }, [pic]).activities.pic as (input: unknown) => Promise<unknown>, {}))
    const name = `${createHash('sha256').update(Buffer.from(PNG, 'base64')).digest('hex')}.png`
    // Recorded under the call that produced it, which the panel's tool.result names it under (#2478).
    expect(stored).toEqual([{ session: SESSION, names: [name], toolUseId: 'toolu_01' }])
    expect(out).toContain('first')
    expect(out).toContain('second')
    expect(out).toContain(`[image ${name}]`)
    expect(out).toContain(TOOL_IMAGES_TOOL)
    expect(out).toContain('[an image the model cannot take]')
    expect(out).not.toContain(PNG)
    expect(out).not.toContain('classic session')
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

  it("runs a turn sent with its sender's tiers with those tiers, cut to what the owner holds now (#2320)", async () => {
    const sent = { ...BEARER, turnTiers: ['read', 'write', 'outward'] as const }
    const h = harness({ currentTiers: async () => ['read', 'write', 'outward'] }, TOOLS, sent)
    const content = await env(`session-${SESSION}`, 'tool-toolu_approved').run(h.activities.send!, { to: 'x' })
    expect(text(content)).toContain('"sent": "x"')
    // The token was cut to read since the turn was sent.
    const cut = harness({ currentTiers: async () => ['read'] }, TOOLS, sent)
    const refused = await failure(env(`session-${SESSION}`, 'tool-toolu_approved').run(cut.activities.send!, { to: 'x' }))
    expect(refused.message).toContain('needs the "outward" tier')
    // A turn sent with no tiers keeps the owner's default: a token owner reads only.
    const plain = harness({}, TOOLS, BEARER)
    const read = await failure(env(`session-${SESSION}`, 'tool-toolu_approved').run(plain.activities.send!, { to: 'x' }))
    expect(read.message).toContain('needs the "outward" tier')
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

  /** A run whose `call_ok` is approved, for the input the default audit hashes as `hash:send`. */
  function flowRuns(owner: Owner | undefined = BROWSER, asked: string[] = [], hash = 'hash:send'): FlowRuns {
    return {
      startedBy: async (id) => (id === RUN ? owner : undefined),
      approved: async (requestId, inputHash) => {
        asked.push(requestId)
        return requestId.endsWith(':call_ok') && inputHash === hash
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
    // #2481: the run and its step, from the activity's own context, for a turn it sends.
    expect(JSON.parse(text(content)).flowTurn).toEqual({ run_id: RUN, call_id: 'call_1' })
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

  it('runs an approved call only with the tool and input the approval was given for (#2318)', async () => {
    const hash = (tool: string, input: Record<string, unknown>) => `${tool}:${JSON.stringify(input)}`
    const approvedFor = hash('send', { to: 'printer' })
    const h = harness({
      flows: flowRuns(BROWSER, [], approvedFor),
      audit: { record: async () => {}, hash, summarise: (tool) => tool },
    })
    const content = await env(FLOW, 'tool-call_ok').run(h.activities.send!, { to: 'printer' })
    expect(text(content)).toContain('"sent": "printer"')
    // The same call id with other input: not what the person approved.
    const refused = await failure(env(FLOW, 'tool-call_ok').run(h.activities.send!, { to: 'elsewhere' }))
    expect(refused.nonRetryable).toBe(true)
    expect(refused.type).toBe('NotApproved')
  })

  it("refuses a grant's call as GrantRevoked once its grant was revoked or expired (#2481)", async () => {
    const audits: unknown[] = []
    const flows: FlowRuns = {
      ...flowRuns(),
      lapsed: async (requestId, inputHash) => requestId.endsWith(':call_lapsed') && inputHash === 'hash:send',
    }
    const h = harness({ flows, audit: { record: async (row) => void audits.push(row), hash: () => 'hash:send', summarise: (tool) => tool } })
    const refused = await failure(env(FLOW, 'tool-call_lapsed').run(h.activities.send!, { to: 'printer' }))
    expect(refused.nonRetryable).toBe(true)
    expect(refused.type).toBe('GrantRevoked')
    expect(refused.message).toContain("the run's grant was revoked or expired before this call ran")
    expect(audits).toEqual([expect.objectContaining({ action: 'send', outcome: 'refused' })])
    // Not approved and no grant behind it: still NotApproved.
    expect((await failure(env(FLOW, 'tool-call_2').run(h.activities.send!, { to: 'printer' }))).type).toBe('NotApproved')
  })

  it("runs a token starter's call with the tiers its token holds now (#2320)", async () => {
    const live = harness({ flows: flowRuns(BEARER), currentTiers: async () => ['read', 'write', 'outward'] })
    const content = await env(FLOW, 'tool-call_ok').run(live.activities.send!, { to: 'x' })
    expect(text(content)).toContain('"sent": "x"')
    // A revoked token holds nothing, and a starter whose tiers are kept nowhere reads only.
    const revoked = harness({ flows: flowRuns(BEARER), currentTiers: async () => [] })
    expect((await failure(env(FLOW, 'tool-call_ok').run(revoked.activities.whoami!, {}))).message).toContain('needs the "read" tier')
    const unknown = harness({ flows: flowRuns(BEARER), currentTiers: async () => undefined })
    expect((await failure(env(FLOW, 'tool-call_ok').run(unknown.activities.send!, { to: 'x' }))).message).toContain(
      'needs the "outward" tier',
    )
  })

  it('refuses a flow with no run recorded, or with no store to read it from, and runs nothing', async () => {
    const other = `flow-${'1'.repeat(8)}-0000-4000-8000-000000000000`
    const unknown = await failure(env(other, 'tool-call_1').run(harness({ flows: flowRuns() }).activities.whoami!, {}))
    expect(unknown.nonRetryable).toBe(true)
    expect(unknown.type).toBe('UnknownFlow')
    const unread = await failure(env(FLOW, 'tool-call_1').run(harness().activities.whoami!, {}))
    expect(unread.type).toBe('UnknownFlow')
  })

  // #2258: without the execution's run id no decision could match, so it is not refused NotApproved unexplained.
  it('fails a flow call that carries no workflow execution run id, naming it, and runs nothing', async () => {
    const h = harness({ flows: flowRuns(BROWSER) })
    const missing = await failure(env(FLOW, 'tool-call_ok', '').run(h.activities.send!, { to: 'printer' }))
    expect([missing.type, missing.nonRetryable]).toEqual(['UnknownFlow', true])
    expect(missing.message).toMatch(/carries no workflow execution run id/)
    expect(h.audits).toEqual([])
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

  // #2477: a flow call's images are kept under the run's subject; the flow gets pointers.
  const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
  const PNG_NAME = `${createHash('sha256').update(Buffer.from(PNG, 'base64')).digest('hex')}.png`
  const pic = defineTool({
    name: 'pic',
    description: 'an image and words',
    input: z.object({}),
    risk: 'read',
    routes: [],
    handler: async () => ({
      content: [
        { type: 'text', text: 'first' },
        { type: 'image', data: PNG, mimeType: 'image/png' },
        { type: 'image', data: PNG, mimeType: 'image/png' },
        { type: 'image', data: PNG, mimeType: 'image/jpeg' },
      ],
    }),
  })

  function memoryFlowBlobs(max = Infinity) {
    const rows = new Map<string, { mediaType: 'image/png'; bytes: Buffer }>()
    return {
      rows,
      put: async (subject: string, images: readonly { name: string; mediaType: string; bytes: Buffer }[]) => {
        const kept: string[] = []
        for (const i of images) {
          const key = `${subject}/${i.name}`
          if (!rows.has(key) && rows.size >= max) continue
          rows.set(key, { mediaType: 'image/png', bytes: i.bytes })
          kept.push(i.name)
        }
        return kept
      },
      get: async (subject: string, name: string) => rows.get(`${subject}/${name}`),
    }
  }

  it("keeps a flow call's images under the run's subject and answers with pointers, never the bytes", async () => {
    const flowBlobs = memoryFlowBlobs()
    const out = await env(FLOW, 'tool-call_1').run(harness({ flows: flowRuns(), flowBlobs }, [pic]).activities.pic!, {})
    const { text: said, images } = out as { text: string; images: unknown }
    expect(images).toEqual([{ name: PNG_NAME, mediaType: 'image/png', bytes: Buffer.from(PNG, 'base64').length }])
    expect(said).toContain('first')
    expect(said.split(`[image ${PNG_NAME}]`)).toHaveLength(3)
    expect(said).toContain('[an image the model cannot take]')
    expect([...flowBlobs.rows.keys()]).toEqual([`${FLOW}/${PNG_NAME}`])
    expect(JSON.stringify(out)).not.toContain(PNG)
    expect(JSON.stringify(out)).not.toContain(PNG.slice(0, 16))
  })

  it("says so, with no pointer, for an image past the run's cap (#2578)", async () => {
    const out = await env(FLOW, 'tool-call_1').run(harness({ flows: flowRuns(), flowBlobs: memoryFlowBlobs(0) }, [pic]).activities.pic!, {})
    expect(typeof out).toBe('string')
    expect(String(out)).toContain(FLOW_IMAGES_FULL)
    expect(out).not.toContain('[image ')
  })

  it('answers text alone when a flow call returned no image, and a placeholder with no store', async () => {
    const plain = await env(FLOW, 'tool-call_1').run(harness({ flows: flowRuns(), flowBlobs: memoryFlowBlobs() }).activities.whoami!, {})
    expect(typeof plain).toBe('string')
    const none = await env(FLOW, 'tool-call_1').run(harness({ flows: flowRuns() }, [pic]).activities.pic!, {})
    expect(String(none).split('\n[an image]')).toHaveLength(4)
    expect(none).not.toContain('[image ')
  })

  it("gives a flow's tools the run's own images, and a session's none", async () => {
    const flowBlobs = memoryFlowBlobs()
    await flowBlobs.put(FLOW, [{ name: PNG_NAME, mediaType: 'image/png', bytes: Buffer.from(PNG, 'base64') }])
    const peek = defineTool({
      name: 'peek',
      description: 'reads a flow image',
      input: z.object({ name: z.string() }),
      risk: 'read',
      routes: [],
      handler: async ({ name }, ctx) => {
        const found = await ctx.flowImages?.get(name)
        return json({ has: ctx.flowImages !== undefined, found: found ? found.bytes.length : null })
      },
    })
    const h = harness({ flows: flowRuns(), flowBlobs }, [peek])
    expect(text(await env(FLOW, 'tool-call_1').run(h.activities.peek!, { name: PNG_NAME }))).toContain('"found": 68')
    const other = `flow-${'1'.repeat(8)}-0000-4000-8000-000000000000`
    const elsewhere = harness({ flows: { ...flowRuns(), startedBy: async () => BROWSER }, flowBlobs }, [peek])
    expect(text(await env(other, 'tool-call_1').run(elsewhere.activities.peek!, { name: PNG_NAME }))).toContain('"found": null')
    expect(text(await env().run(h.activities.peek!, { name: PNG_NAME }))).toContain('"has": false')
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

describe("decidedCalls: a released turn's approved call (unified runtime spec §5.4 step 5)", () => {
  const OTHER = '9d1f2a3b-4c5d-4e6f-8a7b-0c1d2e3f4a5b'
  let ran = 0
  const keys: (string | undefined)[] = []
  const ship = defineTool({
    name: 'ship',
    description: 'Ships something',
    input: z.object({ what: z.string() }),
    risk: 'outward',
    routes: ['POST /api/v1/workflows/{definition_id}/runs'],
    handler: async (_input, ctx) => {
      ran++
      keys.push(ctx.callKey)
      return json({ shipped: true })
    },
  })
  const asked: string[] = []
  const run = decidedCalls([ship], {
    services: services(),
    sessions: { ownerOf: async () => BROWSER },
    // Every request id is approved: what decides is whose it is.
    approvals: {
      approved: async (requestId) => {
        asked.push(requestId)
        return true
      },
    },
  })
  const call = (requestId: string, toolUseId = 'toolu_1') => ({
    sessionId: SESSION,
    requestId,
    toolUseId,
    tool: 'mcp__scadbuddy__ship',
    input: { what: 'box' },
    signal: new AbortController().signal,
    heartbeat: () => {},
  })

  it("runs a call whose request id is this session's entry for it", async () => {
    ran = 0
    expect(await run(call(`durable:${SESSION}:run-1:toolu_1`))).toMatchObject({ ran: true, ok: true })
    expect(ran).toBe(1)
  })

  it('keys every run of one call on its request id, so a re-placed continuation re-attaches to the first run (spec §7)', async () => {
    keys.length = 0
    const rid = `durable:${SESSION}:run-1:toolu_1`
    await run(call(rid))
    await run(call(rid))
    expect(keys).toEqual([rid, rid])
  })

  it.each([
    ["another session's entry", `durable:${OTHER}:run-1:toolu_1`, 'toolu_1'],
    ['an entry for another call', `durable:${SESSION}:run-1:toolu_2`, 'toolu_1'],
    ['no durable entry at all', `flow:${SESSION}:run-1:toolu_1`, 'toolu_1'],
    ['an id with no run', `durable:${SESSION}::toolu_1`, 'toolu_1'],
  ])('refuses %s before any approval is read, and runs nothing', async (_what, requestId, toolUseId) => {
    ran = 0
    asked.length = 0
    expect(await run(call(requestId, toolUseId))).toMatchObject({ ran: false, text: expect.stringContaining('is not session') })
    expect(ran).toBe(0)
    expect(asked).toEqual([])
  })

  // #3441: only ScadBuddy's own tools can run outside the conversation; a released park of
  // any other (a remote plugin's, http_request, a package's Bash) is told it was not run,
  // so nothing outside the backend's Idempotency-Key can run twice.
  it.each(['mcp__plugin_notes_api__send', 'mcp__scadbuddy_http__http_request', 'Bash', 'mcp__scadbuddy__unknown'])(
    'tells the model %s was not run, and runs nothing',
    async (tool) => {
      ran = 0
      asked.length = 0
      expect(await run({ ...call(`durable:${SESSION}:run-1:toolu_1`), tool })).toEqual({
        ran: false,
        text: `${tool} cannot be run outside the conversation, so it was not run`,
      })
      expect(ran).toBe(0)
      expect(asked).toEqual([])
    },
  )
})
