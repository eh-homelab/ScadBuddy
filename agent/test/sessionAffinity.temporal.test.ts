import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk'
import type { Context } from '@temporalio/activity'
import { defaultPayloadConverter } from '@temporalio/common'
import { historyToJSON } from '@temporalio/common/lib/proto-utils.js'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { bundleWorkflowCode, Worker } from '@temporalio/worker'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { AuditLog } from '../src/audit/log.js'
import { connectDatabase, type Database } from '../src/db.js'
import { sessionWorkflowId } from '../src/gate/durable.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { type SessionManager, type SessionManagerDeps, SETTING_SESSION_MAX_TURNS, SETTING_SESSION_MODE } from '../src/sessions/manager.js'
import { RUNTIME_DEFAULTS } from '../src/sessions/runtimeSettings.js'
import { RESTARTED_PROMPT, TurnHost } from '../src/sessions/turnHost.js'
import { TASK_QUEUE } from '../src/temporal/names.js'
import { acquireActivities, releaseActivities } from '../src/temporal/session/affinity.js'
import { sessionFinishActivities } from '../src/temporal/session/finishActivity.js'
import { type HostWorkers, startHostWorkers } from '../src/temporal/session/hostWorkers.js'
import {
  ACQUIRE_WORKER_ACTIVITY,
  SESSION_QUEUE,
  SESSION_RUNTIME_SETTING,
  SESSION_SETTINGS_ACTIVITY,
  sessionHostQueue,
} from '../src/temporal/session/names.js'
import { sessionTurnActivity } from '../src/temporal/session/turnActivity.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, REPLY_COST_USD, startFakeAnthropic } from './support/fakeAnthropic.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// Session affinity (unified runtime spec 2026-10-13 §5.2, plan Task 5.2) end to end: AgentSession
// on a Temporal dev server, two replicas (A, B), each with its own manager, warm cache, shared
// `agent-tools` worker (acquire_worker, session.finish_turn) and host workers
// (`agent-sessions.<name>` for session_turn and release_worker, `agent-tools.<name>` for pinned
// calls), and the real Claude Code against the fake Anthropic endpoint. Which replica answers
// acquire_worker is decided by which replicas' agent-tools workers run.
//
// Timing: everything waits on a condition (a latch, the event log, the history), except where a
// Temporal timeout is what is tested: the 5 s schedule-to-start of a dead home, and the 20 s
// heartbeat of a replica that stopped answering.

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
const PINNED_WORKFLOWS = fileURLToPath(new URL('./support/pinnedWorkflows.ts', import.meta.url))
const PINNED_TEST_QUEUE = 'pinned-test'

function latch(): { promise: Promise<void>; release: () => void } {
  let release!: () => void
  const promise = new Promise<void>((resolve) => {
    release = resolve
  })
  return { promise, release }
}

const conversation = (r: RecordedRequest) => JSON.stringify(r.body?.messages ?? [])

type Replica = {
  name: string
  m: SessionManager
  /** acquire_worker answers, and NoCapacity refusals, on this replica. */
  acquired: number
  refused: number
  /** Claude Code processes spawned. */
  spawned: number
  /** While set, its session_turn sends no heartbeat: a replica that stopped answering. */
  frozen: boolean
  /** Its agent-tools worker: the one acquire_worker answers on. */
  tools: AgentWorker
  hosts: HostWorkers
  turnHost: TurnHost
  /** Its turns' shutdown grace (TurnHost.stop), and the session host worker's past it. */
  drainMs: number
}

