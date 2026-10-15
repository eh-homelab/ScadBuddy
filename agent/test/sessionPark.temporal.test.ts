import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk'
import { WorkflowNotFoundError } from '@temporalio/client'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { bundleWorkflowCode, Worker } from '@temporalio/worker'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { createApp } from '../src/app.js'
import { AuditLog } from '../src/audit/log.js'
import { connectDatabase, type Database } from '../src/db.js'
import { PgEventListener } from '../src/events/pgListener.js'
import { DurableGate, sessionWorkflowId } from '../src/gate/durable.js'
import { PENDING_INPUT_QUERY } from '../src/gate/names.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { originPolicy } from '../src/http/origins.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { type SessionManager, SETTING_SESSION_MODE } from '../src/sessions/manager.js'
import type { InputEntry } from '../src/sessions/protocol.js'
import { RUNTIME_DEFAULTS } from '../src/sessions/runtimeSettings.js'
import { TurnHost } from '../src/sessions/turnHost.js'
import { TASK_QUEUE } from '../src/temporal/names.js'
import { acquireActivities, releaseActivities } from '../src/temporal/session/affinity.js'
import { sessionFinishActivities } from '../src/temporal/session/finishActivity.js'
import { sessionGateActivities } from '../src/temporal/session/gateActivities.js'
import { type HostWorkers, startHostWorkers } from '../src/temporal/session/hostWorkers.js'
import { ACQUIRE_WORKER_ACTIVITY, SESSION_QUEUE, SESSION_SETTINGS_ACTIVITY, sessionHostQueue, UNIFIED_SESSIONS_SETTING } from '../src/temporal/session/names.js'
import { sessionTurnActivity } from '../src/temporal/session/turnActivity.js'
import { gateActivities, PgSessionOwners } from '../src/temporal/toolActivities.js'
import { AgentWorker, workerIdentity } from '../src/temporal/worker.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// Parks through the session's workflow, held warm (unified runtime spec 2026-10-13 §5.4 "Open"
// and "Hold", plan Task 5.3), end to end: AgentSession on a Temporal dev server, one replica
// (its agent-tools worker with the gate's activities, its session host worker running the real
// TurnHost), the real Claude Code against the fake Anthropic endpoint, and the panel's routes
// (GET /api/v1/ai/pending-input, POST /api/v1/ai/pending-input/{id}). A parked call opens its
// entry with the Update `open_input` and holds on its row; the response reaches it by NOTIFY
// (the replica's listener) or the 1 s poll. Every wait is on a condition (`pollUntil`), never
// on the host's wall clock, which steps (the plan's notes).

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = !TEMPORAL_CLI
  ? TEMPORAL_SKIP
  : cliMissing
    ? ` (skipped: ${cliMissing})`
    : TEST_DATABASE_URL
      ? ''
      : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }
const READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const SEND = 'mcp__stub__send'
const COLOUR = {
  question: 'Which colour?',
  header: 'Colour',
  multiSelect: false,
  options: [
    { label: 'Red', description: 'warm' },
    { label: 'Blue', description: 'cool' },
  ],
}

/** The last message of a request: what the model is answering now. */
const lastMessage = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1) ?? null)
const conversation = (r: RecordedRequest) => JSON.stringify(r.body?.messages ?? [])

