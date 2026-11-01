import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk'
import type { Context } from '@temporalio/activity'
import { defaultPayloadConverter } from '@temporalio/common'
import { WorkflowNotFoundError } from '@temporalio/client'
import { historyToJSON } from '@temporalio/common/lib/proto-utils.js'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { bundleWorkflowCode, Worker } from '@temporalio/worker'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Sql } from 'postgres'
import { z } from 'zod'
import { AuditLog } from '../src/audit/log.js'
import { connectDatabase, type Database } from '../src/db.js'
import { sessionWorkflowId } from '../src/gate/durable.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { type SessionManager, type SessionManagerDeps, SETTING_SESSION_MAX_TURNS } from '../src/sessions/manager.js'
import { RUNTIME_DEFAULTS } from '../src/sessions/runtimeSettings.js'
import { RESTARTED_PROMPT, TurnHost } from '../src/sessions/turnHost.js'
import { TASK_QUEUE } from '../src/temporal/names.js'
import { acquireActivities, liveHostQueues, releaseActivities } from '../src/temporal/session/affinity.js'
import { sessionFinishActivities } from '../src/temporal/session/finishActivity.js'
import { sessionGateActivities } from '../src/temporal/session/gateActivities.js'
import { type HostWorkers, startHostWorkers } from '../src/temporal/session/hostWorkers.js'
import type { AcquireInput } from '../src/temporal/session/models.js'
import {
  ACQUIRE_WORKER_ACTIVITY,
  SESSION_QUEUE,
  SESSION_SETTINGS_ACTIVITY,
  sessionHostQueue,
} from '../src/temporal/session/names.js'
import { sessionTurnActivity } from '../src/temporal/session/turnActivity.js'
import { AgentWorker, workerIdentity } from '../src/temporal/worker.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, REPLY_COST_USD, startFakeAnthropic } from './support/fakeAnthropic.js'
import { liveClaudeCodes } from './support/claudeProcesses.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP, polledHosts } from './support/temporal.js'
import { dumpThenCleanUp, dumpWorkflow } from './support/workflowDump.js'

// Session affinity (unified runtime spec 2026-10-13 §5.2, plan Task 5.2) end to end: AgentSession
// on a Temporal dev server, two replicas (A, B), each with its own manager, warm cache, shared
// `agent-tools` worker (acquire_worker, session.finish_turn) and host workers
// (`agent-sessions.<name>` for session_turn and release_worker, `agent-tools.<name>` for pinned
// calls), and the real Claude Code against the fake Anthropic endpoint. Which replica answers
// acquire_worker is decided by which replicas' agent-tools workers run.
//
// Timing: everything waits on a condition (a latch, the event log, the history), except where a
// Temporal timeout is what is tested: the 5 s schedule-to-start of a dead home, the 20 s
// heartbeat of a replica that stopped answering, and the 5 s time cap. Those run on the dev
// server's own timers (wall time), which is why the suite is not on the time-skipping test
// environment: its activities are real Claude Code turns, which that environment does not skip
// over, and the workflow needs the dev server's Updates and worker deployments. The waits here
// (`pollUntil`, a `setTimeout` loop; the latches) are on Node's monotonic timers.

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

// The lone-stall test's budget (#3693), from its phases as the dump's timeline measured them
// (local, load 11-15, host clock stepping +-8.5 s every ~2 s: 4 runs; the hosted runs of the
// report): no single 180 s, a sum of what each phase may take.
/** session_turn's heartbeatTimeout (workflow.ts). */
const HEARTBEAT_TIMEOUT_MS = 20_000
/** One step of this host's wall clock, which the dev server's timers run on (measured 8.0-8.6 s). */
const CLOCK_STEP_MS = 10_000
/**
 * A stalled placement is given up on this long after it froze. The 0.8 is measured, not
 * documented: freeze to the second acquire took 35.0-35.6 s hosted and 35.7-36.3 s locally,
 * which matches the SDK sending a beat at once, flushing the next (throttled) at 80 % of the
 * heartbeat timeout, and the server's timeout running from that flush. Two clock steps, not one:
 * the measured local worst case (38.6 s) is already 2.6 s past it, and a step can land on the
 * server's timer as well.
 */
