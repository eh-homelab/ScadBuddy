import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { Context } from '@temporalio/activity'
import { type Client, WorkflowNotFoundError } from '@temporalio/client'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { bundleWorkflowCode, Worker } from '@temporalio/worker'
import type { Sql } from 'postgres'
import { afterAll, afterEach, beforeAll } from 'vitest'
import { AuditLog } from '../../src/audit/log.js'
import { DurableGate, sessionWorkflowId } from '../../src/gate/durable.js'
import { SessionBlobs } from '../../src/sessions/blobs.js'
import { DurableTurns, type DurableTurn } from '../../src/sessions/durable.js'
import type { SessionManager, SessionManagerDeps, SessionRecord, Turn } from '../../src/sessions/manager.js'
import { readRuntimeSettings, RUNTIME_DEFAULTS } from '../../src/sessions/runtimeSettings.js'
import { TurnHost } from '../../src/sessions/turnHost.js'
import { TASK_QUEUE } from '../../src/temporal/names.js'
import { acquireActivities, releaseActivities } from '../../src/temporal/session/affinity.js'
import { sessionFinishActivities } from '../../src/temporal/session/finishActivity.js'
import { sessionGateActivities } from '../../src/temporal/session/gateActivities.js'
import {
  ACQUIRE_WORKER_ACTIVITY,
  RELEASE_WORKER_ACTIVITY,
  SESSION_QUEUE,
  SESSION_SETTINGS_ACTIVITY,
  SESSION_TURN_ACTIVITY,
  sessionHostQueue,
} from '../../src/temporal/session/names.js'
import { sessionTurnActivity } from '../../src/temporal/session/turnActivity.js'
import { gateActivities, PgSessionOwners } from '../../src/temporal/toolActivities.js'
import { AgentWorker, workerIdentity } from '../../src/temporal/worker.js'
import { localTemporal, polledHosts, TEMPORAL_CLI, TEMPORAL_SKIP } from './temporal.js'

// AgentSession for the session tests (unified runtime spec 2026-10-13 §5, plan Step 6.3): one
// Temporal dev server per test file, and the workers main.ts runs for a session's turns, in this
// process: the `agent-sessions` workflow worker, the shared `agent-tools` activities a session uses
// (acquire_worker, the gate's writers, describe_call, session.finish_turn) and one session host
// queue (`session_turn`, `release_worker`). A file calls `agentSessions()` inside its describe;
// from then on every `manager()` (support/sessions.ts) gets DurableTurns and DurableGate on that
// server, and its sessions' turns run through AgentSession on the manager's own warm cache, as a
// worker replica's do (TurnHost over `SessionManager.turnCore`), the scripted runner included.
//
// The workers are the file's; what they act on is each test's. Every activity finds its manager
// by the session its workflow is for (`session-<id>`), recorded when that manager sends a turn,
// and falls back to the manager made last: tests in a file run one after another, each on a
// database of its own. A test's sessions' workflows are terminated after it.

const WORKFLOWS = fileURLToPath(new URL('../../src/temporal/workflows.ts', import.meta.url))

/** Why `agentSessions()` cannot run here, or '' when it can: a Temporal CLI is needed. */
export const AGENT_SESSIONS_SKIP = TEMPORAL_CLI ? '' : TEMPORAL_SKIP

/** One manager the stack serves. */
type Served = {
  m: SessionManager
  sql: Sql
  turnHost: TurnHost
  audit: AuditLog
  settings: SessionManagerDeps['settings']
}

type Stack = {
  env: TestWorkflowEnvironment
  hostQueue: string
  /** The managers made in the running test, newest last. */
  served: Served[]
  /** Which manager sent each session's turns. */
  bySession: Map<string, Served>
}

let active: Stack | undefined

/** The test's manager for the activity running now: by its workflow's session, else the newest. */
function servedNow(stack: Stack): Served {
  let workflowId: string | undefined
  try {
    workflowId = Context.current().info.workflowExecution?.workflowId
  } catch {
    workflowId = undefined
  }
  const session = workflowId?.startsWith('session-') ? workflowId.slice('session-'.length) : undefined
  const served = (session && stack.bySession.get(session)) || stack.served.at(-1)
  if (!served) throw new Error('no manager is served: the test made none after agentSessions()')
  return served
}

/**
 * Runs the session tests of the calling describe through AgentSession (see the header). Call it
 * first in the describe, before the file's own hooks, and skip the describe with
 * AGENT_SESSIONS_SKIP.
 */
