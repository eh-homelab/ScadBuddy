import { type Client, WithStartWorkflowOperation, WorkflowNotFoundError, WorkflowUpdateFailedError, WorkflowUpdateStage } from '@temporalio/client'
import { Context } from '@temporalio/activity'
import { ApplicationFailure } from '@temporalio/common'
import { historyFromJSON, historyToJSON } from '@temporalio/common/lib/proto-utils.js'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { bundleWorkflowCode, Worker, type WorkflowBundle } from '@temporalio/worker'
import { randomUUID } from 'node:crypto'
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { sessionWorkflowId } from '../src/gate/durable.js'
import { PENDING_INPUT_QUERY, RESPOND_UPDATE } from '../src/gate/names.js'
import { openInput, type OpenInput, resolveInput, type ResolveInput } from '../src/gate/store.js'
import type { Database } from '../src/db.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { type SessionManager, SETTING_SESSION_MODE } from '../src/sessions/manager.js'
import { RUNTIME_DEFAULTS } from '../src/sessions/runtimeSettings.js'
import { TASK_QUEUE } from '../src/temporal/names.js'
import { sessionFinishActivities } from '../src/temporal/session/finishActivity.js'
import type { FinishTurnInput, Message, OpenInputArgs, SessionStart, SessionTurnInput, SessionTurnResult } from '../src/temporal/session/models.js'
import {
  ACQUIRE_WORKER_ACTIVITY,
  OPEN_INPUT_UPDATE,
  SESSION_GATE_ACTIVITIES as GATE,
  RELEASE_WORKER_ACTIVITY,
  SEND_MESSAGE_UPDATE,
  SESSION_FINISH_ACTIVITY,
  SESSION_QUEUE,
  UNIFIED_SESSIONS_SETTING,
  SESSION_SETTINGS_ACTIVITY,
  SESSION_TURN_ACTIVITY,
  SESSION_WORKFLOW,
  sessionHostQueue,
  TURN_QUERY,
} from '../src/temporal/session/names.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// AgentSession (unified runtime spec 2026-10-13 §5.1, plan Task 5.1) on a Temporal dev
// server: send, busy, end, continue-as-new after each turn and the replay of recorded
// histories; then DurableTurns routing a session by its recorded runtime. `acquire_worker`,
// `session_turn` and `release_worker` are stubs on one replica's queues (the real ones are
// test/sessionAffinity.temporal.test.ts's) and `session.finish_turn` records what it was
// sent, or is the real activity where a test reads the event log.

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))
const STANDIN_WORKFLOWS = fileURLToPath(new URL('./support/durableSessionWorkflows.ts', import.meta.url))
const FIXTURES = new URL('./fixtures/agent_session_histories/', import.meta.url)
const STANDIN_QUEUE = 'durable-standin'
/** The one replica's session host queue (spec §5.2), which `acquire_worker` hands out. */
const HOST = sessionHostQueue('agent-session-test')
const PG_SKIP = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`
const OWNER = { kind: 'browser', id: 'browser', label: 'You' } as const

const message = (turnId: string, text = 'hello'): Message => ({ turn_id: turnId, text, author: OWNER, images: [], system_append: null })

function held(): { promise: Promise<void>; release: () => void } {
  let release!: () => void
  const promise = new Promise<void>((resolve) => {
    release = resolve
  })
  return { promise, release }
}

describe.skipIf(!TEMPORAL_CLI)(`AgentSession${TEMPORAL_SKIP}`, { timeout: 90_000 }, () => {
  let env: TestWorkflowEnvironment
  let bundle: WorkflowBundle
  let workers: Worker[] = []
  let running: Promise<void>[] = []
  let turns: SessionTurnInput[]
  let finished: FinishTurnInput[]
  let runTurn: (input: SessionTurnInput) => Promise<SessionTurnResult>
  let finish: (args: FinishTurnInput) => Promise<boolean>
  let settings: () => Promise<typeof RUNTIME_DEFAULTS>
  let settingsRead: number
  let acquired: number
  let released: string[]
  /** The gate's activities (stubs): what describe_call and resolve_input wait on, and what each was sent. */
  let describeHold: Promise<void>
  let resolveHold: Promise<void>
  let described: number
  let opened: OpenInput[]
  let resolved: ResolveInput[]
  /** The gate's real writers (gate/store.ts) where a test reads the rows; the stubs write nothing. */
  /** The sessions this test started: each one's workflow is terminated after it (#3716), so none runs into the next test. */
  let started: Set<string>
  let writes: { open: (args: OpenInput) => Promise<boolean>; resolve: (args: ResolveInput) => Promise<boolean> } | undefined

  beforeAll(async () => {
    env = await localTemporal()
    bundle = await bundleWorkflowCode({ workflowsPath: WORKFLOWS })
    const make = (taskQueue: string, activities: Record<string, (...args: never[]) => unknown>, workflows: Pick<Parameters<typeof Worker.create>[0], 'workflowBundle' | 'workflowsPath'>) =>
      Worker.create({
        connection: env.nativeConnection,
        taskQueue,
        activities,
        // A cancel reaches an activity by its heartbeat's answer.
        defaultHeartbeatThrottleInterval: '200ms',
        maxHeartbeatThrottleInterval: '200ms',
        ...workflows,
      })
    workers = [
      await make(SESSION_QUEUE, { [SESSION_SETTINGS_ACTIVITY]: async () => settings() }, { workflowBundle: bundle }),
      await make(
        TASK_QUEUE,
        {
          [ACQUIRE_WORKER_ACTIVITY]: async () => {
            acquired++
            return { hostQueue: HOST, identity: 'agent-session-test' }
          },
          [SESSION_FINISH_ACTIVITY]: async (args: FinishTurnInput) => finish(args),
          // The gate's (temporal/session/gateActivities.ts), as stubs: no rows.
          [GATE.settings]: async () => ({ approvalExpiryS: 600, questionExpiryS: 3600 }),
          [GATE.describe]: async () => {
            described++
            await describeHold
            return { summary: 'a call', input_hash: 'a'.repeat(64) }
          },
          [GATE.open]: async (args: OpenInput) => {
            opened.push(args)
            return writes ? writes.open(args) : true
          },
          [GATE.resolve]: async (args: ResolveInput) => {
            resolved.push(args)
            await resolveHold
            return writes ? writes.resolve(args) : true
          },
        },
        {},
      ),
      await make(
        HOST,
        {
          [SESSION_TURN_ACTIVITY]: async (input: SessionTurnInput) => {
            turns.push(input)
            return runTurn(input)
          },
          [RELEASE_WORKER_ACTIVITY]: async ({ sessionId }: { sessionId: string }) => {
            released.push(sessionId)
          },
        },
        {},
      ),
      await make(STANDIN_QUEUE, {}, { workflowsPath: STANDIN_WORKFLOWS }),
    ]
    running = workers.map((w) => w.run())
  }, 180_000)
  afterAll(async () => {
    workers.forEach((w) => w.shutdown())
    await Promise.all(running)
    await env?.teardown()
  })
  beforeEach(() => {
    turns = []
    finished = []
    settingsRead = 0
    acquired = 0
    released = []
    describeHold = Promise.resolve()
    resolveHold = Promise.resolve()
    described = 0
    opened = []
    resolved = []
    writes = undefined
    started = new Set()
    settings = async () => {
      settingsRead++
      return RUNTIME_DEFAULTS
    }
    runTurn = async () => ({ status: 'done', hostQueue: '' })
    finish = async (args) => {
      finished.push(args)
      return true
    }
  })

  /** Terminates every workflow of `ids` still open: a session a test left running (an idle one waiting for its next message, or one whose run continued as new) must not take the next test's activities. */
  async function terminateAll(ids: Iterable<string>): Promise<void> {
    for (const id of ids) {
      await env.client.workflow
        .getHandle(sessionWorkflowId(id))
        .terminate('the test ended')
        .catch((err: unknown) => {
          if (!(err instanceof WorkflowNotFoundError)) throw err
        })
    }
  }
  afterEach(async () => {
    await terminateAll(started)
  })

  const start = (id: string): SessionStart => ({ session_id: id, creator: OWNER, owner: OWNER, max_turns: 50, system_append: null })

  /** update-with-start of `send_message`, as DurableTurns sends the first message. */
  async function send(client: Client, id: string, m: Message) {
    started.add(id)
    const operation = new WithStartWorkflowOperation<(start: SessionStart) => Promise<void>>(SESSION_WORKFLOW, {
      workflowId: sessionWorkflowId(id),
      taskQueue: SESSION_QUEUE,
      args: [start(id)],
      workflowIdConflictPolicy: 'USE_EXISTING',
      workflowIdReusePolicy: 'REJECT_DUPLICATE',
    })
    return client.workflow.executeUpdateWithStart<(start: SessionStart) => Promise<void>, { accepted: boolean; turn_id: string }, [Message]>(SEND_MESSAGE_UPDATE, {
      args: [m],
      updateId: m.turn_id,
      startWorkflowOperation: operation,
    })
  }

  const handleOf = (id: string) => env.client.workflow.getHandle(sessionWorkflowId(id))
  const runIdOf = async (id: string) => (await handleOf(id).describe()).runId
  const poll = <T>(read: () => Promise<T> | T, expected: T) => expect.poll(async () => read(), { timeout: 20_000 }).toEqual(expected)

  it('starts the workflow with send_message and runs a turn for the message', async () => {
    const id = randomUUID()
    const answer = await send(env.client, id, message('t1', 'make a bracket'))
    expect(answer).toEqual({ accepted: true, turn_id: 't1' })
    await poll(() => finished.map((f) => f.outcome), ['done'])
    expect(turns).toEqual([{ sessionId: id, turnId: 't1', attempt: expect.stringMatching(/:0$/), message: message('t1', 'make a bracket') }])
    expect(finished[0]).toEqual({ sessionId: id, turnId: 't1', outcome: 'done' })
    expect(await handleOf(id).describe().then((d) => d.type)).toBe(SESSION_WORKFLOW)
    expect(acquired).toBe(1)
    await handleOf(id).signal('end')
    await handleOf(id).result()
    // End releases its place on the home it acquired (§5.2 "CompleteSession").
    expect(released).toEqual([id])
  })

  it('refuses a second send_message while a turn runs, `busy`, and holds the turn in its query', async () => {
    const id = randomUUID()
    const hold = held()
    runTurn = async () => {
      await hold.promise
      return { status: 'done', hostQueue: '' }
    }
    await send(env.client, id, message('t1'))
    await poll(() => turns.length, 1)
    expect(await handleOf(id).query(TURN_QUERY)).toBe('t1')
    const refused = await handleOf(id)
      .executeUpdate(SEND_MESSAGE_UPDATE, { args: [message('t2')] })
      .catch((err: unknown) => err)
    expect(refused).toBeInstanceOf(WorkflowUpdateFailedError)
    expect(((refused as WorkflowUpdateFailedError).cause as ApplicationFailure).type).toBe('busy')
    hold.release()
    await poll(() => finished.map((f) => f.turnId), ['t1'])
    expect(turns).toHaveLength(1)
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  it('end closes the workflow once no turn runs, and refuses a send after it as `ended`', async () => {
    const id = randomUUID()
    const hold = held()
    runTurn = async () => {
      await hold.promise
      return { status: 'done', hostQueue: '' }
    }
    await send(env.client, id, message('t1'))
    await poll(() => turns.length, 1)
    await handleOf(id).signal('end')
    // The turn is not cut short: it ends, is written, and then the workflow completes.
    hold.release()
    await handleOf(id).result()
    expect(finished.map((f) => f.outcome)).toEqual(['done'])
    expect((await handleOf(id).describe()).status.name).toBe('COMPLETED')
    // A workflow that has ended refuses a message, as a plain Update to a closed one.
    await expect(handleOf(id).executeUpdate(SEND_MESSAGE_UPDATE, { args: [message('t2')] })).rejects.toThrow()
  })

  it('end on an idle session closes it with no turn run', async () => {
    const id = randomUUID()
    await send(env.client, id, message('t1'))
    await poll(() => finished.length, 1)
    await handleOf(id).signal('end')
    await handleOf(id).result()
    expect(turns).toHaveLength(1)
  })

  it('continues as new after each turn: the run id changes, and the host queue is carried', async () => {
    const id = randomUUID()
    runTurn = async () => ({ status: 'done', hostQueue: HOST })
    await send(env.client, id, message('t1'))
    const first = await runIdOf(id)
    await poll(async () => (await runIdOf(id)) !== first, true)
    const second = await runIdOf(id)
    await send(env.client, id, message('t2'))
    await poll(() => finished.length, 2)
    await poll(async () => (await runIdOf(id)) !== second, true)
    const third = await runIdOf(id)
    expect(new Set([first, second, third]).size).toBe(3)
    expect(turns.map((t) => t.turnId)).toEqual(['t1', 't2'])
    // Acquired for the first turn only: the home is carried across continue-as-new.
    expect(acquired).toBe(1)
    // The carried start: no turn, no stop, and the queue the last turn ran on.
    const input = (await handleOf(id).describe()).raw.workflowExecutionInfo
    expect(input?.type?.name).toBe(SESSION_WORKFLOW)
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  it('ends an interrupted turn `interrupted`, cancelling the activity, and a failed one `failed` with its words', async () => {
    const id = randomUUID()
    runTurn = async () => {
      const context = Context.current()
      const beat = setInterval(() => context.heartbeat(), 200)
      try {
        return await context.cancelled
      } finally {
        clearInterval(beat)
      }
    }
    await send(env.client, id, message('t1'))
    await poll(() => turns.length, 1)
    await handleOf(id).signal('interrupt', { reason: 'stop' })
    await poll(() => finished.map((f) => f.outcome), ['interrupted'])
    runTurn = async () => ({ status: 'failed', hostQueue: '', error: 'the engine died' })
    await poll(async () => handleOf(id).query(TURN_QUERY), null)
    await send(env.client, id, message('t2'))
    await poll(() => finished.map((f) => f.outcome), ['interrupted', 'failed'])
    expect(finished[1]).toEqual({ sessionId: id, turnId: 't2', outcome: 'failed', message: 'the engine died' })
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  it('spends the message of a turn stopped before it ran: it is never run, and the session goes on idle', async () => {
    const id = randomUUID()
    const slow = held()
    settings = async () => {
      settingsRead++
      await slow.promise
      return RUNTIME_DEFAULTS
    }
    await send(env.client, id, message('t1', 'never runs'))
    await poll(() => settingsRead, 1)
    const first = await runIdOf(id)
    await handleOf(id).signal('interrupt', { reason: 'stop' })
    // A local activity holds its workflow task; past 80% of the task's 10 s the SDK hands the
    // task back, and the next task, which carries the Stop, starts while the settings are still
    // being read. Its start in the history, after the signal's event, is the condition to release
    // on: the activation applies the signal before the settings' result.
    await expect
      .poll(
        async () => {
          const types = (JSON.parse(historyToJSON(await handleOf(id).fetchHistory())) as { events: { eventType: string }[] }).events.map((e) => e.eventType)
          const signaled = types.lastIndexOf('EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED')
          return signaled >= 0 && types.slice(signaled).includes('EVENT_TYPE_WORKFLOW_TASK_STARTED')
        },
        { timeout: 20_000, interval: 250 },
      )
      .toBe(true)
    slow.release()
    await poll(() => finished.map((f) => f.outcome), ['interrupted'])
    expect(turns).toEqual([])
    await poll(async () => (await runIdOf(id)) !== first, true)
    expect(await handleOf(id).query(TURN_QUERY)).toBeNull()
    // Idle, not running the stopped message: the next send is the next turn.
    settings = async () => RUNTIME_DEFAULTS
    await send(env.client, id, message('t2', 'runs'))
    await poll(() => finished.map((f) => f.turnId), ['t1', 't2'])
    expect(turns.map((t) => t.message?.text)).toEqual(['runs'])
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  it('ends a turn whose activity raises BudgetExhausted `budget_exhausted`', async () => {
    const id = randomUUID()
    runTurn = async () => {
      throw ApplicationFailure.nonRetryable('spent', 'BudgetExhausted')
    }
    await send(env.client, id, message('t1'))
    await poll(() => finished.map((f) => f.outcome), ['budget_exhausted'])
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  /** Writes one run's history as the fixture `name` (UPDATE_SESSION_HISTORIES=1), which `replays its recorded histories` replays. */
  async function record(name: string, id: string, runId: string): Promise<void> {
    if (!process.env.UPDATE_SESSION_HISTORIES) return
    await writeFile(new URL(`${name}.json`, FIXTURES), `${historyToJSON(await env.client.workflow.getHandle(sessionWorkflowId(id), runId).fetchHistory())}\n`)
  }

  /** Whether the run's history holds session_turn's completion. */
  async function turnCompleted(id: string, runId: string): Promise<boolean> {
    type Event = {
      eventId: string
      eventType: string
      activityTaskScheduledEventAttributes?: { activityType?: { name?: string } }
      activityTaskCompletedEventAttributes?: { scheduledEventId?: string }
    }
    const events = (JSON.parse(historyToJSON(await env.client.workflow.getHandle(sessionWorkflowId(id), runId).fetchHistory())) as { events: Event[] }).events
    const turnIds = new Set(events.filter((e) => e.activityTaskScheduledEventAttributes?.activityType?.name === SESSION_TURN_ACTIVITY).map((e) => String(e.eventId)))
    return events.some((e) => turnIds.has(String(e.activityTaskCompletedEventAttributes?.scheduledEventId)))
  }

  /** The history's event types of one run. */
  const eventTypes = async (id: string, runId?: string) =>
    (JSON.parse(historyToJSON(await env.client.workflow.getHandle(sessionWorkflowId(id), runId).fetchHistory())) as { events: { eventType: string }[] }).events.map((e) => e.eventType)

  /**
   * A turn whose call is still being described when the turn ends (its describe_call held), so
   * `open_input` is the handler the workflow waits on after the turn's end. Once released, the
   * call is settled `cancelled` with no entry (the turn is over) and the handler finishes.
   * Resolves once the turn's end is written, the handler still in describe_call.
   */
  async function lateOpen(id: string): Promise<{ turnRun: string; releaseDescribe: () => void }> {
    const describing = held()
    describeHold = describing.promise
    runTurn = async (input) => {
      // Accepted while the turn runs: its validator passes; the handler then waits on describe_call.
      await env.client.workflow.getHandle(sessionWorkflowId(input.sessionId)).startUpdate(OPEN_INPUT_UPDATE, {
        args: [{ turn_id: input.turnId, call: { id: 'toolu_late', name: 'print_output', input: { output_id: 'o1' } } } satisfies OpenInputArgs],
        waitForStage: WorkflowUpdateStage.ACCEPTED,
      })
      return { status: 'done', hostQueue: '' }
    }
    await send(env.client, id, message('t1'))
    const turnRun = await runIdOf(id)
    await poll(() => finished.map((f) => f.turnId), ['t1'])
    await poll(() => described, 1)
    runTurn = async () => ({ status: 'done', hostQueue: '' })
    return {
      turnRun,
      releaseDescribe: () => {
        describing.release()
      },
    }
  }

  /** The run that is running now, once it has completed or continued as new: what the session did after its handlers finished. */
  async function afterHandlers(id: string, turnRun: string) {
    await expect
      .poll(async () => {
        const d = await handleOf(id).describe()
        return d.runId !== turnRun || d.status.name !== 'RUNNING'
      }, { timeout: 20_000 })
      .toBe(true)
    const d = await handleOf(id).describe()
    return { runId: d.runId, status: d.status.name }
  }

  it('an `end` while a handler finishes after the turn completes the session there: no new run, no new turn', async () => {
    const id = randomUUID()
    const { turnRun, releaseDescribe } = await lateOpen(id)
    await handleOf(id).signal('end')
    // In the turn's run's history before the release: a signal that lands while a workflow task is
    // in flight is buffered by the server and written only once that task completes.
    await poll(async () => (await eventTypes(id, turnRun)).includes('EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED'), true)
    releaseDescribe()
    expect(await afterHandlers(id, turnRun)).toEqual({ runId: turnRun, status: 'COMPLETED' })
    expect(await eventTypes(id, turnRun)).not.toContain('EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW')
    expect(turns.map((t) => t.turnId)).toEqual(['t1'])
    expect(finished.map((f) => f.turnId)).toEqual(['t1'])
    expect(released).toEqual([id])
    // The late call wrote nothing: no entry was opened, none resolved.
    expect(opened).toEqual([])
    expect(resolved).toEqual([])
    await record('end_while_finishing', id, turnRun)
  })

  it('a message taken before that `end` still runs first, in the same run', async () => {
    const id = randomUUID()
    const { turnRun, releaseDescribe } = await lateOpen(id)
    expect(await handleOf(id).executeUpdate(SEND_MESSAGE_UPDATE, { args: [message('t2', 'then this')] })).toEqual({ accepted: true, turn_id: 't2' })
    await handleOf(id).signal('end')
    releaseDescribe()
    expect(await afterHandlers(id, turnRun)).toEqual({ runId: turnRun, status: 'COMPLETED' })
    expect(turns.map((t) => t.message?.text)).toEqual(['hello', 'then this'])
    expect(finished.map((f) => f.turnId)).toEqual(['t1', 't2'])
    expect(await eventTypes(id, turnRun)).not.toContain('EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW')
  })

  it('takes a message sent once the turn`s end is written, before the workflow closes the turn, and runs it next', async () => {
    const id = randomUUID()
    const closing = held()
    finish = async (args) => {
      finished.push(args)
      // The row is idle from here (session.finish_turn has written it); the workflow has not closed the turn.
      if (args.turnId === 't1') await closing.promise
      return true
    }
    await send(env.client, id, message('t1'))
    await poll(() => finished.map((f) => f.turnId), ['t1'])
    expect(await handleOf(id).executeUpdate(SEND_MESSAGE_UPDATE, { args: [message('t2', 'right after')] })).toEqual({ accepted: true, turn_id: 't2' })
    closing.release()
    await poll(() => finished.map((f) => f.turnId), ['t1', 't2'])
    expect(turns.map((t) => t.message?.text)).toEqual(['hello', 'right after'])
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  it('a Stop while the turn closes is for the message taken meanwhile: it never runs, and the session goes on idle', async () => {
    const id = randomUUID()
    const closing = held()
    finish = async (args) => {
      finished.push(args)
      if (args.turnId === 't1') await closing.promise
      return true
    }
    await send(env.client, id, message('t1'))
    await poll(() => finished.map((f) => f.turnId), ['t1'])
    await handleOf(id).executeUpdate(SEND_MESSAGE_UPDATE, { args: [message('t2', 'stopped')] })
    await handleOf(id).signal('interrupt', { reason: 'stop' })
    closing.release()
    await poll(() => finished.map((f) => `${f.turnId} ${f.outcome}`), ['t1 done', 't2 interrupted'])
    expect(turns.map((t) => t.turnId)).toEqual(['t1'])
    await poll(async () => handleOf(id).query(TURN_QUERY), null)
    await send(env.client, id, message('t3', 'runs'))
    await poll(() => finished.map((f) => f.turnId), ['t1', 't2', 't3'])
    expect(turns.map((t) => t.message?.text)).toEqual(['hello', 'runs'])
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  it('two calls of one turn park side by side, each resolved on its own, and the turn`s close waits for both', async () => {
    const id = randomUUID()
    const turnHold = held()
    const resolving = held()
    let rids: string[] = []
    runTurn = async (input) => {
      const handle = env.client.workflow.getHandle(sessionWorkflowId(input.sessionId))
      const open = (call: string) =>
        handle.executeUpdate<{ request_id: string }, [OpenInputArgs]>(OPEN_INPUT_UPDATE, {
          args: [{ turn_id: input.turnId, call: { id: call, name: 'print_output', input: { output_id: call } } }],
        })
      // Both at once: neither waits on the other.
      rids = (await Promise.all([open('toolu_a'), open('toolu_b')])).map((o) => o.request_id)
      await turnHold.promise
      return { status: 'done', hostQueue: '' }
    }
    await send(env.client, id, message('t1'))
    const turnRun = await runIdOf(id)
    await poll(() => rids.length, 2)
    const listed = await handleOf(id).query<{ id: string }[]>(PENDING_INPUT_QUERY)
    expect(listed.map((e) => e.id).sort()).toEqual([...rids].sort())
    resolveHold = resolving.promise
    const respond = (rid: string, decision: 'approve' | 'deny') =>
      handleOf(id).executeUpdate(RESPOND_UPDATE, { args: [{ request_id: rid, response: { kind: 'approval', decision }, responder: OWNER, role: 'browser' }] })
    const answers = Promise.all([respond(rids[0]!, 'approve'), respond(rids[1]!, 'deny')])
    // Both in resolve_input at once, each for its own entry.
    await poll(() => resolved.map((r) => `${r.requestId} ${r.outcome}`).sort(), [`${rids[0]} approved`, `${rids[1]} denied`].sort())
    turnHold.release()
    // The turn's activity has ended, and its close waits on both resolutions before its end is written.
    await poll(() => turnCompleted(id, turnRun), true)
    expect(finished).toEqual([])
    resolving.release()
    expect(await answers).toEqual([
      { kind: 'approval', outcome: 'approved' },
      { kind: 'approval', outcome: 'denied' },
    ])
    await poll(() => finished.map((f) => f.turnId), ['t1'])
    // Nothing was left to cancel.
    expect(resolved).toHaveLength(2)
    await record('park_respond', id, turnRun)
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  it('refuses open_input for a turn that is not running, `stale`', async () => {
    const id = randomUUID()
    await send(env.client, id, message('t1'))
    await poll(() => finished.length, 1)
    const refused = await handleOf(id)
      .executeUpdate(OPEN_INPUT_UPDATE, { args: [{ turn_id: 't1', call: { id: 'toolu_x', name: 'print_output', input: {} } } satisfies OpenInputArgs] })
      .catch((err: unknown) => err)
    expect(refused).toBeInstanceOf(WorkflowUpdateFailedError)
    expect(((refused as WorkflowUpdateFailedError).cause as ApplicationFailure).type).toBe('stale')
    expect(described).toBe(0)
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  it('replays its recorded histories', async () => {
    const id = randomUUID()
    const hold = held()
    runTurn = async () => {
      await hold.promise
      return { status: 'done', hostQueue: '' }
    }
    await send(env.client, id, message('t1'))
    const turnRun = await runIdOf(id)
    await poll(() => turns.length, 1)
    hold.release()
    await poll(async () => (await runIdOf(id)) !== turnRun, true)
    await handleOf(id).signal('end')
    const endRun = await runIdOf(id)
    await handleOf(id).result()
    const recorded = {
      turn: await env.client.workflow.getHandle(sessionWorkflowId(id), turnRun).fetchHistory(),
      ended: await env.client.workflow.getHandle(sessionWorkflowId(id), endRun).fetchHistory(),
    }
    if (process.env.UPDATE_SESSION_HISTORIES) {
      for (const [name, history] of Object.entries(recorded)) await writeFile(new URL(`${name}.json`, FIXTURES), `${historyToJSON(history)}\n`)
    }
    const names = (await readdir(FIXTURES)).filter((f) => f.endsWith('.json')).map((f) => f.slice(0, -'.json'.length))
    expect(names).toEqual(expect.arrayContaining(['turn', 'ended', 'park_respond', 'end_while_finishing']))
    const fixtures = await Promise.all(
      names.map(async (name) => ({
        workflowId: `fixture-${name}`,
        history: historyFromJSON(JSON.parse(await readFile(new URL(`${name}.json`, FIXTURES), 'utf8'))),
      })),
    )
    for await (const { error } of Worker.runReplayHistories({ workflowsPath: WORKFLOWS }, [...fixtures, { workflowId: sessionWorkflowId(id), history: recorded.turn }])) {
      expect(error).toBeUndefined()
    }
  })

  describe.skipIf(!TEST_DATABASE_URL)(`routed by DurableTurns${PG_SKIP}`, () => {
    let db: Database
    let drop: () => Promise<void>

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
    })
    afterEach(async () => {
      // The sessions the managers started (#3716): their workflows end with the test.
      await terminateAll((await db.sql<{ id: string }[]>`SELECT id FROM ai_sessions`).map((r) => r.id))
      await drop()
    })

    const flag = async (value: boolean | undefined) => {
      await db.sql`DELETE FROM ai_settings WHERE key = ${UNIFIED_SESSIONS_SETTING}`
      // Set in the database, never by a route: no route or tool writes this key (unifiedSessionsFlag.pg.test.ts).
      if (value) await db.sql`INSERT INTO ai_settings (key, value) VALUES (${UNIFIED_SESSIONS_SETTING}, ${db.sql.json(value)})`
    }

    async function durableManager(): Promise<SessionManager> {
      const m = manager({
        sql: db.sql,
        paths: await tempPaths(),
        settings: { get: <T>(key: string) => Promise.resolve((key === SETTING_SESSION_MODE ? 'durable' : undefined) as T) },
        run: scriptedRunner(() => ({ throws: 'a durable turn ran in the classic harness' })).runner,
      })
      m.durableTurns = new DurableTurns({ client: env.client, sql: db.sql, events: m.events, taskQueue: STANDIN_QUEUE, sessionQueue: SESSION_QUEUE, sessionHostQueue: HOST })
      return m
    }

    const runtimeOf = async (id: string) => (await db.sql<{ runtime: string | null }[]>`SELECT runtime FROM ai_sessions WHERE id = ${id}`)[0]?.runtime
    const describe_ = async (id: string) => {
      const d = await handleOf(id).describe()
      return { type: d.type, taskQueue: d.taskQueue }
    }

    it('starts a new session as AgentSession on agent-sessions with the flag set, and reads each turn`s end from the event log', async () => {
      await flag(true)
      const m = await durableManager()
      finish = sessionFinishActivities(db.sql, m.events)[SESSION_FINISH_ACTIVITY]
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make me a bracket', context: '[page: /]' })
      expect(session.mode).toBe('durable')
      expect(await describe_(session.id)).toEqual({ type: SESSION_WORKFLOW, taskQueue: SESSION_QUEUE })
      expect(await runtimeOf(session.id)).toBe('unified')
      expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
      expect(turns[0]).toMatchObject({ sessionId: session.id, message: { text: 'make me a bracket\n\n[page: /]', author: browser } })
      const [row] = await db.sql<{ status: string; durable_turn_id: string | null }[]>`SELECT status, durable_turn_id FROM ai_sessions WHERE id = ${session.id}`
      expect(row).toEqual({ status: 'idle', durable_turn_id: null })
      const log = (await m.events.read(session.id)).map((e) => e.event.type)
      expect(log.slice(-2)).toEqual(['session.result', 'session.status'])
      // A second turn goes to the same session, on its next run.
      const second = await m.send(session.id, browser, 'and a lid')
      expect(await second.done).toMatchObject({ kind: 'result' })
      expect(turns.map((t) => t.message?.text)).toEqual(['make me a bracket\n\n[page: /]', 'and a lid'])
    })

    it('keeps a session that started as DurableSession on the Python type and queue after the flag is set', async () => {
      await flag(undefined)
      const m = await durableManager()
      const { session } = await m.start(browser, { origin: 'chat', prompt: 'one' })
      expect(await describe_(session.id)).toEqual({ type: 'DurableSession', taskQueue: STANDIN_QUEUE })
      expect(await runtimeOf(session.id)).toBe('durable')
      await db.sql`UPDATE ai_sessions SET status = 'idle' WHERE id = ${session.id}`
      await handleOf(session.id).signal('end_turn')
      await flag(true)
      await m.send(session.id, browser, 'two')
      expect(await describe_(session.id)).toEqual({ type: 'DurableSession', taskQueue: STANDIN_QUEUE })
      expect(await runtimeOf(session.id)).toBe('durable')
      const { messages } = await handleOf(session.id).query<{ messages: { text: string }[] }>('recorded')
      expect(messages.map((x) => x.text)).toEqual(['one', 'two'])
      expect(turns).toEqual([])
      // And a session first sent to now is the new type.
      const fresh = await m.start(browser, { origin: 'chat', prompt: 'three' })
      expect(await describe_(fresh.session.id)).toEqual({ type: SESSION_WORKFLOW, taskQueue: SESSION_QUEUE })
    })

    it('keeps a session that existed before the migration, with offset 0 and a Python workflow, DurableSession with the flag set', async () => {
      await flag(true)
      const m = await durableManager()
      const { session } = await m.start(browser, { origin: 'chat', title: 't' })
      // As a row from before the column: no runtime, nothing output yet, its workflow running.
      await db.sql`UPDATE ai_sessions SET runtime = NULL, durable_offset = 0 WHERE id = ${session.id}`
      await env.client.workflow.start('DurableSession', { workflowId: sessionWorkflowId(session.id), taskQueue: STANDIN_QUEUE, args: [{}] })
      const migration = await readFile(new URL('../src/db/migrations/20261014T0837Z_session_runtime.sql', import.meta.url), 'utf8')
      for (const statement of migration.replace(/^--.*$/gm, '').split(';').map((x) => x.trim()).filter((x) => x.startsWith('UPDATE'))) {
        await db.sql.unsafe(statement)
      }
      expect(await runtimeOf(session.id)).toBe('durable')
      await m.send(session.id, browser, 'hello')
      expect(await describe_(session.id)).toEqual({ type: 'DurableSession', taskQueue: STANDIN_QUEUE })
      expect(await runtimeOf(session.id)).toBe('durable')
      expect(turns).toEqual([])
    })

    it('a call whose turn ended while it was described writes nothing: the session stays idle and takes the next send', async () => {
      await flag(true)
      const m = await durableManager()
      finish = sessionFinishActivities(db.sql, m.events)[SESSION_FINISH_ACTIVITY]
      // The real store writes (gate/store.ts), so the row is what a person's panel and the next send see.
      writes = { open: (args) => openInput(db.sql, args), resolve: (args) => resolveInput(db.sql, args) }
      const describing = held()
      describeHold = describing.promise
      runTurn = async (input) => {
        // Accepted while the turn runs; its handler then waits on describe_call past the turn's end.
        await env.client.workflow.getHandle(sessionWorkflowId(input.sessionId)).startUpdate(OPEN_INPUT_UPDATE, {
          args: [{ turn_id: input.turnId, call: { id: 'toolu_late', name: 'print_output', input: { output_id: 'o1' } } } satisfies OpenInputArgs],
          waitForStage: WorkflowUpdateStage.ACCEPTED,
        })
        return { status: 'done', hostQueue: '' }
      }
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'print it' })
      const turnRun = await runIdOf(session.id)
      expect(await turn!.done).toMatchObject({ kind: 'result' })
      await poll(() => described, 1)
      const statusOf = async () => (await db.sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${session.id}`)[0]?.status
      // session.finish_turn has written the turn's end.
      expect(await statusOf()).toBe('idle')
      describing.release()
      // The handler has finished once the run continues as new.
      await poll(async () => (await runIdOf(session.id)) !== turnRun, true)
      expect(await statusOf()).toBe('idle')
      expect(opened).toEqual([])
      expect(await db.sql`SELECT 1 FROM ai_pending_input WHERE session_id = ${session.id}`).toHaveLength(0)
      runTurn = async () => ({ status: 'done', hostQueue: '' })
      const next = await m.send(session.id, browser, 'and again')
      expect(await next.done).toMatchObject({ kind: 'result' })
    })

    it('refuses to fork a unified session, whatever its state, writing nothing', async () => {
      await flag(true)
      const m = await durableManager()
      finish = sessionFinishActivities(db.sql, m.events)[SESSION_FINISH_ACTIVITY]
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
      await turn!.done
      const before = await db.sql`SELECT id FROM ai_sessions`
      await expect(m.fork(session.id, browser)).rejects.toMatchObject({ code: 'invalid', message: expect.stringMatching(/unified runtime.*cannot be forked yet/) })
      expect(await db.sql`SELECT id FROM ai_sessions`).toHaveLength(before.length)
    })
  })
})
