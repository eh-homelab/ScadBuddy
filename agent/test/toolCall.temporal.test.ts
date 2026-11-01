import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { Context } from '@temporalio/activity'
import { bundleWorkflowCode, Worker } from '@temporalio/worker'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import type { Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { respond } from '../src/routes/pendingInput.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { event } from '../src/sessions/protocol.js'
import { RESTARTED_PROMPT, restartedPrompt } from '../src/sessions/turnHost.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { finishedToolCalls, toolCallStarter } from '../src/tools/activityProjection.js'
import { harnessTools } from '../src/tools/harness.js'
import { defineTool, json, type Tool, type ToolContext } from '../src/tools/registry.js'
import { PgApprovalRecords, PgSessionOwners, type ToolActivityDeps, toolCallActivities } from '../src/temporal/toolActivities.js'
import { toolCallWorkflowId } from '../src/temporal/toolCall.js'
import { services } from './helpers/mcp.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, stopManagers, tempPaths } from './support/sessions.js'
import { AGENT_SESSIONS_SKIP, agentSessions, agentSessionsEnv } from './support/agentSessions.js'
import { AuditLog } from '../src/audit/log.js'

// A turn's ScadBuddy tool calls as activities (unified runtime plan 2026-10-13 Step 9.2, decision
// Q1): with `tools_as_activities` on, each call of an AgentSession turn (the real SDK and its
// bundled Claude Code, against the fake Anthropic endpoint) is a ToolCall workflow of its own,
// `session-<uuid>-call-<tool_use_id>`, whose one activity `tool:<name>` runs on a worker that is
// not the turn's: here a worker of this file's own, on a queue of its own.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))

/** Each tool run: where it ran, and the key it was given. */
type Ran = { tool: string; input: unknown; taskQueue: string; workflowId: string; callKey: string | undefined }

describe.skipIf(skip !== undefined || AGENT_SESSIONS_SKIP !== '')(`a turn's tool calls as activities${skip ? ` (skipped: ${skip})` : ''}${AGENT_SESSIONS_SKIP}`, () => {
  agentSessions()
  const queue = `tool-calls-${randomUUID().slice(0, 8)}`
  let worker: Worker
  let running: Promise<void>
  let fake: FakeAnthropic
  let script: (r: RecordedRequest) => Reply
  let db: Database
  let drop: () => Promise<void>
  let ran: Ran[]
  /** What the file's worker runs with: the running test's database. */
  let deps: ToolActivityDeps | undefined

  const record = (tool: string, input: unknown, ctx: ToolContext) => {
    const info = Context.current().info
    ran.push({ tool, input, taskQueue: info.taskQueue, workflowId: info.workflowExecution?.workflowId ?? '', callKey: ctx.callKey })
  }
  const probe = defineTool({
    name: 'probe',
    label: 'probe',
    description: 'Reads a number back',
    input: z.object({ n: z.number() }),
    risk: 'read',
    routes: [],
    handler: async (args, ctx) => {
      record('probe', args, ctx)
      return json({ probed: args.n })
    },
  })
  const send = defineTool({
    name: 'send',
    label: 'send',
    description: 'Sends something out of ScadBuddy',
    // A default the model leaves out: the approval binds the input as parked, before any parse fills it.
    input: z.object({ what: z.string(), copies: z.number().default(1) }),
    risk: 'outward',
    routes: [],
    handler: async (args, ctx) => {
      record('send', args, ctx)
      return json({ sent: args.what })
    },
  })
  /** Runs until it is cancelled. */
  const wait = defineTool({
    name: 'wait',
    label: 'wait',
    description: 'Waits',
    input: z.object({}),
    risk: 'read',
    routes: [],
    handler: async (args, ctx) => {
      record('wait', args, ctx)
      await new Promise((resolve) => ctx.signal.addEventListener('abort', resolve, { once: true }))
      return json({ waited: true })
    },
  })
  const TOOLS: readonly Tool[] = [probe, send, wait]

  beforeAll(async () => {
    const env = agentSessionsEnv()
    const activities = toolCallActivities(TOOLS, {
      services: services(),
      sessions: { ownerOf: (id) => deps!.sessions.ownerOf(id) },
      approvals: { approved: (id, call) => deps!.approvals!.approved(id, call) },
      audit: { hash: (n, i) => deps!.audit!.hash(n, i), summarise: (n, i) => deps!.audit!.summarise(n, i), record: async () => {} },
    })
    worker = await Worker.create({
      connection: env.nativeConnection,
      taskQueue: queue,
      identity: `tool-worker-${queue}`,
      workflowBundle: await bundleWorkflowCode({ workflowsPath: WORKFLOWS }),
      activities,
    })
    running = worker.run()
  }, 180_000)
  afterAll(async () => {
    worker?.shutdown()
    await running
  })
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    fake = await startFakeAnthropic((r) => script(r))
    ran = []
    deps = {
      services: services(),
      sessions: new PgSessionOwners(db.sql),
      approvals: new PgApprovalRecords(db.sql),
      audit: new AuditLog({ sql: db.sql, hashKey: Buffer.alloc(32, 5) }),
    }
  })
  afterEach(async () => {
    await stopManagers()
    await fake.close()
    await drop()
  })

  async function agent(): Promise<SessionManager> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const start = toolCallStarter({ client: agentSessionsEnv().client, blobs: new SessionBlobs(db.sql), queue })
    return manager({
      sql: db.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: 'gw-tool-calls-token-0123456789abcdef' }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : key === 'tools_as_activities' ? true : undefined) as T) },
      ...harnessTools(services(), TOOLS),
      toolCalls: () => start,
      approvalPollMs: 50,
      approvalHashKey: Buffer.alloc(32, 5),
    })
  }

  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')

  it("runs a read call once, as its own ToolCall's activity, on a worker other than the turn's", async () => {
    script = (r) => (lastContent(r).includes('tool_result') ? { text: 'Probed.' } : { toolUse: { name: 'mcp__scadbuddy__probe', input: { n: 7 } } })
    const m = await agent()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'probe it' })
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(ran).toHaveLength(1)
    const [call] = ran
    expect(call).toMatchObject({ tool: 'probe', input: { n: 7 }, taskQueue: queue })
    expect(call!.workflowId).toMatch(new RegExp(`^session-${session.id}-call-toolu_`))
    const toolUseId = call!.workflowId.slice(`session-${session.id}-call-`.length)
    expect(call!.callKey).toBe(`call:${session.id}:${toolUseId}`)
    const described = await agentSessionsEnv().client.workflow.getHandle(toolCallWorkflowId(session.id, toolUseId)).describe()
    expect(described).toMatchObject({ type: 'ToolCall', taskQueue: queue, status: { name: 'COMPLETED' } })
    // The model read the activity's result.
    const told = fake.messageCalls().map(lastContent).find((c) => c.includes('tool_result'))
    expect(told).toContain('probed')
    expect(told).toContain('7')
  }, 120_000)

  it('runs 12 parallel calls of one assistant message, each once', async () => {
    const calls = Array.from({ length: 12 }, (_, n) => ({ name: 'mcp__scadbuddy__probe', input: { n } }))
    script = (r) => (lastContent(r).includes('tool_result') ? { text: 'All probed.' } : { toolUses: calls })
    const m = await agent()
    const { turn } = await m.start(browser, { origin: 'chat', prompt: 'probe them all' })
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(ran.map((r) => (r.input as { n: number }).n).sort((a, b) => a - b)).toEqual(calls.map((c) => c.input.n))
    expect(new Set(ran.map((r) => r.workflowId)).size).toBe(12)
    expect(ran.every((r) => r.taskQueue === queue)).toBe(true)
  }, 120_000)

  it("parks an outward call, then runs it approved, keyed by the request id it was approved on", async () => {
    script = (r) => (lastContent(r).includes('tool_result') ? { text: 'Sent.' } : { toolUse: { name: 'mcp__scadbuddy__send', input: { what: 'the box' } } })
    const m = await agent()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'send the box' })
    let entry: { request_id: string } | undefined
    await expect
      .poll(
        async () => {
          ;[entry] = await db.sql<{ request_id: string }[]>`
            SELECT request_id FROM ai_pending_input WHERE session_id = ${session.id} AND kind = 'approval'`
          return entry?.request_id
        },
        { timeout: 60_000 },
      )
      .toEqual(expect.any(String))
    expect(ran).toEqual([])
    expect(await respond(m, browser, entry!.request_id, { kind: 'approval', decision: 'approve' })).toMatchObject({ outcome: 'approved' })
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(ran).toHaveLength(1)
    expect(ran[0]).toMatchObject({ tool: 'send', input: { what: 'the box', copies: 1 }, taskQueue: queue, callKey: entry!.request_id })
    expect(fake.messageCalls().map(lastContent).find((c) => c.includes('tool_result'))).toContain('sent')
  }, 120_000)

  it('refuses an outward call whose ToolCall names no approval, and the model reads why', async () => {
    const m = await agent()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    script = () => ({ text: 'Hello.' })
    await turn!.done
    const start = toolCallStarter({ client: agentSessionsEnv().client, blobs: new SessionBlobs(db.sql), queue })
    const result = await start({ sessionId: session.id, toolUseId: 'toolu_01Unapproved_x', tool: 'send', input: { what: 'x' }, signal: new AbortController().signal })
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result.content)).toContain('needs an approval')
    expect(ran).toEqual([])
  }, 120_000)

  it("gives a restarted turn the results of its calls whose ToolCall completed, and runs none again", async () => {
    script = () => ({ text: 'Hello.' })
    const m = await agent()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    await turn!.done
    const client = agentSessionsEnv().client
    // A turn whose placement was lost mid-call: two calls in flight, one of which finished as its ToolCall.
    const turnId = randomUUID()
    await m.events.append(session.id, [
      event({ type: 'user.turn', sessionId: session.id, turnId, text: 'probe twice' } as never),
      event({ type: 'tool.call', sessionId: session.id, id: 'toolu_01Done_a', name: 'mcp__scadbuddy__probe', input: { n: 41 }, risk: 'read' }),
      event({ type: 'tool.call', sessionId: session.id, id: 'toolu_01Lost_b', name: 'mcp__scadbuddy__probe', input: { n: 42 }, risk: 'read' }),
    ])
    const start = toolCallStarter({ client, blobs: new SessionBlobs(db.sql), queue })
    await start({ sessionId: session.id, toolUseId: 'toolu_01Done_a', tool: 'probe', input: { n: 41 }, signal: new AbortController().signal })
    expect(ran).toHaveLength(1)
    const finished = await finishedToolCalls({ sql: db.sql, client }, session.id, turnId)
    expect(finished).toEqual([{ id: 'toolu_01Done_a', name: 'mcp__scadbuddy__probe', ok: true, text: expect.stringContaining('41') }])
    const prompt = restartedPrompt(finished)
    expect(prompt).not.toBe(RESTARTED_PROMPT)
    expect(prompt).toContain('toolu_01Done_a')
    expect(prompt).toContain('41')
    expect(prompt).not.toContain('toolu_01Lost_b')
    // Reading it again runs nothing: the call ran once.
    await finishedToolCalls({ sql: db.sql, client }, session.id, turnId)
    expect(ran).toHaveLength(1)
    expect(restartedPrompt([])).toBe(RESTARTED_PROMPT)
  }, 120_000)

  it("forgetting the session terminates its calls still open", async () => {
    script = () => ({ text: 'Hello.' })
    const m = await agent()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    await turn!.done
    const client = agentSessionsEnv().client
    const toolUseId = 'toolu_01Open_c'
    await m.events.append(session.id, [
      event({ type: 'tool.call', sessionId: session.id, id: toolUseId, name: 'mcp__scadbuddy__wait', input: {}, risk: 'read' }),
    ])
    const start = toolCallStarter({ client, blobs: new SessionBlobs(db.sql), queue })
    const answer = start({ sessionId: session.id, toolUseId, tool: 'wait', input: {}, signal: new AbortController().signal }).catch((err: unknown) => err)
    await expect.poll(() => ran.length, { timeout: 30_000 }).toBe(1)
    await forgetSubject({ sql: db.sql, client }, `session-${session.id}`)
    const described = await client.workflow.getHandle(toolCallWorkflowId(session.id, toolUseId)).describe()
    expect(described.status.name).toBe('TERMINATED')
    await answer
  }, 120_000)
})
