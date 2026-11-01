import { type Client, WithStartWorkflowOperation, WorkflowNotFoundError } from '@temporalio/client'
import { historyToJSON } from '@temporalio/common/lib/proto-utils.js'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { bundleWorkflowCode, Worker, type WorkflowBundle } from '@temporalio/worker'
import { randomUUID } from 'node:crypto'
import { writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { DurableGate, sessionWorkflowId } from '../src/gate/durable.js'
import { INPUT_DECIDED_SIGNAL } from '../src/gate/names.js'
import { EventLog } from '../src/sessions/eventLog.js'
import { RUNTIME_DEFAULTS } from '../src/sessions/runtimeSettings.js'
import { TASK_QUEUE } from '../src/temporal/names.js'
import { sessionGateActivities } from '../src/temporal/session/gateActivities.js'
import type { Continuation, Message, OpenInputArgs, SessionStart, SessionTurnInput, SessionTurnResult } from '../src/temporal/session/models.js'
import {
  ACQUIRE_WORKER_ACTIVITY,
  OPEN_INPUT_UPDATE,
  RELEASE_WORKER_ACTIVITY,
  SEND_MESSAGE_UPDATE,
  SESSION_FINISH_ACTIVITY,
  SESSION_GATE_ACTIVITIES as GATE,
  SESSION_QUEUE,
  SESSION_SETTINGS_ACTIVITY,
  SESSION_TURN_ACTIVITY,
  SESSION_WORKFLOW,
  sessionHostQueue,
} from '../src/temporal/session/names.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// ai_pending_input is the record of an open request (unified runtime spec 2026-10-13 §14.7 R5,
// plan Step 9.7): AgentSession on a Temporal dev server with the gate's real writers on Postgres
// (gateActivities.ts), and a stub `session_turn` that parks one outward call and releases, so the
// turn goes on only once the workflow knows how the entry ended. The respond is DurableGate's,
// as the route sends it: decided in the row, then the workflow woken. The other activities are
// stubs, as in agentSession.temporal.test.ts.

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))
const FIXTURES = new URL('./fixtures/agent_session_histories/', import.meta.url)
const HOST = sessionHostQueue('gate-record-test')
const OWNER = { kind: 'browser', id: 'browser', label: 'You' } as const
const skip = !TEMPORAL_CLI ? TEMPORAL_SKIP : TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

const message = (turnId: string): Message => ({ turn_id: turnId, text: 'ship the box', author: OWNER, images: [], system_append: null })