export function agentSessions(): void {
  let workflowWorker: Worker | undefined
  let workflowRun: Promise<void> | undefined
  let workers: AgentWorker[] = []
  let stack: Stack | undefined

  beforeAll(async () => {
    const env = await localTemporal()
    const tag = randomUUID().slice(0, 8)
    const identity = `test-${tag}`
    const hostQueue = sessionHostQueue(identity)
    stack = { env, hostQueue, served: [], bySession: new Map() }
    const s = stack
    const now = () => servedNow(s)
    workflowWorker = await Worker.create({
      connection: env.nativeConnection,
      taskQueue: SESSION_QUEUE,
      workflowBundle: await bundleWorkflowCode({ workflowsPath: WORKFLOWS }),
      activities: {
        [SESSION_SETTINGS_ACTIVITY]: async () => {
          const settings = now().settings
          return settings ? readRuntimeSettings(settings as never) : RUNTIME_DEFAULTS
        },
      },
    })
    workflowRun = workflowWorker.run()
    const acquire = async (input: unknown) =>
      acquireActivities({ warm: now().m.warm, hostQueue, identity, liveHosts: async () => new Set([hostQueue]) })[ACQUIRE_WORKER_ACTIVITY](input as never)
    const routed = (build: (served: Served) => Record<string, (args: never) => Promise<unknown>>, names: readonly string[]) =>
      Object.fromEntries(names.map((name) => [name, (args: never) => build(now())[name]!(args)]))
    const finishNames = Object.keys(sessionFinishActivities(undefined as never, undefined as never))
    const gateNames = Object.keys(sessionGateActivities(undefined as never))
    const describeNames = Object.keys(gateActivities({ audit: undefined, sessions: undefined as never }))
    const tools = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      taskQueue: TASK_QUEUE,
      identity: workerIdentity(identity),
      activities: {
        [ACQUIRE_WORKER_ACTIVITY]: acquire,
        ...routed((x) => sessionFinishActivities(x.sql, x.m.events) as never, finishNames),
        ...routed((x) => sessionGateActivities(x.sql) as never, gateNames),
        ...routed((x) => gateActivities({ audit: x.audit, sessions: new PgSessionOwners(x.sql) }) as never, describeNames),
      },
      shutdownGraceMs: 1_000,
      log: () => {},
    })
    const turn = (served: Served) => sessionTurnActivity(served.turnHost, { client: env.client, heartbeatMs: 1_000 })[SESSION_TURN_ACTIVITY]
    const host = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      taskQueue: hostQueue,
      identity: workerIdentity(identity),
      activities: {
        [SESSION_TURN_ACTIVITY]: (input: never) => turn(now())(input),
        [RELEASE_WORKER_ACTIVITY]: (input: never) => releaseActivities(now().m.warm)[RELEASE_WORKER_ACTIVITY](input),
        [ACQUIRE_WORKER_ACTIVITY]: acquire,
      },
      maxConcurrentActivityTaskExecutions: 20,
      shutdownGraceMs: 2_000,
      shutdownForceMs: 3_000,
      log: () => {},
    })
    workers = [tools, host]
    await Promise.all(workers.map((w) => w.running()))
    // DurableTurns.unready reads the pollers: wait until Temporal lists them.
    for (;;) {
      const polled = await Promise.all(
        [
          [SESSION_QUEUE, 1],
          [TASK_QUEUE, 2],
          [hostQueue, 2],
        ].map(async ([queue, type]) => {
          const { pollers } = await env.client.workflowService.describeTaskQueue({
            namespace: 'default',
            taskQueue: { name: queue as string },
            taskQueueType: type as number,
          })
          return (pollers ?? []).length > 0
        }),
      )
      if (polled.every(Boolean)) break
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
    active = stack
  }, 180_000)

  afterEach(async () => {
    if (!stack) return
    const served = stack.served.splice(0)
    const sessions = [...stack.bySession.keys()]
    stack.bySession.clear()
    await Promise.all(
      sessions.map((id) =>
        stack!.env.client.workflow
          .getHandle(sessionWorkflowId(id))
          .terminate('the test ended')
          .catch((err: unknown) => {
            if (!(err instanceof WorkflowNotFoundError)) throw err
          }),
      ),
    )
    await Promise.all(served.map((s) => s.turnHost.stop({ graceMs: 0, abortWaitMs: 2_000 })))
  }, 30_000)

  afterAll(async () => {
    active = undefined
    await Promise.all(workers.map((w) => w.stop()))
    workflowWorker?.shutdown()
    await workflowRun
    await stack?.env.teardown()
  }, 60_000)
}

/** The file's dev server, once `agentSessions()`' beforeAll has run: for a test's own workers. */
export function agentSessionsEnv(): TestWorkflowEnvironment {
  if (!active) throw new Error('no agentSessions() stack is set up in this describe')
  return active.env
}

