import { spawn } from 'node:child_process'
import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { ApplicationFailure } from '@temporalio/common'
import { WorkflowNotFoundError } from '@temporalio/client'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { bundleWorkflowCode, Worker } from '@temporalio/worker'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { createApp } from '../src/app.js'
import { AuditLog } from '../src/audit/log.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { connectDatabase, type Database } from '../src/db.js'
import { PgEventListener } from '../src/events/pgListener.js'
import { DurableGate, sessionWorkflowId } from '../src/gate/durable.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ATTENTION_TOOL } from '../src/harness/questions.js'
import { runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { originPolicy } from '../src/http/origins.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { type SessionManager } from '../src/sessions/manager.js'
import type { InputEntry } from '../src/sessions/protocol.js'
import { RUNTIME_DEFAULTS } from '../src/sessions/runtimeSettings.js'
import { PostgresSessionStore } from '../src/sessions/store.js'
import { RESTARTED_PROMPT, TurnHost } from '../src/sessions/turnHost.js'
import { TASK_QUEUE } from '../src/temporal/names.js'
import { acquireActivities, releaseActivities } from '../src/temporal/session/affinity.js'
import { sessionFinishActivities } from '../src/temporal/session/finishActivity.js'
import { sessionGateActivities } from '../src/temporal/session/gateActivities.js'
import { type HostWorkers, startHostWorkers } from '../src/temporal/session/hostWorkers.js'
import { ACQUIRE_WORKER_ACTIVITY, SESSION_QUEUE, SESSION_SETTINGS_ACTIVITY, SHUTTING_DOWN_FAILURE, sessionHostQueue } from '../src/temporal/session/names.js'
import { sessionTurnActivity } from '../src/temporal/session/turnActivity.js'
import { decidedCalls, gateActivities, PgApprovalRecords, PgSessionOwners } from '../src/temporal/toolActivities.js'
import { AgentWorker, workerIdentity } from '../src/temporal/worker.js'
import { createHarnessServer } from '../src/tools/projections.js'
import { defineTool, idempotencyKey, text } from '../src/tools/registry.js'
import { services } from './helpers/mcp.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP, polledHosts } from './support/temporal.js'

// A park that outlasts its hold releases the process (unified runtime spec 2026-10-13 §5.4
// "Release" and "Continue after a release", plan Task 5.4), end to end: AgentSession on a
// Temporal dev server, one replica running the real TurnHost and the real Claude Code against
// the fake Anthropic endpoint, `park_hold_seconds` at 1. The outward tool is a ScadBuddy
// registry tool (`ship`) served by the harness server, as every ScadBuddy tool is, so the
// released call runs through the same layer (toolActivities.ts `decidedCalls`). Its command
// stands in for a backend one: `backend` records each send's Idempotency-Key and acts only on
// a key it has not seen, as the backend's operation record does.

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
const SHIP = 'mcp__scadbuddy__ship'
const sha = (s: string) => createHash('sha256').update(s).digest('hex')

/** The last message of a request: what the model is answering now. */
const lastMessage = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1) ?? null)