const STALL_MS = 0.8 * HEARTBEAT_TIMEOUT_MS + HEARTBEAT_TIMEOUT_MS + 2 * CLOCK_STEP_MS
/** A placement from its acquire to its first model answer: Claude Code's start and one request (measured 0.2-9 s). */
const PLACEMENT_MS = 30_000
/** How long a new placement waits for the one it replaced to end (TurnHost.run). */
const REPLACED_WAIT_MS = 10_000
/** The replica's start (`replica`'s own wait), the first placement to its held call, the stall, the second placement after the cut, and the closing checks. */
const LONE_STALL_BUDGET_MS = 30_000 + PLACEMENT_MS + STALL_MS + REPLACED_WAIT_MS + PLACEMENT_MS + 10_000 // 166 s

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
  /** Its identity, so its queues' names: `<name>-<this test's tag>`, so no other test's turn or acquire reaches it. */
  identity: string
  m: SessionManager
  /** acquire_worker answers on this replica: places taken, referrals to other replicas (the turn was lost here), NoCapacity refusals. */
  acquired: number
  referred: number
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
  /** Every replica this test started, those it stopped too: the dump's. */
  let everyReplica: Replica[] = []
  const pools: Database[] = []
  /** The stub tool's calls, and what a call waits on before it answers. */
  let slowCalls: number
  /** The stub tool's calls that have answered. */
  let slowReturned: number
  let slowGate: Promise<void>
  /** What a call with this `q` waits on instead (two turns held apart). */
  let slowGates: Map<string, Promise<void>>
  /** What `session_settings` answers each turn (the runtime settings, §5.2). */
  let runtimeSettings: typeof RUNTIME_DEFAULTS
  /** This test's own, in each replica's identity: its session host and pinned queues are its alone. */
  let tag: string
  /** The turns this test started, and how each `done` stands: what a failed test's dump shows (#3693). */
  let watched: { id: string; label: string; state: string }[]
  /** What this test reached, and when (ms since it started, monotonic): the dump's timeline. */
  let phases: { at: number; what: string }[]
  let startedAt: number
  const phase = (what: string) => phases.push({ at: Math.round(performance.now() - startedAt), what })

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
    // Set in the database, as no route writes it (unifiedSessionsFlag.pg.test.ts).
    slowCalls = 0
    slowReturned = 0
    slowGate = Promise.resolve()
    slowGates = new Map()
    runtimeSettings = RUNTIME_DEFAULTS
    script = () => ({ text: 'ok' })
    tag = randomUUID().slice(0, 8)
    watched = []
    everyReplica = []
    phases = []
    startedAt = performance.now()
  })
  // The hook's own timeout: the dump (at most 20 s, dumpThenCleanUp) and the cleanup, each replica's stop within its grace.
  afterEach(async ({ task }) => {
    const failed = task.result?.state === 'fail'
    if (!failed) {
      // Every test's timeline, passed or not: what its time was spent on.
      // (process.stderr: vitest keeps a passed test's console output back.)
      process.stderr.write(`phases of "${task.name}" (ms): ${phases.map((p) => `${p.at} ${p.what}`).join('; ')}; end ${Math.round(performance.now() - startedAt)}\n`)
    }
    // A test that failed, its timeout included: where each session waits (#3693), before it is
    // terminated; a dump that throws never skips the cleanup.
    await dumpThenCleanUp(failed ? () => dumpSessions(task.name) : undefined, async () => {
      // This test's sessions end with it: an open one would go on asking for a home on the shared
      // `agent-tools`, where the next test's replicas answer (its own queues are named apart, that
      // one cannot be: the workflow names it).
      for (const { id } of await db.sql<{ id: string }[]>`SELECT id FROM ai_sessions`) {
        await env.client.workflow
          .getHandle(sessionWorkflowId(id))
          .terminate('the test ended')
          .catch((err: unknown) => {
            if (!(err instanceof WorkflowNotFoundError)) throw err
          })
      }
      for (const r of replicas.splice(0)) await stopReplica(r)
      await fake.close()
      for (const pool of pools.splice(0)) await pool.close()
      await drop()
    })
  }, 60_000)

  /** Every session of this test's schema: its workflow (dumpWorkflow), its row, and the test's own view (the replicas' counters, each turn's `done`). */
  async function dumpSessions(name: string): Promise<void> {
    const lines = [`=== ${name}: where it waits (#3693), at ${new Date().toISOString()}`]
    for (const r of everyReplica) {
      lines.push(
        `replica ${r.identity}: acquired=${r.acquired} referred=${r.referred} refused=${r.refused} spawned=${r.spawned} frozen=${r.frozen} placements=${r.turnHost.active} warm=${r.m.warm.size()}`,
      )
    }
    lines.push(`slow tool: called=${slowCalls} returned=${slowReturned}; model requests=${fake.messageCalls().length}`)
    for (const w of watched) lines.push(`turn ${w.label} (session ${w.id}): done ${w.state}`)
    lines.push(`phases (ms since the test started): ${phases.map((p) => `${p.at} ${p.what}`).join('; ')}; now ${Math.round(performance.now() - startedAt)}`)
    for (const row of await db.sql<Record<string, unknown>[]>`
      SELECT id, status, durable_turn_id, turn_attempt, turn_mark, cost_usd, turns FROM ai_sessions ORDER BY created_at`) {
      lines.push(`row ${JSON.stringify(row)}`)
      lines.push(await dumpWorkflow(env.client, sessionWorkflowId(String(row.id))))
      const events = await everyReplica[0]?.m.events.read(String(row.id), 0, 10_000)
      if (events) lines.push(`session events: ${events.map((e) => e.event.type).join(', ')}`)
    }
    console.error(lines.join('\n'))
  }
  /** Follows a turn's `done` for the dump. */
  function watch<T>(id: string, label: string, done: Promise<T>): Promise<T> {
    const w = { id, label, state: 'pending' }
    watched.push(w)
    done.then(
      (end) => {
        phase(`turn ${label} done`)
        w.state = `resolved ${JSON.stringify(end).slice(0, 300)}`
      },
      (err: unknown) => {
        w.state = `rejected ${String(err).slice(0, 300)}`
      },
    )
    return done
  }

  /** Whether a worker polls `queue` (1 workflow, 2 activity) now. */
  async function polled(queue: string, type: 1 | 2): Promise<boolean> {
    const { pollers } = await env.client.workflowService.describeTaskQueue({ namespace: 'default', taskQueue: { name: queue }, taskQueueType: type })
    return (pollers ?? []).length > 0
  }
  const pollUntil = <T>(read: () => Promise<T> | T, expected: T, timeout = 30_000) => expect.poll(read, { timeout, interval: 100 }).toEqual(expected)

  /** The stub tool: counts its calls and answers once `slowGate` lets it. */
  const slow = tool('slow', 'Does something slowly', { q: z.string() }, async ({ q }) => {
    slowCalls++
    phase(`slow called (${q})`)
    await (slowGates.get(q) ?? slowGate)
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
      /** How often its placements read the row (TurnHost FENCE_MS by default). */
      fenceMs?: number
      /** Sessions given a place here before its workers start (`warm.reserve`): fresh, so held (RESERVATION_HOLD_MS). */
      reserved?: string[]
      /** In place of the listing of live session hosts acquire_worker reads (the real one, cut to this test's replicas). */
      liveHosts?: () => Promise<ReadonlySet<string>>
      /** What its TurnHost writes through, in place of its pool (`latchedCostWrites`). */
      turnHostSql?: (sql: Sql) => Sql
    } = {},
  ): Promise<Replica> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const pool = connectDatabase(url, { searchPath: schema })
    pools.push(pool)
    const identity = `${name}-${tag}`
    const hostQueue = sessionHostQueue(identity)
    const r = { name, identity, acquired: 0, referred: 0, refused: 0, spawned: 0, frozen: false, drainMs: options.drainMs ?? 1_000 } as Replica
    r.m = manager({
      sql: pool.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: 'gw-affinity-test-token' }),
      settings: {
        get: <T>(key: string) =>
          Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : options.settings?.[key]) as T),
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
            phase(`${name} spawned Claude Code`)
            if (options.dead) return spawn('sleep', ['3600'], { stdio: ['pipe', 'pipe', 'ignore'] })
            return spawn(o.command, o.args, { cwd: o.cwd, env: o.env, stdio: ['pipe', 'pipe', 'ignore'] })
          },
        }),
      ...options.extra,
    })
    r.m.durableTurns = new DurableTurns({ client: env.client, sql: pool.sql, events: r.m.events, liveHosts: polledHosts(env.client, hostQueue) })
    r.turnHost = new TurnHost({
      core: r.m.turnCore,
      sql: options.turnHostSql ? options.turnHostSql(pool.sql) : pool.sql,
      images: new SessionBlobs(pool.sql),
      hostQueue,
      ...(options.fenceMs ? { fenceMs: options.fenceMs } : {}),
    })
    for (const id of options.reserved ?? []) expect(r.m.warm.reserve(id)).toBe('reserved')
    const acquire = acquireActivities({
      warm: r.m.warm,
      hostQueue,
      identity,
      // The real listing (the pollers' identities), cut to this test's replicas: the dev server
      // still lists the earlier tests' pollers as fresh for a minute after their workers stopped.
      liveHosts:
        options.liveHosts ??
        (async () => {
          const live = await liveHostQueues(env.client)
          return new Set([...live].filter((queue) => replicas.some((other) => sessionHostQueue(other.identity) === queue)))
        }),
    })[ACQUIRE_WORKER_ACTIVITY]
    /** acquire_worker here, counted: a place taken, a referral to other replicas, or a refusal. */
    const counted = async (input: AcquireInput) => {
      try {
        const answer = await acquire(input)
        if (answer.elsewhere?.length) r.referred++
        else r.acquired++
        phase(`${name} acquire_worker ${answer.elsewhere?.length ? 'referred' : 'acquired'}`)
        return answer
      } catch (err) {
        r.refused++
        phase(`${name} acquire_worker refused`)
        throw err
      }
    }
    r.tools = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      taskQueue: TASK_QUEUE,
      identity: workerIdentity(identity),
      activities: {
        [ACQUIRE_WORKER_ACTIVITY]: counted,
        ...sessionFinishActivities(pool.sql, r.m.events),
        // `gate.settings`, read as each turn starts, and the gate's writers (main.ts).
        ...sessionGateActivities({ sql: pool.sql, events: r.m.events }),
      },
      shutdownGraceMs: 1_000,
      log: () => {},
    })
    r.hosts = startHostWorkers({
      address: env.address,
      namespace: 'default',
      identity,
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
          // Asked here directly when another replica refers the turn.
          [ACQUIRE_WORKER_ACTIVITY]: counted,
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
    everyReplica.push(r)
    phase(`replica ${name} up`)
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

  /**
   * `sql`, with every spend write of the session row (`cost_usd = cost_usd + …`: TurnHost's
   * running spend and its end) held until `gate` opens; `count.held` and `count.landed` say how
   * many were asked for and how many have committed, `count.values` each one's parameters.
   */
  function latchedCostWrites(sql: Sql, gate: Promise<void>, count: { held: number; landed: number; values: unknown[][] }): Sql {
    return new Proxy(sql, {
      apply(target, self, args: unknown[]) {
        const strings = args[0]
        if (!Array.isArray(strings) || !strings.join('').includes('SET cost_usd = cost_usd +')) return Reflect.apply(target, self, args)
        count.held++
        count.values.push(args.slice(1))
        return gate.then(async () => {
          const rows = await (Reflect.apply(target, self, args) as Promise<unknown>)
          count.landed++
          return rows
        })
      },
    })
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
    void watch(started.session.id, prompt, started.turn!.done)
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
    void watch(id, 'second', second.done)
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

  it("counts a request a command makes with the process's token, which no result prices (Step 9.4)", async () => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) => (conversation(r).includes('tool_use') || conversation(r).includes('from a command') ? { text: 'carried on' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    const a = await replica('a')
    const { id, turn } = await startOn(a, 'do the slow thing')
    await pollUntil(() => slowCalls, 1)
    // What a `Bash` command of the turn would find in its environment: the gateway's URL and
    // token, never the credential (gateway/server.ts).
    const [claude] = liveClaudeCodes()
    const environ = Object.fromEntries(
      readFileSync(`/proc/${claude!.pid}/environ`, 'utf8')
        .split('\0')
        .filter(Boolean)
        .map((kv) => [kv.slice(0, kv.indexOf('=')), kv.slice(kv.indexOf('=') + 1)]),
    )
    expect(JSON.stringify(environ)).not.toContain('gw-affinity-test-token')
    const res = await fetch(`${environ.ANTHROPIC_BASE_URL}/v1/messages`, {
      method: 'POST',
      headers: { authorization: `Bearer ${environ.ANTHROPIC_AUTH_TOKEN}`, 'content-type': 'application/json', 'anthropic-version': '2023-06-01' },
      body: JSON.stringify({ model: 'claude-sonnet-4-5', max_tokens: 5, messages: [{ role: 'user', content: 'from a command' }] }),
    })
    expect(res.status).toBe(200)
    await res.text()
    hold.release()
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    await settled(id)
    // Claude Code's two requests, and the command's, which only the gateway saw priced.
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(3 * REPLY_COST_USD, 12)
    expect(row.unpricedUsd).toBeCloseTo(REPLY_COST_USD, 12)
    expect(await turnCosts(id)).toEqual([expect.closeTo(3 * REPLY_COST_USD, 12)])
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
    const began = performance.now()
    const second = await b.m.send(id, browser, 'second')
    void watch(id, 'second', second.done)
    const run = await runningRun(id)
    expect(await second.done).toMatchObject({ kind: 'result', subtype: 'success' })
    // The first try waited its 5 s on A's queue, then acquire ran again, on B (measured on the
    // monotonic clock: the server's timer is the thing under test, not the host's wall clock).
    expect(performance.now() - began).toBeGreaterThanOrEqual(5_000)
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
    // A pushed the message into a process that never answers, and its beat says so (the SDK
    // throttles beats to 80% of the 20 s heartbeat timeout, so this can take a while).
    await pollUntil(async () => ((await beatOnServer(id)) as { pushed?: boolean } | undefined)?.pushed === true, true, 60_000)
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
    // One request, billed once: the row, B's turn, and A's cut placement at nothing.
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(REPLY_COST_USD, 12)
    expect(row.turns).toBe(1)
    expect((await turnCosts(id)).sort((x, y) => x - y)).toEqual([0, expect.closeTo(REPLY_COST_USD, 12)])
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
    expect(await turnCosts(id)).toEqual([expect.closeTo(REPLY_COST_USD, 12), expect.closeTo(REPLY_COST_USD, 12)])
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
    // B is up before A stalls: the turn has another replica to go to, so A, if asked, refers it there.
    const b = await replica('b')
    // A's heartbeat stalls; A itself (its process, its agent-tools worker) stays up.
    a.frozen = true
    // The heartbeat times out and acquire runs again: B takes the turn, never A.
    await pollUntil(() => b.acquired, 1, 60_000)
    expect(a.acquired).toBe(1)
    expect(a.refused).toBe(0)
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(b.acquired).toBe(1)
    expect(a.acquired).toBe(1)
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
    // A's request and B's, each once: on the row, and in each placement's own cost row.
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(2 * REPLY_COST_USD, 12)
    expect(row.turns).toBe(1)
    expect(await turnCosts(id)).toEqual([expect.closeTo(REPLY_COST_USD, 12), expect.closeTo(REPLY_COST_USD, 12)])
  })

  it('a replaced placement`s writes are refused at the write, before its tick: no event, no transcript entry, and its spend still counts', async () => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) =>
      conversation(r).includes('slow done')
        ? { text: 'late from A' }
        : conversation(r).includes(RESTARTED_PROMPT)
          ? { text: 'carried on' }
          : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } }
    // A never hears the move: deaf to its activity's cancel, and its row check never comes round.
    const a = await replica('a', { deaf: true, fenceMs: 3_600_000 })
    const { id, turn } = await startOn(a, 'do the slow thing')
    await pollUntil(() => slowCalls, 1)
    // B answers the acquire once A's heartbeat stalls: A's agent-tools worker is gone by then.
    const b = await replica('b')
    await a.tools.stop()
    a.frozen = true
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    await settled(id)
    expect(b.acquired).toBe(1)
    const entries = async () =>
      Number((await db.sql<{ n: string }[]>`SELECT count(*)::text AS n FROM ai_session_entries WHERE session_id = ${id} AND subpath = ''`)[0]!.n)
    const before = { events: (await a.m.events.read(id, 0, 10_000)).length, entries: await entries() }
    // B holds the turn now. A's call answers, and A carries on: its tool result, a model request, its reply.
    hold.release()
    await pollUntil(() => a.turnHost.active, 0)
    expect(fake.messageCalls().filter((r) => conversation(r).includes('slow done'))).toHaveLength(1)
    // Its spend counts: N = 2 requests on A (the tool call, the late reply), M = 1 on B.
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(3 * REPLY_COST_USD, 12)
    expect((await turnCosts(id)).sort((x, y) => x - y)).toEqual([expect.closeTo(REPLY_COST_USD, 12), expect.closeTo(2 * REPLY_COST_USD, 12)])
    // None of its writes landed: no event, no transcript entry.
    expect(await said(a, id, 'late from A')).toBe(false)
    expect(await said(a, id, 'slow done')).toBe(false)
    expect({ events: (await a.m.events.read(id, 0, 10_000)).length, entries: await entries() }).toEqual(before)
  })

  it('a replica restarted in place takes its lost turn back: the one home there is, lost on, is a home still', async () => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) => (conversation(r).includes(RESTARTED_PROMPT) ? { text: 'carried on' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    const a = await replica('a', { drainMs: 1_000 })
    const { id, turn } = await startOn(a, 'outlast the restart')
    await pollUntil(() => slowCalls, 1)
    // SIGTERM, then the same container again: its workers stop (the turn, still running past
    // the grace, is cut and fails ShuttingDown) and new ones start under the same identity, so
    // under the same queue names, which the turn was just lost on.
    replicas.splice(replicas.indexOf(a), 1)
    await stopReplica(a)
    const again = await replica('a')
    // No other replica can take the turn: the restarted one is asked, and takes it.
    await pollUntil(() => again.acquired, 1, 60_000)
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(again.refused + again.referred).toBe(0)
    // Resumed, not re-sent: the prompt had landed before the restart.
    const resumed = fake.messageCalls().at(-1)!
    expect(conversation(resumed)).toContain(RESTARTED_PROMPT)
    expect(userTurns(resumed, 'outlast the restart')).toBe(1)
    expect(slowCalls).toBe(1)
    expect(again.spawned).toBe(1)
    hold.release()
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(2 * REPLY_COST_USD, 12)
  })

  it('a replica restarted in place: the cut placement`s spend, landing only after the restart wrote its own, still counts once (#3695)', async () => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) => (conversation(r).includes(RESTARTED_PROMPT) ? { text: 'carried on' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    // Every spend write of the first placement waits until the restart's turn has ended. (So its
    // end, which follows its writes, never fails ShuttingDown within the stop: its worker gives
    // the activity up, and the turn is lost to its heartbeat instead, as a replica that died.)
    const writes = latch()
    const count = { held: 0, landed: 0, values: [] as unknown[][] }
    const a = await replica('a', { turnHostSql: (sql) => latchedCostWrites(sql, writes.promise, count) })
    const { id, turn } = await startOn(a, 'outlast the restart')
    await pollUntil(() => slowCalls, 1)
    // Its request has been flushed (message_stop), and is held.
    await pollUntil(() => count.held > 0, true)
    replicas.splice(replicas.indexOf(a), 1)
    await stopReplica(a)
    const again = await replica('a')
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(again.acquired).toBe(1)
    // The restart claimed the row, read its spend and wrote its own while none of A's had landed.
    expect(count.landed).toBe(0)
    const before = await recorded(id)
    writes.release()
    // A's two writes: its running spend (its one request, at message_stop), then its end, which
    // waits for the first (Spend.settled) and so is asked for only now.
    await pollUntil(() => count.landed, 2)
    expect(count.held).toBe(2)
    hold.release()
    const row = await recorded(id)
    // One request on A, one on the restart: each once, whichever landed first. Increments, so
    // no write is lost to another's order.
    expect({ before: before.costUsd, after: row.costUsd }).toEqual({ before: expect.closeTo(REPLY_COST_USD, 12), after: expect.closeTo(2 * REPLY_COST_USD, 12) })
    // A's end writes its audit row only after its spend's UPDATE has landed (TurnHost#write), so
    // `landed` reaching 2 does not yet mean the row is there (#4817): wait for it.
    await pollUntil(() => turnCosts(id), [expect.closeTo(REPLY_COST_USD, 12), expect.closeTo(REPLY_COST_USD, 12)])
    // `turns` as classic reckons each placement's end (turnCore.ts tallyTurn): the restart's
    // result, its one request; then A's, a stopped turn with a result, whose `num_turns` it adds.
    // (Its end's parameters: the cost, the turns and the unpriced increments.)
    const aTurns = Number(count.values[1]![1])
    expect(before.turns).toBe(1)
    expect(aTurns).toBeGreaterThan(0)
    expect(row.turns).toBe(before.turns + aTurns)
  })

  it('a lone replica whose heartbeat stalled takes its own turn back, its stalled placement cut first', async () => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) => (conversation(r).includes(RESTARTED_PROMPT) ? { text: 'carried on' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    const a = await replica('a')
    const { id, turn } = await startOn(a, 'do the slow thing')
    const run = await runningRun(id)
    await pollUntil(() => slowCalls, 1)
    a.frozen = true
    phase('a frozen')
    // The heartbeat times out and acquire runs again, with A excluded: A is the only replica
    // there is, so A is asked and takes it.
    await pollUntil(() => a.acquired, 2, STALL_MS)
    a.frozen = false
    phase('a unfrozen')
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(a.refused + a.referred).toBe(0)
    const timeouts = (await historyTypes(id, run)).filter((e) => e.eventType === 'EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT')
    expect(timeouts.map((e) => e.activityTaskTimedOutEventAttributes?.failure?.timeoutFailureInfo?.timeoutType)).toEqual(['TIMEOUT_TYPE_HEARTBEAT'])
    // The new placement cut the stalled one (`replaced`) and resumed on a process of its own.
    const resumed = fake.messageCalls().at(-1)!
    expect(conversation(resumed)).toContain(RESTARTED_PROMPT)
    expect(slowCalls).toBe(1)
    expect(a.spawned).toBe(2)
    await pollUntil(() => a.turnHost.active, 0)
    hold.release()
    await pollUntil(() => slowReturned, 1)
    expect(await said(a, id, 'slow done')).toBe(false)
    const row = await recorded(id)
    expect(row.costUsd).toBeCloseTo(2 * REPLY_COST_USD, 12)
  }, LONE_STALL_BUDGET_MS)

  it('a lost home asked alone refers the turn to the other live replica, which reserves on its own queue and takes it', async () => {
    const hold = latch()
    slowGate = hold.promise
    script = (r) => (conversation(r).includes(RESTARTED_PROMPT) ? { text: 'carried on' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    const a = await replica('a')
    const { id, turn } = await startOn(a, 'do the slow thing')
    await pollUntil(() => slowCalls, 1)
    const b = await replica('b')
    await pollUntil(async () => (await liveHostQueues(env.client)).has(sessionHostQueue(b.identity)), true)
    // Only A can answer the shared acquire: B's agent-tools worker stops (its poller stays listed,
    // fresh, for a minute), and B's session host worker still runs.
    await b.tools.stop()
    a.frozen = true
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    // A, excluded, referred the turn to B; B, asked on its own queue, reserved a place and ran it.
    expect({ acquired: a.acquired, referred: a.referred, refused: a.refused }).toEqual({ acquired: 1, referred: 1, refused: 0 })
    expect({ acquired: b.acquired, refused: b.refused, spawned: b.spawned }).toEqual({ acquired: 1, refused: 0, spawned: 1 })
    expect(conversation(fake.messageCalls().at(-1)!)).toContain(RESTARTED_PROMPT)
    expect(await said(b, id, 'carried on')).toBe(true)
    await pollUntil(() => a.turnHost.active, 0)
    hold.release()
  })

  /**
   * A's turn lost to a restart in place while B is listed live but stopped (its pollers stay
   * listed, fresh, for a minute after its workers stop): the restarted A, the only replica to
   * answer the shared acquire, refers the turn to B, whose queue never starts it (5 s), and is
   * then asked again on its own queue.
   */
  async function restartWhileBStopped(again: Parameters<typeof replica>[1]) {
    slowGate = latch().promise
    script = (r) => (conversation(r).includes(RESTARTED_PROMPT) ? { text: 'carried on' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    const a = await replica('a')
    const { id, turn } = await startOn(a, 'outlast the restart')
    const run = await runningRun(id)
    await pollUntil(() => slowCalls, 1)
    const b = await replica('b')
    await pollUntil(async () => (await liveHostQueues(env.client)).has(sessionHostQueue(b.identity)), true)
    await Promise.all([b.tools.stop(), b.hosts.stop()])
    replicas.splice(replicas.indexOf(a), 1)
    await stopReplica(a)
    return { id, turn, run, a: await replica('a', again), b }
  }

  it('a lost home every referral failed for reserves its place again: not full, it takes the turn', async () => {
    const { id, turn, run, a } = await restartWhileBStopped({})
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    // Referred to B, which never started it; then asked on its own queue, where it reserved.
    expect({ acquired: a.acquired, referred: a.referred, refused: a.refused, spawned: a.spawned }).toEqual({ acquired: 1, referred: 1, refused: 0, spawned: 1 })
    const timeouts = (await historyTypes(id, run)).filter((e) => e.eventType === 'EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT')
    expect(timeouts.map((e) => e.activityTaskTimedOutEventAttributes?.failure?.timeoutFailureInfo?.timeoutType)).toEqual(['TIMEOUT_TYPE_SCHEDULE_TO_START'])
    expect(conversation(fake.messageCalls().at(-1)!)).toContain(RESTARTED_PROMPT)
  })

  it('a lost home every referral failed for reserves its place again: full, it refuses NoCapacity and never goes over its cap', async () => {
    // The restarted A's one place is another session's, freshly promised (held RESERVATION_HOLD_MS).
    const { id, turn, a } = await restartWhileBStopped({ maxEntries: 1, reserved: ['someone else'] })
    await pollUntil(() => a.refused > 0, true, 60_000)
    expect(a.referred).toBe(1)
    expect(a.spawned).toBe(0)
    // The placement moves on: the last asks with no exclusion, and A takes it once the other
    // session's hold lapses (RESERVATION_HOLD_MS, 30 s, inside the acquire's 60 s; measured:
    // refusals until 26 s, acquired at 37 s, so about 23 s to spare, on wall-clock timers): a
    // turn that failed unplaced would be a hold that never lapses.
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect({ acquired: a.acquired, spawned: a.spawned }).toEqual({ acquired: 1, spawned: 1 })
    expect(await said(a, id, 'could not be placed')).toBe(false)
  })

  it('a lost home that cannot list the live replicas takes its turn back, as if alone, never failing the acquire', async () => {
    slowGate = latch().promise
    script = (r) => (conversation(r).includes(RESTARTED_PROMPT) ? { text: 'carried on' } : { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } })
    const a = await replica('a')
    const { turn } = await startOn(a, 'outlast the restart')
    await pollUntil(() => slowCalls, 1)
    replicas.splice(replicas.indexOf(a), 1)
    await stopReplica(a)
    // DescribeTaskQueue refused (a role that may not describe a queue).
    const again = await replica('a', { liveHosts: () => Promise.reject(new Error('PERMISSION_DENIED: describe task queue')) })
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect({ acquired: again.acquired, referred: again.referred, refused: again.refused }).toEqual({ acquired: 1, referred: 0, refused: 0 })
  })

  it('a stale placement of a turn that ended cannot write while the next turn runs under the same attempt id (the fence checks the turn)', async () => {
    const one = latch()
    const two = latch()
    slowGates.set('one', one.promise).set('two', two.promise)
    script = (r) => {
      const c = conversation(r)
      if (c.includes('slow done')) return { text: c.includes('turn two') ? 'late from B' : 'late from A' }
      return { toolUse: { name: 'mcp__stub__slow', input: { q: c.includes('turn two') ? 'two' : 'one' } } }
    }
    runtimeSettings = { ...RUNTIME_DEFAULTS, turn_max_seconds: 5 }
    // A never hears turn 1 end: deaf to its activity's cancel, and its row check never comes round.
    const a = await replica('a', { deaf: true, fenceMs: 3_600_000 })
    const { id, turn } = await startOn(a, 'do the slow thing')
    await pollUntil(() => slowCalls, 1)
    // Turn 1 ends at its time cap, never moved: A stays home, and A's placement keeps running.
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    await settled(id)
    expect(a.turnHost.active).toBe(1)
    const [row] = await db.sql<{ turn_attempt: string }[]>`SELECT turn_attempt FROM ai_sessions WHERE id = ${id}`
    const attempt = row!.turn_attempt
    expect(attempt).toMatch(/:0$/)
    // Turn 2 in the same run carries the same attempt id (`<run id>:0`): the workflow places it so
    // when its message arrives as turn 1 closes, which no test can time, so it is placed here as
    // the workflow would, on B, with session_turn claiming the row as it does.
    const turnId2 = randomUUID()
    await db.sql`UPDATE ai_sessions SET status = 'running', durable_turn_id = ${turnId2}, updated_at = now() WHERE id = ${id}`
    const b = await replica('b')
    const running = b.turnHost.run(
      { sessionId: id, turnId: turnId2, attempt, message: { turn_id: turnId2, author: browser, text: 'turn two', images: [], system_append: null } },
      { heartbeat: () => {}, cancelled: new AbortController().signal, cancelReason: () => 'interrupt' },
    )
    await pollUntil(() => slowCalls, 2)
    // While B runs turn 2: A's call answers, and A carries on: its tool result, a model request, its reply.
    one.release()
    await pollUntil(() => a.turnHost.active, 0)
    expect(fake.messageCalls().filter((r) => conversation(r).includes('slow done'))).toHaveLength(1)
    // None of A's writes landed: no event, no transcript entry.
    expect(await said(b, id, 'late from A')).toBe(false)
    expect(await said(b, id, 'slow done')).toBe(false)
    const late = await db.sql<{ n: string }[]>`SELECT count(*)::text AS n FROM ai_session_entries WHERE session_id = ${id} AND entry LIKE '%late from A%'`
    expect(Number(late[0]!.n)).toBe(0)
    two.release()
    expect(await running).toMatchObject({ status: 'done' })
    expect(await said(b, id, 'late from B')).toBe(true)
  })

  it('warm_max_per_replica 0: a turn is admitted and runs cold to its end (I2)', async () => {
    const a = await replica('a', { maxEntries: 0 })
    const { id, turn } = await startOn(a, 'first')
    // Admitted at once: acquire_worker answers (no NoCapacity, which would retry for a minute a cycle).
    await pollUntil(() => ({ acquired: a.acquired, refused: a.refused }), { acquired: 1, refused: 0 }, 15_000)
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    await settled(id)
    const second = await a.m.send(id, browser, 'second')
    void watch(id, 'second', second.done)
    expect(await second.done).toMatchObject({ kind: 'result', subtype: 'success' })
    // A process per turn, none kept.
    expect(a.spawned).toBe(2)
    expect(a.m.warm.size()).toBe(0)
    expect(a.refused).toBe(0)
  })

  it('a send right after a turn cut at turn_max_seconds runs: the cut placement still here goes first, never WarmBusy (I3)', async () => {
    script = (r) => (conversation(r).includes('again') ? { text: 'ok' } : { hang: true })
    runtimeSettings = { ...RUNTIME_DEFAULTS, turn_max_seconds: 5 }
    // The cut placement hears of its turn's end neither from its activity's cancel (deaf: the
    // SDK flushes a throttled beat at 16 s, which the server answers NotFound) nor from the row
    // (its check never comes round), so the `replaced` cut is what frees its process. Beats far
    // apart too: the 5 s time cap ends the activity long before the 20 s heartbeat timeout could.
    const a = await replica('a', { deaf: true, heartbeatMs: 60_000, fenceMs: 3_600_000 })
    const { id, turn } = await startOn(a, 'take too long')
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    await settled(id)
    expect(a.turnHost.active).toBe(1)
    const second = await a.m.send(id, browser, 'again')
    void watch(id, 'again', second.done)
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

  it('a kept home full of running turns as the next turn arrives refuses NoCapacity, and the turn moves: never past the cap, no placement spent (#3443)', async () => {
    const hold = latch()
    script = (r) =>
      conversation(r).includes('slow please') && !conversation(r).includes('tool_result')
        ? { toolUse: { name: 'mcp__stub__slow', input: { q: 'x' } } }
        : { text: 'ok' }
    const a = await replica('a', { maxEntries: 1 })
    const kept = await startOn(a, 'one')
    expect(await kept.turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    await settled(kept.id)
    // Another session takes A's one place (the kept session's idle process is evicted for it,
    // as a lapsed reservation would be) and holds it with a running turn.
    slowGate = hold.promise
    const other = await startOn(a, 'slow please')
    await pollUntil(() => slowCalls, 1)
    expect(a.spawned).toBe(2)
    const b = await replica('b')
    const run = await runningRun(kept.id)
    const second = await a.m.send(kept.id, browser, 'second')
    void watch(kept.id, 'second', second.done)
    expect(await second.done).toMatchObject({ kind: 'result', subtype: 'success' })
    // Refused on A as it arrived, so nothing was spawned there past its cap; taken by B.
    expect({ aSpawned: a.spawned, aWarm: a.m.warm.size(), bSpawned: b.spawned, bWarm: b.m.warm.size() }).toEqual({ aSpawned: 2, aWarm: 1, bSpawned: 1, bWarm: 1 })
    const failures = (await historyTypes(kept.id, run)).filter((e) => e.eventType === 'EVENT_TYPE_ACTIVITY_TASK_FAILED')
    expect(failures).toHaveLength(1)
    expect(JSON.stringify(failures[0])).toContain('NoCapacity')
    // Nothing ran on A: B sends the message itself, never the restart notice.
    const last = conversation(fake.messageCalls().at(-1)!)
    expect(last).toContain('second')
    expect(last).not.toContain(RESTARTED_PROMPT)
    expect(await said(a, kept.id, 'could not be placed')).toBe(false)
    hold.release()
    expect(await other.turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
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
      args: [`${TASK_QUEUE}.${a.identity}`],
    })
    expect(pinned).toBe('pinned on a')
    hold.release()
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
  })
})
