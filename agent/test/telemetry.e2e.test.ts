import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { createBackendClient } from '../src/api/backend.js'
import { connectDatabase, type Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { originPolicy } from '../src/http/origins.js'
import { registerApprovalRoutes } from '../src/routes/approvals.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import { defineTool, text } from '../src/tools/registry.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, collectUntil, manager, tempPaths } from './support/sessions.js'
import { exportedText, resetTracing, testTracing, waitForSpan } from './support/tracing.js'
import { fixedCredentials } from './support/fixedCredentials.js'

// The agent's trace on the real Agent SDK and its bundled Claude Code binary,
// pointed at the fake Anthropic endpoint, with sessions and approvals in
// Postgres (spec 2026-10-01 §8, agent): a turn yields agent.turn → agent.tool,
// and the tool's backend request carries the tool's context; a parked call
// ends its spans before any decision; the decision links to it through
// ai_approvals.traceparent, and agent.turn.resume is its child.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

const spans = testTracing()
const TOKEN = 'gw-tracing-e2e-token-8888999900001111'
const SENTINEL = 's3ntinel-e2e-5c71'
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

/** An outward stand-in for a ScadBuddy tool that calls the backend. */
const PING = defineTool({
  name: 'ping_backend',
  description: 'Ping the backend (test only).',
  input: z.object({ job: z.string() }),
  risk: 'outward',
  routes: ['GET /healthz'],
  handler: async ({ job }, { backend }) => {
    await backend.GET('/healthz')
    return text(`pinged ${job.slice(0, 1)}`)
  },
})
const LIST_MODELS = ALL_TOOLS.find((t) => t.name === 'list_models')!

type Hit = { path: string; traceparent: string | undefined }