describe.skipIf(skip !== '')(`session affinity${skip}`, { timeout: 180_000 }, () => {
  let env: TestWorkflowEnvironment
  let workflowWorkers: Worker[] = []
  let workflowRuns: Promise<void>[] = []
  let fake: FakeAnthropic
  let script: (r: RecordedRequest) => Reply
  let db: Database
  let schema: string
  let url: string
  let drop: () => Promise<void>
  const replicas: Replica[] = []
  const pools: Database[] = []
  /** The stub tool's calls, and what a call waits on before it answers. */
  let slowCalls: number
  /** The stub tool's calls that have answered. */
  let slowReturned: number
  let slowGate: Promise<void>
  /** What `session_settings` answers each turn (the runtime settings, §5.2). */
  let runtimeSettings: typeof RUNTIME_DEFAULTS

  beforeAll(async () => {
    env = await localTemporal()
    const make = (taskQueue: string, options: Pick<Parameters<typeof Worker.create>[0], 'workflowBundle' | 'workflowsPath' | 'activities'>) =>
      Worker.create({ connection: env.nativeConnection, taskQueue, ...options })
    workflowWorkers = [
      await make(SESSION_QUEUE, {
        workflowBundle: await bundleWorkflowCode({ workflowsPath: WORKFLOWS }),
        activities: { [SESSION_SETTINGS_ACTIVITY]: async () => runtimeSettings },
      }),
      await make(PINNED_TEST_QUEUE, { workflowsPath: PINNED_WORKFLOWS }),
    ]
    workflowRuns = workflowWorkers.map((w) => w.run())
  }, 180_000)
  afterAll(async () => {
    workflowWorkers.forEach((w) => w.shutdown())
    await Promise.all(workflowRuns)
    await env?.teardown()
  })
  beforeEach(async () => {
    fake = await startFakeAnthropic((r) => script(r))
    ;({ db, schema, url, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    // Set in the database, as no route writes it (sessionRuntimeFlag.pg.test.ts).
    await db.sql`INSERT INTO ai_settings (key, value) VALUES (${SESSION_RUNTIME_SETTING}, ${db.sql.json('unified')})`
    slowCalls = 0
    slowReturned = 0
    slowGate = Promise.resolve()
    runtimeSettings = RUNTIME_DEFAULTS
    script = () => ({ text: 'ok' })
  })
  afterEach(async () => {
    for (const r of replicas.splice(0)) await stopReplica(r)
    await fake.close()
    for (const pool of pools.splice(0)) await pool.close()
    await drop()
  })

  /** Whether a worker polls `queue` (1 workflow, 2 activity) now. */
  async function polled(queue: string, type: 1 | 2): Promise<boolean> {
    const { pollers } = await env.client.workflowService.describeTaskQueue({ namespace: 'default', taskQueue: { name: queue }, taskQueueType: type })
    return (pollers ?? []).length > 0
  }
  const pollUntil = <T>(read: () => Promise<T> | T, expected: T, timeout = 30_000) => expect.poll(read, { timeout, interval: 100 }).toEqual(expected)

  /** The stub tool: counts its calls and answers once `slowGate` lets it. */
  const slow = tool('slow', 'Does something slowly', { q: z.string() }, async () => {
    slowCalls++
    await slowGate
    slowReturned++
    return { content: [{ type: 'text', text: 'slow done' }] }
  })

  async function replica(
    name: string,
    options: {
      slots?: number
      maxEntries?: number
      extra?: Partial<SessionManagerDeps>
      settings?: Record<string, unknown>
      /** Its turns' shutdown grace (stopReplica); 1 s by default. */
      drainMs?: number
      /** session_turn's heartbeat period (turnActivity.ts HEARTBEAT_MS by default). */
      heartbeatMs?: number
      /** Claude Code processes that start and never answer: a placement that dies before the prompt lands. */
      dead?: boolean
      /** session_turn never hears its cancellation (its worker cut off from Temporal): only the row can tell it. */
      deaf?: boolean
    } = {},
  ): Promise<Replica> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const pool = connectDatabase(url, { searchPath: schema })
    pools.push(pool)
    const hostQueue = sessionHostQueue(name)
    const r = { name, acquired: 0, refused: 0, spawned: 0, frozen: false, drainMs: options.drainMs ?? 1_000 } as Replica
    r.m = manager({
      sql: pool.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: 'gw-affinity-test-token' }),
      settings: {
        get: <T>(key: string) =>
          Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : key === SETTING_SESSION_MODE ? 'durable' : options.settings?.[key]) as T),
      },
      mcpServers: () => ({ stub: createSdkMcpServer({ name: 'stub', tools: [slow] }) }),
      tierOf: (n) => (n === 'mcp__stub__slow' ? 'read' : undefined),
      warm: { maxEntries: options.maxEntries ?? 8 },
      // Each turn's cost as its own audit row (#1922): what each placement that ended wrote.
      audit: new AuditLog({ sql: pool.sql }),
      run: (run) =>
        runHarness({
          ...run,
          spawnClaudeCodeProcess: (o) => {
            r.spawned++
            if (options.dead) return spawn('sleep', ['3600'], { stdio: ['pipe', 'pipe', 'ignore'] })
            return spawn(o.command, o.args, { cwd: o.cwd, env: o.env, stdio: ['pipe', 'pipe', 'ignore'] })
          },
        }),
      ...options.extra,
    })
    r.m.durableTurns = new DurableTurns({ client: env.client, sql: pool.sql, events: r.m.events, sessionHostQueue: hostQueue })
    r.turnHost = new TurnHost({ core: r.m.turnCore, sql: pool.sql, images: new SessionBlobs(pool.sql), hostQueue })
    const acquire = acquireActivities({ warm: r.m.warm, hostQueue, identity: name })[ACQUIRE_WORKER_ACTIVITY]
    r.tools = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      taskQueue: TASK_QUEUE,
      activities: {
        [ACQUIRE_WORKER_ACTIVITY]: async (input: { sessionId: string }) => {
          try {
            const answer = await acquire(input)
            r.acquired++
            return answer
          } catch (err) {
            r.refused++
            throw err
          }
        },
        ...sessionFinishActivities(pool.sql, r.m.events),
      },
      shutdownGraceMs: 1_000,
      log: () => {},
    })
    r.hosts = startHostWorkers({
      address: env.address,
      namespace: 'default',
      identity: name,
      pinned: { pinned_probe: async () => `pinned on ${name}` },
      sessions: {
        activities: {
          ...sessionTurnActivity(r.turnHost, {
            beat: (context: Context, beat) => {
              if (!r.frozen) context.heartbeat(beat)
            },
            ...(options.heartbeatMs ? { heartbeatMs: options.heartbeatMs } : {}),
            ...(options.deaf ? { cancelled: () => new AbortController().signal } : {}),
          }),
          ...releaseActivities(r.m.warm),
        },
        slots: options.slots ?? 10,
        // As main.ts: past the turns' own drain and abort wait.
        shutdownGraceMs: r.drainMs + 1_000,
        shutdownForceMs: r.drainMs + 2_000,
      },
      shutdownGraceMs: 1_000,
    })
    await Promise.all([r.tools.running(), r.hosts.pinned.running(), r.hosts.sessions!.running()])
    // Started sessions check these pollers (DurableTurns.unready): wait until Temporal lists them.
    await pollUntil(async () => (await polled(SESSION_QUEUE, 1)) && (await polled(TASK_QUEUE, 2)) && (await polled(hostQueue, 2)), true)
    replicas.push(r)
    return r
  }

  /**
   * What SIGTERM does to a replica (main.ts): its AgentSession turns and classic turns get their
   * grace, every worker stops, and the warm processes are closed once the turns have ended.
   */
  async function stopReplica(r: Replica): Promise<void> {
    const limits = { graceMs: r.drainMs, abortWaitMs: 1_000 }
    const hosted = r.turnHost.stop(limits)
    await Promise.all([r.tools.stop(), r.hosts.stop(), r.m.stopTurns({ ...limits, others: hosted })])
  }

  /** The session's spend and turns as the row records them. */
  async function recorded(id: string) {
    const [row] = await db.sql<{ cost_usd: number; unpriced_cost_usd: number; turns: number }[]>`
      SELECT cost_usd, unpriced_cost_usd, turns FROM ai_sessions WHERE id = ${id}`
    return { costUsd: Number(row!.cost_usd), unpricedUsd: Number(row!.unpriced_cost_usd), turns: Number(row!.turns) }
  }
  /** The audit rows of the session's turn costs (#1922), what each placement that ended wrote. */
  async function turnCosts(id: string) {
    return (
      await db.sql<{ cost_usd: number }[]>`
        SELECT cost_usd FROM ai_audit WHERE session_id = ${id} AND kind = 'turn' ORDER BY id`
    ).map((r) => Number(r.cost_usd))
  }
  /** What the session's pending session_turn last heartbeated, as the server holds it. */
  async function beatOnServer(id: string): Promise<unknown> {
    const d = await env.client.workflow.getHandle(sessionWorkflowId(id)).describe()
    const payload = d.raw.pendingActivities?.[0]?.heartbeatDetails?.payloads?.[0]
    return payload ? defaultPayloadConverter.fromPayload(payload) : undefined
  }
  /** Whether any of the session's events says `text`. */
  const said = async (r: Replica, id: string, text: string) => JSON.stringify(await r.m.events.read(id, 0, 10_000)).includes(text)
  const userTurns = (r: RecordedRequest, text: string) => conversation(r).split(text).length - 1

  /** Starts a unified session with its first turn, as the panel does. */
  async function startOn(r: Replica, prompt: string) {
    const started = await r.m.start(browser, { origin: 'chat', prompt })
    expect(started.modeFallback).toBeUndefined()
    return { id: started.session.id, turn: started.turn! }
  }

  const types = async (r: Replica, id: string) => (await r.m.events.read(id, 0, 10_000)).map((e) => e.event.type)
  /**
   * The run that is running the session's turn now: it continues as new only after the turn has
   * ended, so read right after the send it is the run whose history holds the turn.
   */
  const runningRun = async (id: string) => (await env.client.workflow.getHandle(sessionWorkflowId(id)).describe()).runId
  /**
   * The workflow has closed the turn (its `turn` query is empty again): `turn.done` follows the
   * event log, which `session.finish_turn` writes a moment before the workflow takes the next
   * message, and a send in between is refused `busy`.
   */
  const settled = (id: string) =>
    pollUntil(() => env.client.workflow.getHandle(sessionWorkflowId(id)).query<string | null>('turn'), null)
  /** One run's history: the turn's run, never whichever run continued as new after it. */
  const historyTypes = async (id: string, runId: string) =>
    (JSON.parse(historyToJSON(await env.client.workflow.getHandle(sessionWorkflowId(id), runId).fetchHistory())) as { events: { eventType: string; activityTaskTimedOutEventAttributes?: { failure?: { timeoutFailureInfo?: { timeoutType?: string } } } }[] }).events

  it('the first turn acquires and later turns stay on home, on the warm process', async () => {
    const a = await replica('a')
    const { id, turn } = await startOn(a, 'first')
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    await settled(id)
    const second = await a.m.send(id, browser, 'second')
    expect(await second.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(a.acquired).toBe(1)
    // One Claude Code for both turns: the second was a warm hit.
    expect(a.spawned).toBe(1)
    expect(a.m.warm.size()).toBe(1)
    expect(conversation(fake.messageCalls().at(-1)!)).toContain('first')
  })

  /** The session's event types and its final status, for a classic turn and a unified one of the same script. */
  async function bothPaths(a: Replica, prompt: string) {
    const classic = await a.m.start(browser, { origin: 'chat', prompt, mode: 'classic' })
    const classicOutcome = await classic.turn!.done
    const { id, turn } = await startOn(a, prompt)
    const unifiedOutcome = await turn.done
    const status = async (sid: string) => (await db.sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${sid}`)[0]?.status
    const errors = async (r: Replica, sid: string) =>
      (await r.m.events.read(sid, 0, 10_000)).flatMap((e) => (e.event.type === 'error' ? [e.event.code] : []))
    return {
      classic: { types: await types(a, classic.session.id), status: await status(classic.session.id), errors: await errors(a, classic.session.id), outcome: classicOutcome },
      unified: { types: await types(a, id), status: await status(id), errors: await errors(a, id), outcome: unifiedOutcome },
      id,
    }
  }

  it('events match a classic turn of the same script', async () => {
    const a = await replica('a')
    const { classic, unified, id } = await bothPaths(a, 'make a box')
    expect(classic.outcome).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(unified.outcome).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(unified.types).toEqual(classic.types)
    expect(unified.types).toEqual([
      'session.started',
      'session.status',
      'user.turn',
      'session.status',
      'assistant.text.delta',
      'assistant.text.done',
      'session.result',
      'session.status',
    ])
    // The cost and turns are the row's, as a classic turn writes them.
    const [row] = await db.sql<{ turns: number; cost_usd: number; status: string; durable_turn_id: string | null }[]>`
      SELECT turns, cost_usd, status, durable_turn_id FROM ai_sessions WHERE id = ${id}`
    expect(row).toMatchObject({ turns: 1, status: 'idle', durable_turn_id: null })
    expect(row!.cost_usd).toBeGreaterThan(0)
  })

  it('events match a classic turn that runs out of turns: error_max_turns, and the session stays idle', async () => {
    // Every reply asks for the tool again: one turn of the session's one is spent, and Claude Code stops.
    script = () => ({ toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    const a = await replica('a', { settings: { [SETTING_SESSION_MAX_TURNS]: 1 } })
    const { classic, unified } = await bothPaths(a, 'loop')
    expect(classic.outcome).toMatchObject({ kind: 'result', subtype: 'error_max_turns' })
    expect(unified.outcome).toMatchObject({ kind: 'result', subtype: 'error_max_turns' })
    expect(unified.types).toEqual(classic.types)
    expect(unified.errors).toEqual(classic.errors)
    expect(classic.errors).toEqual(['error_max_turns'])
    expect(unified.status).toBe(classic.status)
    expect(classic.status).toBe('idle')
  })

  it('events match a classic turn the API refuses: api_error, and the session fails', async () => {
    script = () => ({ error: { status: 400, type: 'invalid_request_error', message: 'messages: text content blocks must be non-empty' } })
    const a = await replica('a')
    const { classic, unified } = await bothPaths(a, 'refuse me')
    expect(classic.outcome).toMatchObject({ kind: 'failed' })
    expect(unified.outcome).toMatchObject({ kind: 'failed' })
    expect(unified.types).toEqual(classic.types)
    expect(unified.errors).toEqual(classic.errors)
    expect(classic.errors).toEqual(['api_error'])
    expect(unified.status).toBe(classic.status)
    expect(classic.status).toBe('failed')
  })

  it('a turn past turn_max_seconds is placed once, never moved, and ends idle as classic`s error_max_turns does', async () => {
    // A model that never answers holds the turn however long its start takes.
    script = () => ({ hang: true })
    // The wall clock is what is tested: 5 s, below the route's minimum of 300, set here only.
    runtimeSettings = { ...RUNTIME_DEFAULTS, turn_max_seconds: 5 }
    const a = await replica('a')
    const { id, turn } = await startOn(a, 'take too long')
    const run = await runningRun(id)
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    const history = await historyTypes(id, run)
    const timeouts = history.filter((e) => e.eventType === 'EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT')
    expect(timeouts.map((e) => e.activityTaskTimedOutEventAttributes?.failure?.timeoutFailureInfo?.timeoutType)).toEqual(['TIMEOUT_TYPE_START_TO_CLOSE'])
    // Placed once: one acquire, one session_turn, one tool call.
    expect(a.acquired).toBe(1)
    const scheduled = history.filter(
      (e) => e.eventType === 'EVENT_TYPE_ACTIVITY_TASK_SCHEDULED' && (e as { activityTaskScheduledEventAttributes?: { activityType?: { name?: string } } }).activityTaskScheduledEventAttributes?.activityType?.name === 'session_turn',
    )
    expect(scheduled).toHaveLength(1)
    // Its message reached the model at most once: never sent again on another placement.
    expect(fake.messageCalls().length).toBeLessThanOrEqual(1)
    const tail = (await types(a, id)).slice(-3)
    const errors = (await a.m.events.read(id, 0, 10_000)).flatMap((e) => (e.event.type === 'error' ? [e.event.code] : []))
    expect(errors).toEqual(['turn_max_seconds'])
    const [row] = await db.sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${id}`
    // As classic's turn that ran out (error_max_turns): the result, the error, and idle.
    script = () => ({ toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    slowGate = Promise.resolve()
    const c = await replica('c', { settings: { [SETTING_SESSION_MAX_TURNS]: 1 } })
    const classic = await c.m.start(browser, { origin: 'chat', prompt: 'loop', mode: 'classic' })
    await classic.turn!.done
    expect(tail).toEqual((await types(c, classic.session.id)).slice(-3))
    expect(tail).toEqual(['session.result', 'error', 'session.status'])
    const [classicRow] = await db.sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${classic.session.id}`
    expect(row?.status).toBe(classicRow?.status)
    expect(row?.status).toBe('idle')
  })

  it('release_worker frees the slot on end', async () => {
    const a = await replica('a')
    const { id, turn } = await startOn(a, 'hello')
    await turn.done
    expect(a.m.warm.size()).toBe(1)
    const handle = env.client.workflow.getHandle(sessionWorkflowId(id))
    await handle.signal('end')
    await handle.result()
    expect(a.m.warm.size()).toBe(0)
  })

  it('a dead home is re-acquired after schedule-to-start: a stopped replica`s session finishes its next turn on another', async () => {
    const a = await replica('a')
    const { id, turn } = await startOn(a, 'first')
    await turn.done
    expect(a.m.warm.size()).toBe(1)
    // SIGTERM on A: its workers stop, and its warm process goes with it (stopTurns closes the
    // cache), so nothing of the session is left on A to release.
    await stopReplica(a)
    replicas.splice(replicas.indexOf(a), 1)
    expect(a.m.warm.size()).toBe(0)
    const b = await replica('b')
    await settled(id)
    const began = Date.now()
    const second = await b.m.send(id, browser, 'second')
    const run = await runningRun(id)
    expect(await second.done).toMatchObject({ kind: 'result', subtype: 'success' })
    // The first try waited its 5 s on A's queue, then acquire ran again, on B.
    expect(Date.now() - began).toBeGreaterThanOrEqual(5_000)
    const timeouts = (await historyTypes(id, run)).filter((e) => e.eventType === 'EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT')
    expect(timeouts.map((e) => e.activityTaskTimedOutEventAttributes?.failure?.timeoutFailureInfo?.timeoutType)).toEqual(['TIMEOUT_TYPE_SCHEDULE_TO_START'])
    expect(a.acquired + b.acquired).toBe(2)
    expect(b.acquired).toBe(1)
    expect(b.m.warm.size()).toBe(1)
    // The message was never pushed on A, so B sends it, not the restart notice.
    const last = conversation(fake.messageCalls().at(-1)!)
    expect(last).toContain('second')
    expect(last).not.toContain(RESTARTED_PROMPT)
  })

  it('a placement lost before its prompt landed: the next sends the message itself, once, whatever the beat said (I1)', async () => {
    const a = await replica('a', { dead: true })
    const { id, turn } = await startOn(a, 'the only copy')
    // A pushed the message into a process that never answers, and its beat says so.
    await pollUntil(async () => ((await beatOnServer(id)) as { pushed?: boolean } | undefined)?.pushed === true, true)
    a.frozen = true
    await a.tools.stop()
    const b = await replica('b')
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    // Nothing of the turn was in the transcript, so B sent the message, not the restart notice.
    const calls = fake.messageCalls()
    expect(calls).toHaveLength(1)
    expect(conversation(calls[0]!)).not.toContain(RESTARTED_PROMPT)
    expect(userTurns(calls[0]!, 'the only copy')).toBe(1)
    expect(b.acquired).toBe(1)
    // A was cut off once B held the turn: its process is gone and it wrote nothing.
    await pollUntil(() => a.turnHost.active, 0)
    expect(a.m.warm.size()).toBe(0)
    // One request, billed once: the row and B's turn.
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(REPLY_COST_USD, 12)
    expect(row.turns).toBe(1)
    expect(await turnCosts(id)).toEqual([expect.closeTo(REPLY_COST_USD, 12)])
  })

  it('a placement lost after its prompt landed: the next resumes with the restart message, never the message again (I1)', async () => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) => (conversation(r).includes('tool_use') ? { text: 'carried on' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    const a = await replica('a')
    // A never heartbeats at all: the workflow has no word that the message went in.
    a.frozen = true
    const { id, turn } = await startOn(a, 'do the slow thing')
    const run = await runningRun(id)
    await pollUntil(() => slowCalls, 1)
    await a.tools.stop()
    const b = await replica('b')
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    const timeouts = (await historyTypes(id, run)).filter((e) => e.eventType === 'EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT')
    expect(timeouts.map((e) => e.activityTaskTimedOutEventAttributes?.failure?.timeoutFailureInfo?.timeoutType)).toEqual(['TIMEOUT_TYPE_HEARTBEAT'])
    expect(b.acquired).toBe(1)
    // B read the transcript: the message was in it, so it resumed with the restart message.
    const resumed = fake.messageCalls().at(-1)!
    expect(conversation(resumed)).toContain(RESTARTED_PROMPT)
    expect(userTurns(resumed, 'do the slow thing')).toBe(1)
    expect(slowCalls).toBe(1)
    await pollUntil(() => a.turnHost.active, 0)
    // A's request (counted as it went, at the table's prices) and B's, each once.
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(2 * REPLY_COST_USD, 12)
    expect(row.unpricedUsd).toBeCloseTo(REPLY_COST_USD, 12)
    expect(row.turns).toBe(1)
    expect(await turnCosts(id)).toEqual([expect.closeTo(REPLY_COST_USD, 12)])
    hold.release()
  })

  it.each([
    ['its worker tells it the turn timed out', false],
    ['only the row tells it (its worker never hears the cancel)', true],
  ])('a placement alive but stalled is cut off once the turn moves (%s): it writes nothing more, its spend counts once, and the turn avoids it (I3)', async (_how, deaf) => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) => (conversation(r).includes('tool_use') ? { text: 'carried on' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    const a = await replica('a', { deaf })
    const { id, turn } = await startOn(a, 'do the slow thing')
    await pollUntil(() => slowCalls, 1)
    // A's heartbeat stalls; A itself (its process, its agent-tools worker) stays up.
    a.frozen = true
    // The heartbeat times out and acquire runs again: A refuses the turn it lost.
    await pollUntil(() => a.refused >= 1, true, 60_000)
    expect(a.acquired).toBe(1)
    const b = await replica('b')
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(b.acquired).toBe(1)
    // A saw B's placement on the row and cut itself off: its process is gone.
    await pollUntil(() => a.turnHost.active, 0)
    expect(a.m.warm.size()).toBe(0)
    const calls = fake.messageCalls().length
    // The call A held answers now, into nothing: no event, no request, no spend from A.
    hold.release()
    await pollUntil(() => slowReturned, 1)
    expect(await said(a, id, 'slow done')).toBe(false)
    expect(fake.messageCalls()).toHaveLength(calls)
    expect(calls).toBe(2)
    expect(slowCalls).toBe(1)
    // A's request and B's, each once: on the row, and in each placement's own cost row (A's only
    // when it ended still holding the row: told by its worker, before B took it).
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(2 * REPLY_COST_USD, 12)
    expect(row.turns).toBe(1)
    const costs = await turnCosts(id)
    expect(costs).toEqual(deaf ? [expect.closeTo(REPLY_COST_USD, 12)] : [expect.closeTo(REPLY_COST_USD, 12), expect.closeTo(REPLY_COST_USD, 12)])
  })

  it('warm_max_per_replica 0: a turn is admitted and runs cold to its end (I2)', async () => {
    const a = await replica('a', { maxEntries: 0 })
    const { id, turn } = await startOn(a, 'first')
    // Admitted at once: acquire_worker answers (no NoCapacity, which would retry for a minute a cycle).
    await pollUntil(() => ({ acquired: a.acquired, refused: a.refused }), { acquired: 1, refused: 0 }, 15_000)
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    await settled(id)
    const second = await a.m.send(id, browser, 'second')
    expect(await second.done).toMatchObject({ kind: 'result', subtype: 'success' })
    // A process per turn, none kept.
    expect(a.spawned).toBe(2)
    expect(a.m.warm.size()).toBe(0)
    expect(a.refused).toBe(0)
  })

  it('a send right after a turn cut at turn_max_seconds runs: the cut placement still here goes first, never WarmBusy (I3)', async () => {
    script = (r) => (conversation(r).includes('again') ? { text: 'ok' } : { hang: true })
    runtimeSettings = { ...RUNTIME_DEFAULTS, turn_max_seconds: 5 }
    // Beats far apart: the cut placement's activity hears of its timeout only well after the next send.
    const a = await replica('a', { heartbeatMs: 15_000 })
    const { id, turn } = await startOn(a, 'take too long')
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    await settled(id)
    expect(a.turnHost.active).toBe(1)
    const second = await a.m.send(id, browser, 'again')
    expect(await second.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(await said(a, id, 'WarmBusy')).toBe(false)
  })

  it('SIGTERM mid-turn: a turn that ends within the grace finishes on its replica, billed once (I4)', async () => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) => (conversation(r).includes('tool_result') ? { text: 'done' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    const a = await replica('a', { drainMs: 20_000 })
    const { id, turn } = await startOn(a, 'finish in the drain')
    await pollUntil(() => slowCalls, 1)
    const b = await replica('b')
    replicas.splice(replicas.indexOf(a), 1)
    const stopping = stopReplica(a)
    await pollUntil(() => a.turnHost.stopping, true)
    hold.release()
    const outcome = await turn.done
    await stopping
    // Billed once: its two requests, both on A.
    expect((await recorded(id)).costUsd).toBeCloseTo(2 * REPLY_COST_USD, 12)
    expect(outcome).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(b.acquired).toBe(0)
    expect(fake.messageCalls()).toHaveLength(2)
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(2 * REPLY_COST_USD, 12)
    expect(row.unpricedUsd).toBeCloseTo(0, 12)
    expect(row.turns).toBe(2)
    expect(await turnCosts(id)).toEqual([expect.closeTo(2 * REPLY_COST_USD, 12)])
  })

  it('SIGTERM mid-turn: a refusal within the grace keeps its end, never moved and billed again (I4)', async () => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) =>
      conversation(r).includes(RESTARTED_PROMPT)
        ? { text: 'ran again' }
        : conversation(r).includes('tool_result')
          ? { error: { status: 400, type: 'invalid_request_error', message: 'messages: text content blocks must be non-empty' } }
          : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } }
    const a = await replica('a', { drainMs: 20_000 })
    const { id, turn } = await startOn(a, 'refuse in the drain')
    await pollUntil(() => slowCalls, 1)
    const b = await replica('b')
    replicas.splice(replicas.indexOf(a), 1)
    const stopping = stopReplica(a)
    await pollUntil(() => a.turnHost.stopping, true)
    hold.release()
    const outcome = await turn.done
    await stopping
    // Billed once: the tool call's request; the refused one costs nothing, and nothing ran again.
    expect((await recorded(id)).costUsd).toBeCloseTo(REPLY_COST_USD, 12)
    expect(outcome).toMatchObject({ kind: 'failed' })
    expect(b.acquired).toBe(0)
    const errors = (await b.m.events.read(id, 0, 10_000)).flatMap((e) => (e.event.type === 'error' ? [e.event.code] : []))
    expect(errors).toEqual(['api_error'])
    // The tool call's request, once; the refused one costs nothing.
    expect(fake.messageCalls().some((r) => conversation(r).includes(RESTARTED_PROMPT))).toBe(false)
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(REPLY_COST_USD, 12)
    expect(await turnCosts(id)).toEqual([expect.closeTo(REPLY_COST_USD, 12)])
  })

  it('SIGTERM mid-turn: a turn still running past the grace is cut and moves, its spend counted once (I4)', async () => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) => (conversation(r).includes(RESTARTED_PROMPT) ? { text: 'carried on' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    const a = await replica('a', { drainMs: 1_000 })
    const { id, turn } = await startOn(a, 'outlast the drain')
    await pollUntil(() => slowCalls, 1)
    const b = await replica('b')
    replicas.splice(replicas.indexOf(a), 1)
    await stopReplica(a)
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(b.acquired).toBe(1)
    hold.release()
    // A's request (as A's cut turn wrote it) and B's, each once.
    // (Its turns are as classic counts a stopped turn's: the interrupted result's.)
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(2 * REPLY_COST_USD, 12)
    expect(await turnCosts(id)).toEqual([expect.closeTo(REPLY_COST_USD, 12), expect.closeTo(REPLY_COST_USD, 12)])
  })

  it('a full replica evicts its least recently used idle process for a new session, never one running a turn', async () => {
    const hold = latch()
    script = (r) =>
      conversation(r).includes('slow please') && !conversation(r).includes('tool_result')
        ? { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } }
        : { text: 'ok' }
    const a = await replica('a', { maxEntries: 1 })
    const first = await startOn(a, 'one')
    await first.turn.done
    expect(a.spawned).toBe(1)
    // Full, but idle: the next session takes its place.
    slowGate = hold.promise
    const second = await startOn(a, 'slow please')
    await pollUntil(() => slowCalls, 1)
    expect(a.spawned).toBe(2)
    expect(a.m.warm.size()).toBe(1)
    // Full, and running: a third session is refused (NoCapacity) and retried, and the running one is left alone.
    const third = await startOn(a, 'three')
    await pollUntil(() => a.refused > 0, true)
    expect(a.spawned).toBe(2)
    hold.release()
    expect(await second.turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(await third.turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(a.spawned).toBe(3)
  })

  it('a turn holding the session host queue never blocks a pinned agent-tools.<identity> call', async () => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) => (conversation(r).includes('tool_result') ? { text: 'ok' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    // One slot for turns: the held turn takes it.
    const a = await replica('a', { slots: 1 })
    const { turn } = await startOn(a, 'hold the slot')
    await pollUntil(() => slowCalls, 1)
    // A DurableSession's pinned call on the same replica (5 s to start, as `turn_tools` gets).
    const pinned = await env.client.workflow.execute('pinnedCall', {
      taskQueue: PINNED_TEST_QUEUE,
      workflowId: `pinned-${Date.now()}`,
      args: [`${TASK_QUEUE}.a`],
    })
    expect(pinned).toBe('pinned on a')
    hold.release()
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
  })
})