describe.skipIf(skip !== '')(`a session turn parks through its workflow${skip}`, { timeout: 180_000 }, () => {
  let env: TestWorkflowEnvironment
  let workflowWorker: Worker
  let workflowRun: Promise<void>
  let fake: FakeAnthropic
  let script: (r: RecordedRequest) => Reply
  let db: Database
  let schema: string
  let url: string
  let drop: () => Promise<void>
  let m: SessionManager
  let pool: Database
  let listener: PgEventListener
  let tools: AgentWorker
  let hosts: HostWorkers
  let turnHost: TurnHost
  let spawned: number
  /** The stub outward tool's calls, by `what`. */
  let sent: string[]

  beforeAll(async () => {
    env = await localTemporal()
    workflowWorker = await Worker.create({
      connection: env.nativeConnection,
      taskQueue: SESSION_QUEUE,
      workflowBundle: await bundleWorkflowCode({ workflowsPath: WORKFLOWS }),
      activities: { [SESSION_SETTINGS_ACTIVITY]: async () => RUNTIME_DEFAULTS },
    })
    workflowRun = workflowWorker.run()
  }, 180_000)
  afterAll(async () => {
    workflowWorker?.shutdown()
    await workflowRun
    await env?.teardown()
  })

  const send = tool('send', 'Sends something out of ScadBuddy', { what: z.string() }, async ({ what }) => {
    sent.push(what)
    return { content: [{ type: 'text', text: `sent ${what}` }] }
  })

  beforeEach(async () => {
    fake = await startFakeAnthropic((r) => script(r))
    ;({ db, schema, url, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    // Set in the database, as no route writes it (unifiedSessionsFlag.pg.test.ts).
    await db.sql`INSERT INTO ai_settings (key, value) VALUES (${UNIFIED_SESSIONS_SETTING}, ${db.sql.json(true)})`
    spawned = 0
    sent = []
    script = () => ({ text: 'ok' })
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    pool = connectDatabase(url, { searchPath: schema })
    const tag = randomUUID().slice(0, 8)
    const identity = `park-${tag}`
    const hostQueue = sessionHostQueue(identity)
    const audit = new AuditLog({ sql: pool.sql })
    m = manager({
      sql: pool.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: 'gw-park-test-token' }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : key === SETTING_SESSION_MODE ? 'durable' : undefined) as T) },
      mcpServers: () => ({ stub: createSdkMcpServer({ name: 'stub', tools: [send] }) }),
      tierOf: (n) => (n === SEND ? 'outward' : undefined),
      warm: { maxEntries: 8 },
      audit,
      run: (run) =>
        runHarness({
          ...run,
          spawnClaudeCodeProcess: (o) => {
            spawned++
            return spawn(o.command, o.args, { cwd: o.cwd, env: o.env, stdio: ['pipe', 'pipe', 'ignore'] })
          },
        }),
    })
    m.durableTurns = new DurableTurns({ client: env.client, sql: pool.sql, events: m.events, sessionHostQueue: hostQueue })
    // The respond route's way to a durable entry: the workflow's `respond` Update (main.ts). Its
    // deadline is wall-clock (`withDeadline(Date.now() + …)`): 30 s, not 5, so a step of this host's
    // clock (about 8 s) or a loaded worker cannot turn a respond the workflow takes into a 503.
    m.durable = new DurableGate(env.client, { timeoutMs: 30_000 })
    // The replica's LISTEN connection, as main.ts's event bus: a held call wakes on `scadbuddy_input`.
    listener = new PgEventListener(url, { searchPath: schema, retryMinMs: 20, log: () => {} })
    listener.start()
    turnHost = new TurnHost({
      core: m.turnCore,
      sql: pool.sql,
      images: new SessionBlobs(pool.sql),
      hostQueue,
      listen: (channel, onNotify) => listener.listenAlso(channel, onNotify),
    })
    const acquire = acquireActivities({ warm: m.warm, hostQueue, identity, liveHosts: async () => new Set([hostQueue]) })
    tools = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      taskQueue: TASK_QUEUE,
      identity: workerIdentity(identity),
      activities: {
        ...acquire,
        ...sessionFinishActivities(pool.sql, m.events),
        // As main.ts: the gate's timers and writers, and describe_call (the summary and the hash).
        ...sessionGateActivities(pool.sql),
        ...gateActivities({ audit, sessions: new PgSessionOwners(pool.sql) }),
      },
      shutdownGraceMs: 1_000,
      log: () => {},
    })
    hosts = startHostWorkers({
      address: env.address,
      namespace: 'default',
      identity,
      pinned: { pinned_probe: async () => 'pinned' },
      sessions: {
        activities: { ...sessionTurnActivity(turnHost, { client: env.client }), ...releaseActivities(m.warm), [ACQUIRE_WORKER_ACTIVITY]: acquire[ACQUIRE_WORKER_ACTIVITY] },
        slots: 10,
        shutdownGraceMs: 2_000,
        shutdownForceMs: 3_000,
      },
      shutdownGraceMs: 1_000,
    })
    await Promise.all([tools.running(), hosts.pinned.running(), hosts.sessions!.running()])
    // Started sessions check these pollers (DurableTurns.unready): wait until Temporal lists them.
    await pollUntil(async () => (await polled(SESSION_QUEUE, 1)) && (await polled(TASK_QUEUE, 2)) && (await polled(hostQueue, 2)), true)
  }, 90_000)
  afterEach(async () => {
    for (const { id } of await db.sql<{ id: string }[]>`SELECT id FROM ai_sessions`) {
      await env.client.workflow
        .getHandle(sessionWorkflowId(id))
        .terminate('the test ended')
        .catch((err: unknown) => {
          if (!(err instanceof WorkflowNotFoundError)) throw err
        })
    }
    const hosted = turnHost.stop({ graceMs: 1_000, abortWaitMs: 1_000 })
    await Promise.all([tools.stop(), hosts.stop(), m.stopTurns({ graceMs: 1_000, abortWaitMs: 1_000, others: hosted })])
    await listener.close()
    await fake.close()
    await pool.close()
    await drop()
  }, 60_000)

  async function polled(queue: string, type: 1 | 2): Promise<boolean> {
    const { pollers } = await env.client.workflowService.describeTaskQueue({ namespace: 'default', taskQueue: { name: queue }, taskQueueType: type })
    return (pollers ?? []).length > 0
  }
  const pollUntil = <T>(read: () => Promise<T> | T, expected: T, timeout = 60_000) => expect.poll(read, { timeout, interval: 100 }).toEqual(expected)

  function app() {
    return createApp({
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      approvals: m.approvals,
      sessions: m,
    })
  }
  /** GET /api/v1/ai/pending-input: what the badge lists. */
  async function listed(): Promise<InputEntry[]> {
    const res = await app().request('/api/v1/ai/pending-input', { headers: READ })
    expect(res.status).toBe(200)
    return ((await res.json()) as { entries: InputEntry[] }).entries
  }
  /** POST /api/v1/ai/pending-input/{id}: the panel's respond. */
  async function respond(id: string, body: unknown): Promise<{ status: number; body: unknown }> {
    const res = await app().request(`/api/v1/ai/pending-input/${id}`, { method: 'POST', headers: UI, body: JSON.stringify(body) })
    return { status: res.status, body: await res.json() }
  }
  /** The session's entries as its workflow holds them (the `pending_input` Query): the workflow's own clock for both times. */
  const held = (id: string) => env.client.workflow.getHandle(sessionWorkflowId(id)).query<InputEntry[]>(PENDING_INPUT_QUERY)
  const runningRun = async (id: string) => (await env.client.workflow.getHandle(sessionWorkflowId(id)).describe()).runId
  /** The workflow has closed the turn (its `turn` query is empty again), so the next send is taken. */
  const settled = (id: string) => pollUntil(() => env.client.workflow.getHandle(sessionWorkflowId(id)).query<string | null>('turn'), null)
  const toolUseIds = (r: RecordedRequest) => [...conversation(r).matchAll(/"tool_use","id":"(toolu_[0-9]+)"/g)].map((x) => x[1])

  it('an outward call parks as `durable:<s>:<run>:<toolu>`, is listed, and once approved runs in the held turn on its warm process', async () => {
    script = (r) => (lastMessage(r).includes('tool_result') ? { text: 'sent it' } : { toolUse: { name: SEND, input: { what: 'box' } } })
    const started = await m.start(browser, { origin: 'chat', prompt: 'send the box' })
    const id = started.session.id
    const run = await runningRun(id)
    await pollUntil(async () => (await listed()).length, 1)
    const [entry] = await listed()
    expect(entry).toMatchObject({ kind: 'approval', session_id: id, tool: SEND, requested_by: browser })
    expect(entry!.id).toMatch(new RegExp(`^durable:${id}:${run}:toolu_\\w+$`))
    const [row] = await db.sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${id}`
    expect(row?.status).toBe('waiting_approval')
    // Held, not run: nothing sent, and the model has not been asked again.
    expect(sent).toEqual([])
    const asked = fake.messageCalls().length
    expect(await respond(entry!.id, { kind: 'approval', decision: 'approve' })).toEqual({ status: 200, body: expect.objectContaining({ outcome: 'approved' }) })
    expect(await started.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(sent).toEqual(['box'])
    expect(fake.messageCalls().length).toBe(asked + 1)
    expect(lastMessage(fake.messageCalls().at(-1)!)).toContain('sent box')
    // The entry names the call the model made: its tool_use id.
    expect(toolUseIds(fake.messageCalls().at(-1)!)).toEqual([entry!.id.split(':').at(-1)])
    // The process was never closed: one Claude Code, still warm, and the turn was placed once.
    expect(spawned).toBe(1)
    expect(m.warm.size()).toBe(1)
    expect(await listed()).toEqual([])
    const [response] = await db.sql<{ outcome: string }[]>`SELECT outcome FROM ai_input_responses WHERE request_id = ${entry!.id}`
    expect(response?.outcome).toBe('approved')
  })

  it('a denied call is not run, and the model reads why in the gate`s words', async () => {
    script = (r) => (lastMessage(r).includes('tool_result') ? { text: 'understood' } : { toolUse: { name: SEND, input: { what: 'box' } } })
    const started = await m.start(browser, { origin: 'chat', prompt: 'send the box' })
    await pollUntil(async () => (await listed()).length, 1)
    const [entry] = await listed()
    expect((await respond(entry!.id, { kind: 'approval', decision: 'deny' })).status).toBe(200)
    expect(await started.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(sent).toEqual([])
    expect(lastMessage(fake.messageCalls().at(-1)!)).toContain(`The user denied ${SEND} (approval ${entry!.id}); it was not run.`)
    expect(spawned).toBe(1)
  })

  it('AskUserQuestion parks as `answer`, and the answer reaches the model as the tool`s result', async () => {
    script = (r) => (lastMessage(r).includes('tool_result') ? { text: 'blue it is' } : { toolUse: { name: 'AskUserQuestion', input: { questions: [COLOUR] } } })
    const started = await m.start(browser, { origin: 'chat', prompt: 'paint it' })
    const id = started.session.id
    const run = await runningRun(id)
    await pollUntil(async () => (await listed()).length, 1)
    const [entry] = await listed()
    expect(entry).toMatchObject({ kind: 'answer', tool: 'ask_user', prompt: 'Which colour?' })
    expect(entry!.id).toMatch(new RegExp(`^durable:${id}:${run}:toolu_\\w+$`))
    const [row] = await db.sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${id}`
    expect(row?.status).toBe('waiting_input')
    expect((await respond(entry!.id, { kind: 'answer', answers: { 'Which colour?': 'Blue' } })).status).toBe(200)
    expect(await started.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    const result = lastMessage(fake.messageCalls().at(-1)!)
    expect(result).toContain('tool_result')
    expect(result).toContain('Which colour?')
    expect(result).toContain('Blue')
    expect(toolUseIds(fake.messageCalls().at(-1)!)).toEqual([entry!.id.split(':').at(-1)])
    expect(spawned).toBe(1)
  })

  it('two outward calls in one assistant message each park on an entry of their own, and each runs once when approved', async () => {
    script = (r) =>
      lastMessage(r).includes('tool_result')
        ? { text: 'both sent' }
        : { toolUses: [{ name: SEND, input: { what: 'lid' } }, { name: SEND, input: { what: 'base' } }] }
    const started = await m.start(browser, { origin: 'chat', prompt: 'send both parts' })
    // Claude Code (2.1.296, measured here) asks canUseTool for the second call only once the first
    // has run, so the two park one after the other: each is listed, and runs once, as it is approved.
    const approved: string[] = []
    for (const what of ['lid', 'base']) {
      await pollUntil(async () => (await listed()).filter((e) => !approved.includes(e.id)).length, 1)
      const [entry] = (await listed()).filter((e) => !approved.includes(e.id))
      expect(sent).toEqual(approved.length ? ['lid'] : [])
      expect((await respond(entry!.id, { kind: 'approval', decision: 'approve' })).status).toBe(200)
      approved.push(entry!.id)
      await pollUntil(() => sent.includes(what), true)
    }
    expect(await started.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(sent).toEqual(['lid', 'base'])
    // Both calls of the one assistant message, each its own request id in the same run.
    const calls = toolUseIds(fake.messageCalls().at(-1)!)
    expect(calls).toHaveLength(2)
    expect(approved.map((rid) => rid.split(':').at(-1)).sort()).toEqual([...calls].sort())
    expect(new Set(approved.map((rid) => rid.split(':').slice(0, 3).join(':'))).size).toBe(1)
    // The model's next request answers both in one message: each call's result.
    expect((lastMessage(fake.messageCalls().at(-1)!).match(/"tool_result"/g) ?? []).length).toBe(2)
    expect(spawned).toBe(1)
  })

  it('reads the approval timeout from Settings as each turn starts: a change between turns reaches the next park', async () => {
    script = (r) => (lastMessage(r).includes('tool_result') ? { text: 'done' } : { toolUse: { name: SEND, input: { what: conversation(r).includes('second') ? 'two' : 'one' } } })
    await db.sql`INSERT INTO ai_settings (key, value) VALUES ('approval_expiry_seconds', ${db.sql.json(45)})`
    const first = await m.start(browser, { origin: 'chat', prompt: 'first' })
    const id = first.session.id
    await pollUntil(async () => (await held(id)).length, 1)
    const [one] = await held(id)
    expect(Date.parse(one!.expires_at!) - Date.parse(one!.created_at)).toBe(45_000)
    await respond(one!.id, { kind: 'approval', decision: 'approve' })
    expect(await first.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    await settled(id)
    await db.sql`UPDATE ai_settings SET value = ${db.sql.json(30)} WHERE key = 'approval_expiry_seconds'`
    const second = await m.send(id, browser, 'second')
    await pollUntil(async () => (await held(id)).length, 1)
    const [two] = await held(id)
    expect(two!.id).not.toBe(one!.id)
    expect(Date.parse(two!.expires_at!) - Date.parse(two!.created_at)).toBe(30_000)
    await respond(two!.id, { kind: 'approval', decision: 'approve' })
    expect(await second.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(sent).toEqual(['one', 'two'])
  })
})