describe.skipIf(skip !== undefined)(`agent tracing against the real SDK${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let db: Database
  let schema: string
  let drop: () => Promise<void>
  let stop: AbortController
  let backend: Server
  let backendUrl: string
  const hits: Hit[] = []
  const pools: Database[] = []

  beforeEach(async () => {
    await resetTracing()
    hits.length = 0
    fake = await startFakeAnthropic((r) => script(r))
    ;({ db, schema, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    stop = new AbortController()
    backend = createServer((req, res) => {
      hits.push({ path: (req.url ?? '').split('?')[0] ?? '', traceparent: req.headers.traceparent as string | undefined })
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(req.url?.startsWith('/api/v1/models') ? '[]' : '{"status":"ok"}')
    })
    await new Promise<void>((resolve) => backend.listen(0, '127.0.0.1', resolve))
    backendUrl = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`
  })
  afterEach(async () => {
    stop.abort()
    await fake.close()
    await new Promise<void>((resolve) => backend.close(() => resolve()))
    for (const pool of pools.splice(0)) await pool.close()
    await drop()
  })

  async function agent(): Promise<SessionManager> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const pool = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
    pools.push(pool)
    const services = {
      backend: createBackendClient(backendUrl),
      pending: new PendingActionStore(),
      pollIntervalMs: 5,
      renderWaitMs: 1000,
    }
    return manager({
      sql: pool.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      approvalPollMs: 50,
      ...harnessTools(services, [LIST_MODELS, PING]),
    })
  }

  function routes(m: SessionManager): Hono {
    const app = new Hono()
    registerApprovalRoutes(app, {
      approvals: m.approvals,
      ready: () => Promise.resolve(true),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    return app
  }

  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')
  const named = (name: string) => spans.getFinishedSpans().filter((s) => s.name === name)

  async function approve(app: Hono, id: string): Promise<void> {
    const res = await app.request(`/api/v1/ai/approvals/${id}/approve`, { method: 'POST', headers: UI })
    expect(res.status).toBe(200)
  }

  /** The approval ids the session asked for, once it has asked for `count` (attach replays from the start). */
  async function required(m: SessionManager, sessionId: string, count: number): Promise<string[]> {
    let n = 0
    const all = await collectUntil(
      await m.attach(sessionId, browser, { signal: stop.signal }),
      (e) => e.event.type === 'approval.required' && ++n === count,
      30_000,
    )
    return all.flatMap((e) => (e.event.type === 'approval.required' ? [e.event.id] : []))
  }

  it('a turn is agent.turn → agent.tool, and its backend request carries the tool’s context; Anthropic gets none', async () => {
    script = (r) => (lastContent(r).includes('tool_result') ? { text: 'done' } : { toolUse: { name: 'mcp__scadbuddy__list_models', input: {} } })
    const m = await agent()
    const { turn } = await m.start(browser, { origin: 'chat', prompt: `list the models ${SENTINEL}` })
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })

    const seg0 = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    const [tool] = named('agent.tool/mcp__scadbuddy__list_models')
    expect(tool!.parentSpanContext?.spanId).toBe(seg0.spanContext().spanId)
    expect(tool!.attributes).toMatchObject({ 'scadbuddy.outcome': 'ok', 'scadbuddy.tier': 'read' })
    const [request] = named('GET /api/v1/models')
    expect(request!.parentSpanContext?.spanId).toBe(tool!.spanContext().spanId)
    expect(hits.find((h) => h.path === '/api/v1/models')?.traceparent).toBe(
      `00-${seg0.spanContext().traceId}-${request!.spanContext().spanId}-01`,
    )
    // Kept as a check, not as the guard: the CLI is a separate process with an
    // explicit env, so this cannot fail. The guard is that env's exact keys,
    // with no OTEL_* among them (test/run.test.ts, "puts the credential in env only").
    expect(fake.requests.every((r) => r.headers.traceparent === undefined)).toBe(true)
    expect(exportedText(spans)).not.toContain(SENTINEL)
  }, 60_000)

  it('a parked call ends its spans before any decision; the decision links to it; the resume is its child', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result') ? { text: 'done' } : { toolUse: { name: 'mcp__scadbuddy__ping_backend', input: { job: SENTINEL } } }
    const m = await agent()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ping it' })
    const [approvalId] = await required(m, session.id, 1)

    // Parked: both spans are exported already, and the row holds the tool span's context.
    const parkedTool = await waitForSpan(spans, (s) => s.name === 'agent.tool/mcp__scadbuddy__ping_backend')
    const seg0 = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    expect(parkedTool.attributes).toMatchObject({ 'scadbuddy.outcome': 'parked', 'scadbuddy.approval_id': approvalId })
    expect(seg0.attributes).toMatchObject({ 'scadbuddy.outcome': 'parked', 'scadbuddy.approval_id': approvalId })
    const row = await m.approvals.get(approvalId!, browser)
    expect(row.traceparent).toBe(`00-${parkedTool.spanContext().traceId}-${parkedTool.spanContext().spanId}-01`)
    expect(hits.filter((h) => h.path === '/healthz')).toEqual([])

    await approve(routes(m), approvalId!)
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })

    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.links.map((l) => l.context.spanId)).toEqual([parkedTool.spanContext().spanId])
    expect(decision.attributes['scadbuddy.outcome']).toBe('approved')
    const resume = await waitForSpan(spans, (s) => s.name === 'agent.turn.resume')
    expect(resume.parentSpanContext?.spanId).toBe(decision.spanContext().spanId)
    expect(resume.attributes).toMatchObject({ 'scadbuddy.segment': 1, 'scadbuddy.turn_id': seg0.attributes['scadbuddy.turn_id'] })
    const ran = named('agent.tool/mcp__scadbuddy__ping_backend').find((s) => s.attributes['scadbuddy.outcome'] === 'ok')!
    expect(ran.parentSpanContext?.spanId).toBe(resume.spanContext().spanId)
    const [ping] = named('GET /healthz')
    expect(ping!.parentSpanContext?.spanId).toBe(ran.spanContext().spanId)
    expect(hits.find((h) => h.path === '/healthz')?.traceparent).toBe(
      `00-${decision.spanContext().traceId}-${ping!.spanContext().spanId}-01`,
    )
    expect(exportedText(spans)).not.toContain(SENTINEL)
  }, 90_000)

  it('a turn that parks twice yields segments 0, 1 and 2, each under its own decision', async () => {
    script = (r) => {
      const last = lastContent(r)
      if (last.includes('pinged b')) return { text: 'done' }
      if (last.includes('pinged a')) return { toolUse: { name: 'mcp__scadbuddy__ping_backend', input: { job: 'b' } } }
      return { toolUse: { name: 'mcp__scadbuddy__ping_backend', input: { job: 'a' } } }
    }
    const m = await agent()
    const app = routes(m)
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ping twice' })
    const [first] = await required(m, session.id, 1)
    await approve(app, first!)
    const [, second] = await required(m, session.id, 2)
    await approve(app, second!)
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })

    const segments = [...named('agent.turn'), ...named('agent.turn.resume')].sort(
      (a, b) => Number(a.attributes['scadbuddy.segment']) - Number(b.attributes['scadbuddy.segment']),
    )
    expect(segments.map((s) => [s.name, s.attributes['scadbuddy.segment'], s.attributes['scadbuddy.outcome']])).toEqual([
      ['agent.turn', 0, 'parked'],
      ['agent.turn.resume', 1, 'parked'],
      ['agent.turn.resume', 2, 'success'],
    ])
    const decisions = named('agent.approval')
    const byApproval = (id: string) => decisions.find((d) => d.attributes['scadbuddy.approval_id'] === id)!
    expect(segments[1]!.parentSpanContext?.spanId).toBe(byApproval(first!).spanContext().spanId)
    expect(segments[2]!.parentSpanContext?.spanId).toBe(byApproval(second!).spanContext().spanId)
    expect(new Set(segments.map((s) => s.attributes['scadbuddy.turn_id'])).size).toBe(1)
  }, 120_000)
})