/** The type and task queue of `session-<id>`'s latest run on the file's dev server. */
export async function sessionWorkflow(id: string): Promise<{ type: string; taskQueue: string }> {
  if (!active) throw new Error('no agentSessions() stack is set up in this describe')
  const described = await active.env.client.workflow.getHandle(sessionWorkflowId(id)).describe()
  return { type: described.type, taskQueue: described.taskQueue }
}

/**
 * Serves `m` from the file's stack, when `agentSessions()` set one up (support/sessions.ts
 * `manager()` calls it): DurableTurns and DurableGate on the dev server, and a TurnHost over its
 * warm cache. Its stop and abort reach the turns hosted for it too.
 */
export function serveManager(m: SessionManager, deps: Pick<SessionManagerDeps, 'sql' | 'settings'>): void {
  const stack = active
  if (!stack) {
    turnless(m, deps.sql)
    return
  }
  const served: Served = {
    m,
    sql: deps.sql,
    settings: deps.settings,
    audit: new AuditLog({ sql: deps.sql }),
    turnHost: new TurnHost({
      core: m.turnCore,
      sql: deps.sql,
      images: new SessionBlobs(deps.sql),
      hostQueue: stack.hostQueue,
      fenceMs: 100,
      inputPollMs: 50,
      questions: m.questions,
    }),
  }
  stack.served.push(served)
  m.durable = new DurableGate(stack.env.client)
  m.durableTurns = new ServedTurns(
    { client: stack.env.client, sql: deps.sql, events: m.events, store: m.store, liveHosts: polledHosts(stack.env.client, stack.hostQueue) },
    (id) => stack.bySession.set(id, served),
  )
  const stopTurns = m.stopTurns.bind(m)
  m.stopTurns = (options) => stopTurns({ ...options, others: Promise.all([options.others, served.turnHost.stop({ graceMs: options.graceMs, abortWaitMs: options.abortWaitMs })]).then(() => undefined) })
  const abortAll = m.abortAll.bind(m)
  m.abortAll = () => {
    abortAll()
    void served.turnHost.stop({ graceMs: 0, abortWaitMs: 2_000 })
  }
}

/**
 * Takes Temporal away from `m` for one test of what a service without it does (spec 2026-10-13
 * §10.1: a new session is refused `unavailable`): no DurableTurns or DurableGate.
 */
export function withoutTemporal(m: SessionManager): void {
  m.durableTurns = undefined
  m.durable = undefined
}

/** What a turn sent outside an agentSessions() describe says. */
export const NO_TURNS_HERE = 'this describe runs no turn: call agentSessions() in it, and skip it with AGENT_SESSIONS_SKIP (support/agentSessions.ts)'

/**
 * A Temporal that holds no workflow: every handle's call is WorkflowNotFoundError, as for a
 * session that never ran a turn; anything else is NO_TURNS_HERE.
 */
const noWorkflows = new Proxy({} as Client, {
  get: (_, key) => {
    if (key !== 'workflow') throw new Error(NO_TURNS_HERE)
    return {
      getHandle: (workflowId: string) =>
        new Proxy({}, { get: () => () => Promise.reject(new WorkflowNotFoundError('no workflow in this test', workflowId, undefined)) }),
    }
  },
})

/** DurableTurns for a manager outside an agentSessions() describe: sessions start, and a turn refuses with NO_TURNS_HERE. */
class TurnlessTurns extends DurableTurns {
  override unready(): Promise<string | undefined> {
    return Promise.resolve(undefined)
  }

  override send(): Promise<Turn | undefined> {
    return Promise.reject(new Error(NO_TURNS_HERE))
  }
}

/**
 * Gives `m`, made outside an agentSessions() describe, what a service with Temporal has, for a
 * test that runs no turn (a session's records, its files, its edits): sessions start, a turn
 * fails with NO_TURNS_HERE, and a session's workflow is never found.
 */
function turnless(m: SessionManager, sql: Sql): void {
  m.durableTurns = new TurnlessTurns({ client: noWorkflows, sql, events: m.events })
  m.durable = new DurableGate(noWorkflows)
}

/** DurableTurns that says which manager sent each session's turn. */
class ServedTurns extends DurableTurns {
  readonly #sent: (sessionId: string) => void
  constructor(deps: ConstructorParameters<typeof DurableTurns>[0], sent: (sessionId: string) => void) {
    super(deps)
    this.#sent = sent
  }

  override send(session: SessionRecord, turn: DurableTurn): Promise<Turn | undefined> {
    this.#sent(session.id)
    return super.send(session, turn)
  }
}