describe.skipIf(skip !== '')(`the gate's record${skip}`, { timeout: 180_000 }, () => {
  let env: TestWorkflowEnvironment
  let bundle: WorkflowBundle
  let db: Database
  let drop: () => Promise<void>
  let workers: { worker: Worker; run: Promise<void> }[] = []
  let continuations: Continuation[]
  let reads: number
  /** The entries' first recheck (GateSettings.recheckS), read as each turn starts. */
  let recheckS: number
  /** While set, a read of the row returns what it read only once `release` resolves (`held` says one waits). */
  let hold: { release: Promise<void>; held: boolean } | undefined
  const started = new Set<string>()

  beforeAll(async () => {
    env = await localTemporal()
    bundle = await bundleWorkflowCode({ workflowsPath: WORKFLOWS })
  }, 180_000)
  afterAll(async () => {
    await env?.teardown()
  })
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    continuations = []
    reads = 0
    recheckS = 1
    hold = undefined
    await startWorkers()
  })
  afterEach(async () => {
    for (const id of started) {
      await env.client.workflow
        .getHandle(sessionWorkflowId(id))
        .terminate('the test ended')
        .catch((err: unknown) => {
          if (!(err instanceof WorkflowNotFoundError)) throw err
        })
    }
    started.clear()
    await stopWorkers()
    await drop()
  })

  /** The first placement parks `print_output` and releases; a continuation records how its call ended. */
  async function runTurn(input: SessionTurnInput): Promise<SessionTurnResult> {
    if (input.continuation) {
      continuations.push(input.continuation)
      return { status: 'done', hostQueue: HOST }
    }
    const handle = env.client.workflow.getHandle(sessionWorkflowId(input.sessionId))
    const opened = await handle.executeUpdate<{ request_id: string }, [OpenInputArgs]>(OPEN_INPUT_UPDATE, {
      args: [{ turn_id: input.turnId, call: { id: 'toolu_1', name: 'print_output', input: { output_id: 'o1' } } }],
    })
    return { status: 'released', hostQueue: HOST, parked: [opened.request_id] }
  }

  /** The workers main.ts runs for a session, the gate's activities real and their first recheck at `recheckS`. */
  async function startWorkers(): Promise<void> {
    const gate = sessionGateActivities({ sql: db.sql, events: new EventLog(db.sql) })
    const make = (taskQueue: string, activities: Record<string, (...args: never[]) => unknown>, workflowBundle?: WorkflowBundle) =>
      Worker.create({
        connection: env.nativeConnection,
        taskQueue,
        activities,
        // A stop the test makes is bounded however loaded the machine is: past these, in-flight work is cut.
        shutdownGraceTime: '1s',
        shutdownForceTime: '10s',
        // The restarted workers take the run at once, not after its old sticky queue times out.
        ...(workflowBundle ? { workflowBundle, stickyQueueScheduleToStartTimeout: '1s' } : {}),
      })
    const made = [
      await make(SESSION_QUEUE, { [SESSION_SETTINGS_ACTIVITY]: async () => RUNTIME_DEFAULTS }, bundle),
      await make(TASK_QUEUE, {
        ...gate,
        [GATE.settings]: async () => ({ approvalExpiryS: 600, questionExpiryS: 3600, recheckS }),
        [GATE.read]: async (args: { requestId: string }) => {
          reads++
          const row = await gate[GATE.read](args)
          const held = hold
          if (held) {
            held.held = true
            await held.release
          }
          return row
        },
        [GATE.describe]: async () => ({ summary: 'o1', input_hash: 'a'.repeat(64) }),
        [ACQUIRE_WORKER_ACTIVITY]: async () => ({ hostQueue: HOST, identity: 'gate-record-test' }),
        [SESSION_FINISH_ACTIVITY]: async () => true,
      }),
      await make(HOST, {
        [SESSION_TURN_ACTIVITY]: async (input: SessionTurnInput) => runTurn(input),
        [RELEASE_WORKER_ACTIVITY]: async () => undefined,
      }),
    ]
    workers = made.map((worker) => ({ worker, run: worker.run() }))
  }
  async function stopWorkers(): Promise<void> {
    for (const { worker } of workers) if (worker.getState() === 'RUNNING') worker.shutdown()
    await Promise.all(workers.map((w) => w.run.catch(() => undefined)))
    workers = []
  }

  async function send(client: Client, id: string) {
    started.add(id)
    await db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, mode, runtime)
      VALUES (${id}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'running', 10, 1, 'durable', 'unified')`
    const start: SessionStart = { session_id: id, creator: OWNER, owner: OWNER, max_turns: 50, system_append: null }
    const operation = new WithStartWorkflowOperation<(start: SessionStart) => Promise<void>>(SESSION_WORKFLOW, {
      workflowId: sessionWorkflowId(id),
      taskQueue: SESSION_QUEUE,
      args: [start],
      workflowIdConflictPolicy: 'USE_EXISTING',
      workflowIdReusePolicy: 'REJECT_DUPLICATE',
    })
    await client.workflow.executeUpdateWithStart(SEND_MESSAGE_UPDATE, { args: [message('t1')], updateId: 't1', startWorkflowOperation: operation })
  }

  /** The session's open entry, once its turn has been released: its request id and its `gate`. */
  async function parked(id: string): Promise<{ request_id: string; gate: unknown }> {
    let rows: { request_id: string; gate: unknown }[] = []
    await expect.poll(async () => (rows = await db.sql`SELECT request_id, gate FROM ai_pending_input WHERE session_id = ${id}`).length, { timeout: 20_000 }).toBe(1)
    return rows[0]!
  }

  /**
   * The run that parked the session's entry. A turn's end continues the session as new, so the
   * workflow id alone reads whichever run is latest, which may have no part in the park.
   */
  const parkedRun = async (id: string) => {
    const { runId } = await env.client.workflow.getHandle(sessionWorkflowId(id)).describe()
    return env.client.workflow.getHandle(sessionWorkflowId(id), runId)
  }

  /** Whether the history holds a completed `session_turn`: the first placement's `released`. */
  async function turnReleased(id: string): Promise<boolean> {
    const events = (await env.client.workflow.getHandle(sessionWorkflowId(id)).fetchHistory()).events ?? []
    const turns = new Set(events.filter((e) => e.activityTaskScheduledEventAttributes?.activityType?.name === SESSION_TURN_ACTIVITY).map((e) => String(e.eventId)))
    return events.some((e) => turns.has(String(e.activityTaskCompletedEventAttributes?.scheduledEventId)))
  }

  const approve = (gate: DurableGate, id: string, requestId: string) =>
    gate.respond(id, { request_id: requestId, response: { kind: 'approval', decision: 'approve' }, responder: OWNER, role: 'browser' })

  /** How each continued call's entry ended, as the turn was told; a continuation that was not `parked` by its kind. */
  const resolutions = () => continuations.flatMap((c): unknown[] => (c.kind === 'parked' ? c.calls.map((call) => call.resolution) : [c.kind]))

  it("a respond whose wake is lost is picked up by the timer's read", async () => {
    const id = randomUUID()
    await send(env.client, id)
    const entry = await parked(id)
    const run = await parkedRun(id)
    // Recorded with what the validator reads: the respond is decided in the row.
    expect(entry.gate).toEqual(expect.objectContaining({ id: entry.request_id, kind: 'approval', inputHash: 'a'.repeat(64), inRun: false }))
    const lost = new DurableGate(env.client, { db: { sql: db.sql, events: new EventLog(db.sql) }, wake: async () => {} })
    expect(await approve(lost, id, entry.request_id)).toEqual({ kind: 'approval', outcome: 'approved' })
    // Nothing was sent to the workflow; its recheck read the row, and the turn went on with the decision.
    await expect.poll(resolutions, { timeout: 20_000 }).toEqual([{ kind: 'approval', outcome: 'approved' }])
    const history = await run.fetchHistory()
    expect(history.events?.some((e) => e.workflowExecutionSignaledEventAttributes?.signalName === INPUT_DECIDED_SIGNAL)).toBe(false)
    expect(history.events?.some((e) => e.workflowExecutionUpdateAcceptedEventAttributes?.acceptedRequest?.input?.name === 'respond')).toBe(false)
    expect(reads).toBeGreaterThan(0)
    if (process.env.UPDATE_SESSION_HISTORIES) await writeFile(new URL('park_record.json', FIXTURES), `${historyToJSON(history)}\n`)
  })

  it('a woken workflow reads the decision at once, without waiting for its recheck', async () => {
    // No recheck comes due within the test: only the wake can end the entry.
    recheckS = 600
    const id = randomUUID()
    await send(env.client, id)
    const entry = await parked(id)
    const run = await parkedRun(id)
    const before = reads
    expect(await approve(new DurableGate(env.client, { db: { sql: db.sql, events: new EventLog(db.sql) } }), id, entry.request_id)).toEqual({ kind: 'approval', outcome: 'approved' })
    await expect.poll(resolutions, { timeout: 20_000 }).toEqual([{ kind: 'approval', outcome: 'approved' }])
    // The parked run's history: the turn's end may have continued the session as new by now.
    const history = await run.fetchHistory()
    expect(history.events?.some((e) => e.workflowExecutionSignaledEventAttributes?.signalName === INPUT_DECIDED_SIGNAL)).toBe(true)
    expect(reads).toBeGreaterThan(before)
  })

  it('a wake that lands while a read of the row is in flight reads it again once that read found nothing', async () => {
    recheckS = 600
    const id = randomUUID()
    await send(env.client, id)
    const entry = await parked(id)
    const run = await parkedRun(id)
    // A read that reads the row before the decision commits, still in flight: a spurious wake starts it.
    let release!: () => void
    const held = { release: new Promise<void>((r) => (release = r)), held: false }
    hold = held
    await run.signal(INPUT_DECIDED_SIGNAL, { request_id: entry.request_id })
    await expect.poll(() => held.held, { timeout: 20_000 }).toBe(true)
    hold = undefined
    expect(await approve(new DurableGate(env.client, { db: { sql: db.sql, events: new EventLog(db.sql) } }), id, entry.request_id)).toEqual({ kind: 'approval', outcome: 'approved' })
    // The decision's wake reaches the workflow while that read still waits, so it joins it.
    const wakeTaken = async () => {
      const events = (await run.fetchHistory()).events ?? []
      const last = events.findLastIndex((e) => e.workflowExecutionSignaledEventAttributes?.signalName === INPUT_DECIDED_SIGNAL)
      return events.slice(last + 1).some((e) => e.workflowTaskCompletedEventAttributes)
    }
    await expect.poll(wakeTaken, { timeout: 20_000 }).toBe(true)
    release()
    // That read found nothing; the wake reads again, rather than the entry waiting 600 s for its recheck.
    await expect.poll(resolutions, { timeout: 20_000 }).toEqual([{ kind: 'approval', outcome: 'approved' }])
  })

  it('a respond to an entry whose run has ended (before the sweep removes its row) is stale, and decides nothing', async () => {
    const id = randomUUID()
    await send(env.client, id)
    const entry = await parked(id)
    await env.client.workflow.getHandle(sessionWorkflowId(id)).terminate('the test ended the run')
    await expect(approve(new DurableGate(env.client, { db: { sql: db.sql, events: new EventLog(db.sql) } }), id, entry.request_id)).rejects.toMatchObject({ code: 'stale' })
    expect((await db.sql`SELECT 1 FROM ai_pending_input WHERE request_id = ${entry.request_id}`).length).toBe(1)
    expect((await db.sql`SELECT 1 FROM ai_input_responses WHERE request_id = ${entry.request_id}`).length).toBe(0)
  })

  it('a restart of every worker while a park is open keeps it, and its resolution', async () => {
    const id = randomUUID()
    await send(env.client, id)
    const entry = await parked(id)
    // The release is in the history first: a stop before session_turn's result reached it places
    // the turn again on restart (a `restarted` continuation, not the parked one this test reads).
    await expect.poll(() => turnReleased(id), { timeout: 30_000 }).toBe(true)
    await stopWorkers()
    // Decided while no worker runs: the row takes it, and the wake waits in the history.
    expect(await approve(new DurableGate(env.client, { db: { sql: db.sql, events: new EventLog(db.sql) } }), id, entry.request_id)).toEqual({ kind: 'approval', outcome: 'approved' })
    expect((await db.sql`SELECT 1 FROM ai_pending_input WHERE session_id = ${id}`).length).toBe(0)
    await startWorkers()
    await expect.poll(resolutions, { timeout: 120_000 }).toEqual([{ kind: 'approval', outcome: 'approved' }])
  })
})