describe.skipIf(skip !== '')(`a long park releases the process${skip}`, { timeout: 180_000 }, () => {
  let env: TestWorkflowEnvironment
  let workflowWorker: Worker
  let workflowRun: Promise<void>
  let fake: FakeAnthropic
  let script: (r: RecordedRequest) => Reply | Promise<Reply>
  let db: Database
  let schema: string
  let url: string
  let drop: () => Promise<void>
  /** The first replica's manager: what starts each session, and what the panel's routes read. */
  let m: SessionManager
  let spawned: number
  /** Claude Code processes that have exited. */
  let exited: number
  /** Every send of `ship`'s command: its key and what. */
  let sends: { key: string; what: string }[]
  /** What the stand-in backend did: once per key. */
  let effects: string[]
  /** How many of the next released calls' placements die right after the call ran (a worker lost mid-continuation). */
  let diesAfterRun: number
  /** park_hold_seconds, as the workflow reads it for each turn. */
  let holdSeconds: number

  beforeAll(async () => {
    env = await localTemporal()
    workflowWorker = await Worker.create({
      connection: env.nativeConnection,
      taskQueue: SESSION_QUEUE,
      workflowBundle: await bundleWorkflowCode({ workflowsPath: WORKFLOWS }),
      // Below the routes' floor of 30 s: the workflow reads what the activity gives it.
      activities: { [SESSION_SETTINGS_ACTIVITY]: async () => ({ ...RUNTIME_DEFAULTS, park_hold_seconds: holdSeconds }) },
    })
    workflowRun = workflowWorker.run()
  }, 180_000)
  afterAll(async () => {
    workflowWorker?.shutdown()
    await workflowRun
    await env?.teardown()
  })

  const ship = defineTool({
    name: 'ship',
    label: 'ship',
    description: 'Ships something out of ScadBuddy',
    input: z.object({ what: z.string() }),
    risk: 'outward',
    routes: ['POST /api/v1/workflows/{definition_id}/runs'],
    handler: async ({ what }, ctx) => {
      const key = idempotencyKey(ctx)
      sends.push({ key, what })
      if (!sends.slice(0, -1).some((s) => s.key === key)) effects.push(what)
      return text(`shipped ${what}`)
    },
  })

  /** One replica of the agent: its manager, TurnHost, agent-tools worker and session host workers, on the shared database. */
  type Replica = {
    name: string
    hostQueue: string
    m: SessionManager
    turnHost: TurnHost
    tools: AgentWorker
    hosts: HostWorkers
    listener: PgEventListener
    pool: Database
    /** Its session_turn stops heartbeating (the replica alive, its worker cut off). */
    frozen: boolean
    stopped: boolean
  }
  let replicas: Replica[]
  /** The session host queues acquire_worker may refer a turn to: the replicas up now. */
  let live: Set<string>
  let tag: string
  /** The approvals' HMAC key, as every replica derives it from the one KEK (main.ts `approvalHashKey`). */
  let hashKey: Buffer

  async function replica(name: string): Promise<Replica> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const pool = connectDatabase(url, { searchPath: schema })
    const identity = `release-${name}-${tag}`
    const hostQueue = sessionHostQueue(identity)
    const audit = new AuditLog({ sql: pool.sql, hashKey })
    const principal = { id: browser.id, kind: 'browser' as const, tiers: tiersUpTo('outward') }
    const m = manager({
      sql: pool.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: 'gw-release-test-token' }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      mcpServers: (session) => ({ scadbuddy: createHarnessServer([ship], services(), principal, session.id) }),
      tierOf: (n) => (n === SHIP ? 'outward' : undefined),
      warm: { maxEntries: 8 },
      audit,
      run: (run) =>
        runHarness({
          ...run,
          spawnClaudeCodeProcess: (o) => {
            spawned++
            const child = spawn(o.command, o.args, { cwd: o.cwd, env: o.env, stdio: ['pipe', 'pipe', 'ignore'] })
            child.once('exit', () => exited++)
            return child
          },
        }),
    })
    m.durableTurns = new DurableTurns({ client: env.client, sql: pool.sql, events: m.events, liveHosts: polledHosts(env.client, hostQueue) })
    m.durable = new DurableGate(env.client, { timeoutMs: 30_000, sql: pool.sql })
    const listener = new PgEventListener(url, { searchPath: schema, retryMinMs: 20, log: () => {} })
    listener.start()
    const runCall = decidedCalls([ship], {
      services: services(),
      sessions: new PgSessionOwners(pool.sql),
      approvals: new PgApprovalRecords(pool.sql),
      audit,
    })
    const turnHost = new TurnHost({
      core: m.turnCore,
      sql: pool.sql,
      images: new SessionBlobs(pool.sql),
      hostQueue,
      listen: (channel, onNotify) => listener.listenAlso(channel, onNotify),
      runCall: async (call) => {
        const run = await runCall(call)
        if (diesAfterRun > 0) {
          diesAfterRun--
          // As a worker lost after the call ran and before its turn went on: the turn is placed again.
          throw ApplicationFailure.retryable('the agent service is shutting down', SHUTTING_DOWN_FAILURE)
        }
        return run
      },
    })
    const acquire = acquireActivities({ warm: m.warm, hostQueue, identity, liveHosts: async () => new Set(live) })
    const tools = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      taskQueue: TASK_QUEUE,
      identity: workerIdentity(identity),
      activities: {
        ...acquire,
        ...sessionFinishActivities(pool.sql, m.events),
        ...sessionGateActivities(pool.sql),
        ...gateActivities({ audit, sessions: new PgSessionOwners(pool.sql) }),
      },
      shutdownGraceMs: 1_000,
      log: () => {},
    })
    const r = { name, hostQueue, m, turnHost, tools, listener, pool, frozen: false, stopped: false } as Replica
    r.hosts = startHostWorkers({
      address: env.address,
      namespace: 'default',
      identity,
      pinned: { pinned_probe: async () => 'pinned' },
      sessions: {
        activities: {
          ...sessionTurnActivity(turnHost, {
            client: env.client,
            beat: (context, beat) => {
              if (!r.frozen) context.heartbeat(beat)
            },
          }),
          ...releaseActivities(m.warm),
          [ACQUIRE_WORKER_ACTIVITY]: acquire[ACQUIRE_WORKER_ACTIVITY],
        },
        slots: 10,
        shutdownGraceMs: 2_000,
        shutdownForceMs: 3_000,
      },
      shutdownGraceMs: 1_000,
    })
    await Promise.all([tools.running(), r.hosts.pinned.running(), r.hosts.sessions!.running()])
    await pollUntil(async () => (await polled(SESSION_QUEUE, 1)) && (await polled(TASK_QUEUE, 2)) && (await polled(hostQueue, 2)), true)
    live.add(hostQueue)
    replicas.push(r)
    return r
  }

  /** What SIGTERM does to a replica (main.ts): its turns get their grace, then every worker stops. */
  async function stopReplica(r: Replica): Promise<void> {
    if (r.stopped) return
    r.stopped = true
    live.delete(r.hostQueue)
    const hosted = r.turnHost.stop({ graceMs: 1_000, abortWaitMs: 1_000 })
    await Promise.all([r.tools.stop(), r.hosts.stop(), r.m.stopTurns({ graceMs: 1_000, abortWaitMs: 1_000, others: hosted })])
  }

  beforeEach(async () => {
    fake = await startFakeAnthropic((r) => script(r))
    ;({ db, schema, url, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    spawned = 0
    exited = 0
    sends = []
    effects = []
    diesAfterRun = 0
    holdSeconds = 1
    replicas = []
    live = new Set()
    tag = randomUUID().slice(0, 8)
    hashKey = randomBytes(32)
    script = () => ({ text: 'ok' })
    m = (await replica('a')).m
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
    for (const r of replicas) await stopReplica(r)
    for (const r of replicas) {
      await r.listener.close()
      await r.pool.close()
    }
    await fake.close()
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
  async function listed(): Promise<InputEntry[]> {
    const res = await app().request('/api/v1/ai/pending-input', { headers: READ })
    expect(res.status).toBe(200)
    return ((await res.json()) as { entries: InputEntry[] }).entries
  }
  async function respond(id: string, body: unknown): Promise<{ status: number; body: unknown }> {
    const res = await app().request(`/api/v1/ai/pending-input/${id}`, { method: 'POST', headers: UI, body: JSON.stringify(body) })
    return { status: res.status, body: await res.json() }
  }

  /** The model ships the box, then answers whatever the outcome message says with `reply`. */
  const shipsTheBox = (reply: string) => (r: RecordedRequest) =>
    lastMessage(r).includes('was paused waiting for approval') ? { text: reply } : lastMessage(r).includes('tool_result') ? { text: 'waiting' } : { toolUse: { name: SHIP, input: { what: 'box' } } }

  /** The parked call's results in the session's transcript (ai_session_entries). */
  async function resultsOf(id: string, toolUseId: string) {
    const store = new PostgresSessionStore(db.sql, { keys: testPayloadKeys(db.sql) })
    return ((await store.load({ projectKey: '', sessionId: id })) ?? []).flatMap((e) => {
      const content = (e as { message?: { content?: unknown } }).message?.content
      return Array.isArray(content) ? content.filter((b: { type?: string; tool_use_id?: string }) => b.type === 'tool_result' && b.tool_use_id === toolUseId) : []
    })
  }

  /** A session whose one call parked, and whose process has since been released and has ended: the entry, still open. */
  async function released(prompt = 'ship the box') {
    const started = await m.start(browser, { origin: 'chat', prompt })
    const id = started.session.id
    await pollUntil(async () => (await listed()).length, 1)
    const [entry] = await listed()
    // Released past the hold: no process (exited, its closing result in the transcript), nothing
    // run, and the entry still waits in the workflow.
    await pollUntil(() => m.warm.size(), 0)
    await pollUntil(() => exited, 1)
    await pollUntil(async () => (await resultsOf(id, entry!.id.split(':').at(-1)!)).length, 1)
    expect(spawned).toBe(1)
    expect(effects).toEqual([])
    expect((await listed()).map((e) => e.id)).toEqual([entry!.id])
    return { started, id, entry: entry! }
  }

  it('a released approval resumes, and its call runs once, keyed by its request id, and the model reads how it went', async () => {
    script = shipsTheBox('shipped, thanks')
    const { started, entry } = await released()
    const asked = fake.messageCalls().length
    expect(await respond(entry.id, { kind: 'approval', decision: 'approve' })).toEqual({ status: 200, body: expect.objectContaining({ outcome: 'approved' }) })
    expect(await started.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(effects).toEqual(['box'])
    expect(sends).toEqual([{ key: sha(entry.id), what: 'box' }])
    // A new process resumed the turn, and its first request carries the outcome.
    expect(spawned).toBe(2)
    expect(fake.messageCalls().slice(asked).map(lastMessage), 'the requests after the release').toHaveLength(1)
    const told = lastMessage(fake.messageCalls().at(-1)!)
    expect(told).toContain(`Your call \`${SHIP}\` (id ${entry.id.split(':').at(-1)}) was paused waiting for approval. It was approved and ScadBuddy ran it once.`)
    expect(told).toContain('shipped box')
    // The released call's result interrupted the turn: no request answered it.
    expect(fake.messageCalls().map(lastMessage).filter((last) => last.includes('the turn paused, and you will be told how it ended'))).toEqual([])
    expect(await listed()).toEqual([])
  })

  it("the closed process wrote the parked call's synthetic result into the transcript (spec §12)", async () => {
    script = shipsTheBox('ok')
    const { started, id, entry } = await released()
    expect(await resultsOf(id, entry.id.split(':').at(-1)!)).toEqual([expect.objectContaining({ is_error: true })])
    await respond(entry.id, { kind: 'approval', decision: 'deny' })
    await started.turn!.done
  })

  it('a denied released call is reported in the gate`s words, and not run', async () => {
    script = shipsTheBox('understood')
    const { started, entry } = await released()
    expect((await respond(entry.id, { kind: 'approval', decision: 'deny' })).status).toBe(200)
    expect(await started.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(sends).toEqual([])
    expect(lastMessage(fake.messageCalls().at(-1)!)).toContain(`The user denied ${SHIP} (approval ${entry.id}); it was not run.`)
  })

  it('a released question resumes with its answer, which the model reads in the outcome message', async () => {
    const colour = {
      question: 'Which colour?',
      header: 'Colour',
      multiSelect: false,
      options: [
        { label: 'Red', description: 'warm' },
        { label: 'Blue', description: 'cool' },
      ],
    }
    script = (r) =>
      lastMessage(r).includes("was paused waiting for the user's answer")
        ? { text: 'blue it is' }
        : lastMessage(r).includes('tool_result')
          ? { text: 'waiting' }
          : { toolUse: { name: 'AskUserQuestion', input: { questions: [colour] } } }
    const { started, entry } = await released('paint it')
    expect(entry).toMatchObject({ kind: 'answer', tool: 'ask_user' })
    expect((await respond(entry!.id, { kind: 'answer', answers: { 'Which colour?': 'Blue' } })).status).toBe(200)
    expect(await started.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(spawned).toBe(2)
    const told = lastMessage(fake.messageCalls().at(-1)!)
    expect(told).toContain(`Your question (id ${entry!.id.split(':').at(-1)}) was paused waiting for the user's answer. The user answered:`)
    expect(told).toContain('Which colour?: Blue')
  })

  it('an approved released call runs once across a placement lost after it ran: the second send is answered from the record', async () => {
    script = shipsTheBox('shipped once')
    diesAfterRun = 1
    const { started, entry } = await released()
    expect((await respond(entry.id, { kind: 'approval', decision: 'approve' })).status).toBe(200)
    expect(await started.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    // Sent twice (the lost placement's, then the next one's), with one key, and done once.
    expect(sends).toEqual([
      { key: sha(entry.id), what: 'box' },
      { key: sha(entry.id), what: 'box' },
    ])
    expect(effects).toEqual(['box'])
    expect(lastMessage(fake.messageCalls().at(-1)!)).toContain('It was approved and ScadBuddy ran it once.')
  })

  it('a shutdown while a call is held releases the turn: once approved, the call runs once on the replica still up', async () => {
    // A hold the test never outlasts: only the shutdown ends it.
    holdSeconds = 600
    script = shipsTheBox('shipped from b')
    const [a] = replicas
    const started = await m.start(browser, { origin: 'chat', prompt: 'ship the box' })
    await pollUntil(async () => (await listed()).length, 1)
    const [entry] = await listed()
    const b = await replica('b')
    await stopReplica(a!)
    // Released by the shutdown, not cut: its process ended, the entry still waits.
    await pollUntil(() => exited, 1)
    expect((await listed()).map((e) => e.id)).toEqual([entry!.id])
    expect(effects).toEqual([])
    expect((await respond(entry!.id, { kind: 'approval', decision: 'approve' })).status).toBe(200)
    expect(await started.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(effects).toEqual(['box'])
    expect(sends).toEqual([{ key: sha(entry!.id), what: 'box' }])
    expect(b.m.warm.size()).toBe(1)
    expect(lastMessage(fake.messageCalls().at(-1)!)).toContain('It was approved and ScadBuddy ran it once.')
    // A's process was gone before it could read its held request as failed and ask the model again:
    // no request answers that closing result (it is only replayed, by B's).
    expect(fake.messageCalls().map(lastMessage).filter((last) => last.includes('Tool permission stream closed'))).toEqual([])
  })

  it('a continuation lost after its outcome message landed resumes with the restart message, and its call is not run again', async () => {
    const [a] = replicas
    let first = true
    let hung: () => void = () => {}
    const hang = new Promise<void>((resolve) => {
      hung = resolve
    })
    script = async (r) => {
      const last = lastMessage(r)
      if (last.includes(RESTARTED_PROMPT)) return { text: 'carried on' }
      if (last.includes('was paused waiting for approval') && first) {
        // A's model call for the outcome: A stalls here, its message in the transcript.
        first = false
        a!.frozen = true
        await hang
        return { text: 'too late' }
      }
      return shipsTheBox('unreached')(r)
    }
    const { started, entry } = await released()
    const b = await replica('b')
    expect((await respond(entry.id, { kind: 'approval', decision: 'approve' })).status).toBe(200)
    expect(await started.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    hung()
    // A ran the call once; B found the outcome message in the transcript and ran nothing.
    expect(sends).toEqual([{ key: sha(entry.id), what: 'box' }])
    expect(effects).toEqual(['box'])
    const resumed = fake.messageCalls().at(-1)!
    expect(lastMessage(resumed)).toContain(RESTARTED_PROMPT)
    expect(JSON.stringify(resumed.body?.messages ?? [])).toContain('It was approved and ScadBuddy ran it once.')
    expect(b.m.warm.size()).toBe(1)
  })

  it('a Stop while the turn runs ends it within 2 s, on its replica`s next read of the row', async () => {
    let answer: () => void = () => {}
    script = () =>
      new Promise<Reply>((resolve) => {
        answer = () => resolve({ text: 'too late' })
      })
    const started = await m.start(browser, { origin: 'chat', prompt: 'think hard' })
    await pollUntil(() => fake.messageCalls().length, 1)
    const asked = Date.now()
    expect(await m.interrupt(started.session.id, browser)).toBe(true)
    expect(await started.turn!.done).toMatchObject({ kind: 'interrupted' })
    expect(Date.now() - asked).toBeLessThan(2_000)
    answer()
    // The process stays: a Stop ends the turn, not the process.
    expect(m.warm.size()).toBe(1)
  })

  it('a Stop while a call is held ends the turn and keeps the process: the entry is cancelled, and the model is not asked again', async () => {
    holdSeconds = 600
    script = shipsTheBox('unreached')
    const started = await m.start(browser, { origin: 'chat', prompt: 'ship the box' })
    await pollUntil(async () => (await listed()).length, 1)
    const [entry] = await listed()
    const asked = fake.messageCalls().length
    expect(await m.interrupt(started.session.id, browser)).toBe(true)
    expect(await started.turn!.done).toMatchObject({ kind: 'interrupted' })
    expect(await listed()).toEqual([])
    const [response] = await db.sql<{ outcome: string }[]>`SELECT outcome FROM ai_input_responses WHERE request_id = ${entry!.id}`
    expect(response?.outcome).toBe('cancelled')
    expect(effects).toEqual([])
    expect(fake.messageCalls()).toHaveLength(asked)
    expect(m.warm.size()).toBe(1)
    expect(spawned).toBe(1)
  })

  // #5003: an attention request is an MCP tool's call, whose released result cannot interrupt the
  // turn as canUseTool's refusal does: its release stops the turn, and the continuation says how
  // it ended in the words the tool would have returned.
  const asksForAttention = (input: Record<string, unknown>, reply: string) => (r: RecordedRequest) =>
    lastMessage(r).includes("paused waiting for their reply") ? { text: reply } : lastMessage(r).includes('tool_result') ? { text: 'waiting' } : { toolUse: { name: ATTENTION_TOOL, input } }

  it('a released attention request resumes with the reply, in the words request_user_attention returns', async () => {
    script = asksForAttention({ reason: 'blocked', message: 'Shall I go on?', options: ['Go', 'Stop'] }, 'going')
    const { started, entry } = await released('work')
    expect(entry).toMatchObject({ kind: 'answer', tool: 'wait_for_user', attention: { reason: 'blocked', on_timeout: 'proceed' } })
    const asked = fake.messageCalls().length
    expect((await respond(entry.id, { kind: 'answer', choice: 'Go' })).status).toBe(200)
    expect(await started.turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(spawned).toBe(2)
    expect(fake.messageCalls().slice(asked)).toHaveLength(1)
    const told = lastMessage(fake.messageCalls().at(-1)!)
    expect(told).toContain(`Your request for the user's attention (id ${entry.id.split(':').at(-1)}) was paused waiting for their reply. answered: the user replied \\"Go\\"`)
    // The release stopped the turn: no request answered the released call's result.
    expect(fake.messageCalls().map(lastMessage).filter((last) => last.includes('the turn paused, and you will be told how it ended'))).toEqual([])
  })

  it("a released attention request whose `stop` timer fires ends the turn, and the model is not asked again", async () => {
    script = asksForAttention({ reason: 'blocked', message: 'Need you', timeout_s: 10, on_timeout: 'stop' }, 'too late')
    const { started, id } = await released('work')
    const asked = fake.messageCalls().length
    expect(await started.turn!.done).toMatchObject({ kind: 'interrupted' })
    expect(fake.messageCalls().length).toBe(asked)
    expect(spawned).toBe(1)
    const errors = (await db.sql<{ event: string }[]>`SELECT event FROM ai_session_events WHERE session_id = ${id} ORDER BY seq`)
      .map((r) => JSON.parse(r.event) as { type: string; message?: string })
      .filter((e) => e.type === 'error')
    expect(errors.at(-1)?.message).toBe('the turn ended: nobody replied to the attention request in 10 s (on_timeout: stop)')
  })
})
