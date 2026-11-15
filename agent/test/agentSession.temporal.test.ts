import { type Client, WithStartWorkflowOperation, WorkflowNotFoundError, WorkflowUpdateFailedError, WorkflowUpdateStage } from '@temporalio/client'
import { Context } from '@temporalio/activity'
import { ApplicationFailure } from '@temporalio/common'
import { historyFromJSON, historyToJSON } from '@temporalio/common/proto-utils'
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
import { type SessionManager } from '../src/sessions/manager.js'
import { RUNTIME_DEFAULTS } from '../src/sessions/runtimeSettings.js'
import { TASK_QUEUE } from '../src/temporal/names.js'
import { sessionFinishActivities } from '../src/temporal/session/finishActivity.js'
import type {
  BeginOutcome,
  BeginTurnInput,
  FinishTurnInput,
  Message,
  OpenInputArgs,
  QueueAnswer,
  QueueChangedInput,
  QueuedMessage,
  QueueState,
  SessionStart,
  SessionTurnInput,
  SessionTurnResult,
} from '../src/temporal/session/models.js'
import {
  ACQUIRE_WORKER_ACTIVITY,
  EDIT_QUEUED_UPDATE,
  NO_CAPACITY,
  QUEUE_MESSAGE_UPDATE,
  QUEUE_QUERY,
  REMOVE_QUEUED_UPDATE,
  SESSION_BEGIN_TURN_ACTIVITY,
  SESSION_QUEUE_CHANGED_ACTIVITY,
  OPEN_INPUT_UPDATE,
  TAB_RECONNECTED_UPDATE,
  OWNER_CHANGED_SIGNAL,
  PLACEMENT_MOVED,
  SESSION_GATE_ACTIVITIES as GATE,
  SESSION_GONE,
  RELEASE_WORKER_ACTIVITY,
  SEND_MESSAGE_UPDATE,
  SESSION_FINISH_ACTIVITY,
  SESSION_QUEUE,
  SESSION_SETTINGS_ACTIVITY,
  SESSION_TURN_ACTIVITY,
  SESSION_WORKFLOW,
  sessionHostQueue,
  TURN_QUERY,
} from '../src/temporal/session/names.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP, polledHosts } from './support/temporal.js'

// AgentSession (unified runtime spec 2026-10-13 §5.1, plan Task 5.1) on a Temporal dev
// server: send, busy, end, continue-as-new after each turn and the replay of recorded
// histories; then DurableTurns routing a session by its recorded runtime. `acquire_worker`,
// `session_turn` and `release_worker` are stubs on one replica's queues (the real ones are
// test/sessionAffinity.temporal.test.ts's) and `session.finish_turn` records what it was
// sent, or is the real activity where a test reads the event log.

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))
const FIXTURES = new URL('./fixtures/agent_session_histories/', import.meta.url)
/** The one replica's session host queue (spec §5.2), which `acquire_worker` hands out. */
const HOST = sessionHostQueue('agent-session-test')
const PG_SKIP = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`
const OWNER = { kind: 'browser', id: 'browser', label: 'You' } as const

const message = (turnId: string, text = 'hello'): Message => ({ turn_id: turnId, text, author: OWNER, images: [], system_append: null })
/** A message sent while a turn runs (#6386), as DurableTurns queues it. */
const queued = (turnId: string, words = turnId, extra: Partial<QueuedMessage> = {}): QueuedMessage => ({
  ...message(turnId, extra.context ? `${words}\n\n${extra.context}` : words),
  words,
  queued_at: '2026-11-15T00:00:00.000Z',
  ...extra,
})

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
  let finish: (args: FinishTurnInput) => Promise<unknown>
  /** The queue's activities (#6386): what each was sent, and how a begin answers. */
  let begin: (args: BeginTurnInput) => Promise<BeginOutcome>
  let begun: string[]
  let queueChanged: (args: QueueChangedInput) => Promise<boolean>
  let queueWrites: QueueChangedInput[]
  let settings: () => Promise<typeof RUNTIME_DEFAULTS>
  let settingsRead: number
  let acquired: number
  /** Each acquire_worker's `exclude`: the homes the turn was lost on. */
  let excluded: string[][]
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
    // A deleted run is gone at once, as retention leaves it (#5243).
    env = await localTemporal({ deleteAtOnce: true })
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
          [ACQUIRE_WORKER_ACTIVITY]: async (input: { exclude?: string[] }) => {
            acquired++
            excluded.push(input.exclude ?? [])
            return { hostQueue: HOST, identity: 'agent-session-test' }
          },
          [SESSION_FINISH_ACTIVITY]: async (args: FinishTurnInput) => finish(args),
          [SESSION_BEGIN_TURN_ACTIVITY]: async (args: BeginTurnInput) => {
            begun.push(args.next.turn_id)
            return begin(args)
          },
          [SESSION_QUEUE_CHANGED_ACTIVITY]: async (args: QueueChangedInput) => {
            queueWrites.push(args)
            return queueChanged(args)
          },
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
    excluded = []
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
    // As session.finish_turn answers: with the queue's head, whether it started (#6386).
    finish = async (args) => {
      finished.push(args)
      return args.next ? { written: true, next: 'started' } : true
    }
    begun = []
    begin = async () => 'started'
    queueWrites = []
    queueChanged = async () => true
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
    expect(turns).toEqual([{ sessionId: id, turnId: 't1', attempt: expect.stringMatching(/:0$/), message: message('t1', 'make a bracket'), parkHoldSeconds: 300 }])
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

  it('a handoff tells the gate its new owner: a grant from that owner may no longer approve the session`s own calls, in this run or the next', async () => {
    const id = randomUUID()
    const NEXT = { kind: 'bearer', id: 'token:next', label: 'Next agent' } as const
    let turnHold = held()
    let rid = ''
    runTurn = async (input) => {
      const handle = env.client.workflow.getHandle(sessionWorkflowId(input.sessionId))
      rid = (
        await handle.executeUpdate<{ request_id: string }, [OpenInputArgs]>(OPEN_INPUT_UPDATE, {
          args: [{ turn_id: input.turnId, call: { id: `toolu_${input.turnId}`, name: 'print_output', input: { output_id: input.turnId } } }],
        })
      ).request_id
      await turnHold.promise
      return { status: 'done', hostQueue: '' }
    }
    const grant = (requestId: string) =>
      handleOf(id)
        .executeUpdate(RESPOND_UPDATE, { args: [{ request_id: requestId, response: { kind: 'approval', decision: 'approve' }, responder: NEXT, role: 'grant' }] })
        .then(
          () => 'approved',
          (err: unknown) => (err instanceof WorkflowUpdateFailedError ? String((err.cause as ApplicationFailure | undefined)?.message) : String(err)),
        )
    // Before: NEXT is nobody in this session, so its grant may approve the call.
    await send(env.client, id, message('t1'))
    await poll(() => rid.endsWith('toolu_t1'), true)
    expect(await grant(rid)).toBe('approved')
    // The handoff: NEXT owns the session now, and a grant is never for its own session's calls.
    await handleOf(id).signal(OWNER_CHANGED_SIGNAL, { owner: NEXT })
    turnHold.release()
    await poll(() => finished.map((f) => f.turnId), ['t1'])
    // The next run starts with the new owner (continue-as-new carries it).
    turnHold = held()
    await handleOf(id).executeUpdate(SEND_MESSAGE_UPDATE, { args: [message('t2')] })
    await poll(() => rid.endsWith('toolu_t2'), true)
    expect(await grant(rid)).toMatch(/never your own/)
    turnHold.release()
    await poll(() => finished.map((f) => f.turnId), ['t1', 't2'])
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  it('a kept home full as the turn arrives (NoCapacity) moves the turn at no cost to its placements, once; a home acquired for the turn costs one (#3443)', async () => {
    const id = randomUUID()
    const full = () => ApplicationFailure.retryable('every warm session on the replica is running a turn', NO_CAPACITY)
    // t1: every home refuses, each acquired for it, so each refusal costs one of the three placements.
    // t2: runs on the home it acquires, which t3 and t4 keep.
    // t3: the kept home refuses once, and the move to the next home runs it.
    // t4: the kept home refuses, then every home after: one free move, then the three placements.
    let t3Refused = false
    runTurn = async (input) => {
      if (input.turnId === 't2') return { status: 'done', hostQueue: HOST }
      if (input.turnId === 't3' && t3Refused) return { status: 'done', hostQueue: HOST }
      if (input.turnId === 't3') t3Refused = true
      throw full()
    }
    const placed = (turnId: string) => turns.filter((t) => t.turnId === turnId).map((t) => [t.attempt?.split(':')[1], t.continuation?.kind])
    const next = async (turnId: string, outcomes: string[]) => {
      await poll(async () => handleOf(id).query(TURN_QUERY), null)
      await send(env.client, id, message(turnId))
      const run = await runIdOf(id)
      await poll(() => finished.map((f) => f.outcome), outcomes)
      return run
    }
    await send(env.client, id, message('t1'))
    await poll(() => finished.map((f) => f.outcome), ['failed'])
    expect(finished[0]?.message).toContain('could not be placed on a worker')
    expect(placed('t1')).toEqual([['0', undefined], ['1', 'restarted'], ['2', 'restarted']])
    expect(acquired).toBe(3)
    await next('t2', ['failed', 'done'])
    expect(acquired).toBe(4)
    await next('t3', ['failed', 'done', 'done'])
    // Re-acquired after the kept home refused, the turn restarted from its transcript (nothing ran there).
    expect(placed('t3')).toEqual([['0', undefined], ['1', 'restarted']])
    expect(acquired).toBe(5)
    const t4Run = await next('t4', ['failed', 'done', 'done', 'failed'])
    // Four placements, never a loop: the kept home's refusal, then three on homes acquired for it.
    expect(placed('t4')).toEqual([['0', undefined], ['1', 'restarted'], ['2', 'restarted'], ['3', 'restarted']])
    expect(acquired).toBe(8)
    expect(finished[3]?.message).toContain('could not be placed on a worker')
    await poll(async () => handleOf(id).query(TURN_QUERY), null)
    await record('full_home', id, t4Run)
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  it('a placement cut off because another took the turn (PlacementMoved) is placed again, restarted, at most three times; the words then say why, never "Activity task failed" (#6387)', async () => {
    const id = randomUUID()
    const moved = (input: SessionTurnInput) =>
      ApplicationFailure.retryable(`session ${input.sessionId}'s placement ${input.attempt} was cut off: the turn was placed on another worker`, PLACEMENT_MOVED)
    // t1: its first placement is cut off, the next one runs it. t2: every placement is cut off.
    let t1Moved = false
    runTurn = async (input) => {
      if (input.turnId === 't1' && t1Moved) return { status: 'done', hostQueue: HOST }
      if (input.turnId === 't1') t1Moved = true
      throw moved(input)
    }
    const placed = (turnId: string) => turns.filter((t) => t.turnId === turnId).map((t) => [t.attempt?.split(':')[1], t.continuation?.kind])
    await send(env.client, id, message('t1'))
    const t1Run = await runIdOf(id)
    await poll(() => finished.map((f) => f.outcome), ['done'])
    expect(placed('t1')).toEqual([['0', undefined], ['1', 'restarted']])
    await poll(async () => handleOf(id).query(TURN_QUERY), null)
    await record('placement_moved', id, t1Run)
    await send(env.client, id, message('t2'))
    await poll(() => finished.map((f) => f.outcome), ['done', 'failed'])
    expect(placed('t2')).toEqual([['0', undefined], ['1', 'restarted'], ['2', 'restarted']])
    // A home cut off by another placement is alive: no acquire steers the turn away from it. Over
    // both turns on purpose: t1's second acquire is the one that would name HOST.
    expect(excluded.flat()).toEqual([])
    expect(finished[1]?.message).toBe('the turn could not be placed on a worker: another placement took the turn over (3 tries); send the message again')
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  it('a placement whose turn ended (SessionGone) is never placed again: the turn fails with the cause, never "Activity task failed" (#6387)', async () => {
    const id = randomUUID()
    runTurn = async (input) => {
      throw ApplicationFailure.nonRetryable(`session ${input.sessionId}'s placement ${input.attempt} was cut off: the turn ended without this placement`, SESSION_GONE)
    }
    await send(env.client, id, message('t1'))
    await poll(() => finished.map((f) => f.outcome), ['failed'])
    expect(turns).toHaveLength(1)
    expect(finished[0]?.message).toBe(`session ${id}'s placement ${turns[0]!.attempt} was cut off: the turn ended without this placement`)
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

  it('an attention request may name on_timeout stop, and `tab_reconnected` ends only the tab waits, as the system', async () => {
    const id = randomUUID()
    const turnHold = held()
    let opens: { request_id: string; resolution?: unknown }[] = []
    runTurn = async (input) => {
      const handle = env.client.workflow.getHandle(sessionWorkflowId(input.sessionId))
      const open = (call: string, wait: Record<string, unknown>) =>
        handle.executeUpdate<{ request_id: string; resolution?: unknown }, [OpenInputArgs]>(OPEN_INPUT_UPDATE, {
          args: [{ turn_id: input.turnId, call: { id: call, name: 'wait_for_user', input: { message: 'Need you', ...wait } } }],
        })
      // Each as TurnHost opens it (#5003): the model's own request, and a browser call's tab wait.
      opens = [await open('toolu_a', { reason: 'blocked', timeout_s: 600, on_timeout: 'stop' }), await open('tab:toolu_b', { reason: 'tab_disconnected', timeout_s: 300 })]
      await turnHold.promise
      return { status: 'done', hostQueue: '' }
    }
    await send(env.client, id, message('t1'))
    const turnRun = await runIdOf(id)
    await poll(() => opens.length, 2)
    // Both parked: `stop` is an AgentSession's to name (gate/entry.ts TURN_ON_TIMEOUT).
    expect(opens.map((o) => o.resolution)).toEqual([undefined, undefined])
    expect(opened.map((o) => o.attention)).toEqual([
      { reason: 'blocked', on_timeout: 'stop' },
      { reason: 'tab_disconnected', on_timeout: 'proceed' },
    ])
    expect(await handleOf(id).executeUpdate<number, [Record<string, never>]>(TAB_RECONNECTED_UPDATE, { args: [{}] })).toBe(1)
    expect(resolved).toEqual([
      expect.objectContaining({ requestId: opens[1]!.request_id, outcome: 'cancelled', responder: expect.objectContaining({ kind: 'system' }), response: { reconnected: true } }),
    ])
    // The other is still the user's to answer; once both have ended, a second tab_reconnected ends nothing.
    expect((await handleOf(id).query<{ id: string }[]>(PENDING_INPUT_QUERY)).map((e) => e.id)).toEqual([opens[0]!.request_id])
    await handleOf(id).executeUpdate(RESPOND_UPDATE, { args: [{ request_id: opens[0]!.request_id, response: { kind: 'answer', choice: "I'm here" }, responder: OWNER, role: 'browser' }] })
    expect(await handleOf(id).executeUpdate<number, [Record<string, never>]>(TAB_RECONNECTED_UPDATE, { args: [{}] })).toBe(0)
    turnHold.release()
    await poll(() => finished.map((f) => f.turnId), ['t1'])
    await record('attention_tab', id, turnRun)
    await handleOf(id).signal('end')
    await handleOf(id).result()
  })

  describe('the queue (#6386)', () => {
    /** queue_message, as DurableTurns sends it while a turn runs. */
    const enqueue = (id: string, m: QueuedMessage) => handleOf(id).executeUpdate<QueueAnswer, [QueuedMessage]>(QUEUE_MESSAGE_UPDATE, { args: [m], updateId: m.turn_id })
    const refusal = async (p: Promise<unknown>) => {
      const err = await p.then(
        () => undefined,
        (e: unknown) => e,
      )
      expect(err).toBeInstanceOf(WorkflowUpdateFailedError)
      return ((err as WorkflowUpdateFailedError).cause as ApplicationFailure).type
    }
    const queueOf = async (id: string) => (await handleOf(id).query<QueueState>(QUEUE_QUERY)).queue.map((q) => q.id)
    /** A turn held until released; each turn's id in the order it ran. */
    function holdTurns(): { release: () => void } {
      let hold = held()
      runTurn = async () => {
        await hold.promise
        return { status: 'done', hostQueue: '' }
      }
      return {
        release: () => {
          hold.release()
          hold = held()
          hold.release()
        },
      }
    }

    it('queues messages sent during a turn and runs each as its own turn, in order, each in a run of its own', async () => {
      const id = randomUUID()
      const hold = holdTurns()
      await send(env.client, id, message('t1'))
      const turnRun = await runIdOf(id)
      await poll(() => turns.length, 1)
      expect(await enqueue(id, queued('q1', 'first', { context: '[page: /]' }))).toEqual({ id: 'q1', position: 1, version: 1 })
      expect(await enqueue(id, queued('q2', 'second'))).toEqual({ id: 'q2', position: 2, version: 2 })
      // The same Update again (a re-send) finds its entry; nothing is added.
      expect(await enqueue(id, queued('q2', 'second'))).toMatchObject({ id: 'q2', position: 2 })
      expect(await queueOf(id)).toEqual(['q1', 'q2'])
      expect((await handleOf(id).query<QueueState>(QUEUE_QUERY)).queue[0]).toEqual({ id: 'q1', text: 'first', author: OWNER, queuedAt: '2026-11-15T00:00:00.000Z' })
      // Each change is written, in order, the last version always.
      await poll(() => queueWrites.at(-1)?.version, 2)
      expect(queueWrites.at(-1)?.queue.map((q) => q.id)).toEqual(['q1', 'q2'])
      hold.release()
      await poll(() => finished.map((f) => f.turnId), ['t1', 'q1', 'q2'])
      // The turn's end was given the head: the end started it in its own transaction.
      expect(finished.map((f) => f.next?.turn_id)).toEqual(['q1', 'q2', undefined])
      expect(begun).toEqual([])
      expect(turns.map((t) => [t.turnId, t.message?.text])).toEqual([
        ['t1', 'hello'],
        ['q1', 'first\n\n[page: /]'],
        ['q2', 'second'],
      ])
      // The page context and the claim columns stay out of the turn's message.
      expect(turns[1]?.message).toEqual({ turn_id: 'q1', text: 'first\n\n[page: /]', author: OWNER, images: [], system_append: null, words: 'first' })
      await poll(() => queueWrites.at(-1)?.queue, [])
      expect(await queueOf(id)).toEqual([])
      expect(await eventTypes(id, turnRun)).toContain('EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW')
      await record('queue', id, turnRun)
      await handleOf(id).signal('end')
      await handleOf(id).result()
    })

    it('carries the queue across continue-as-new: the turn it started runs in the next run, with the rest still queued', async () => {
      const id = randomUUID()
      let hold = held()
      runTurn = async () => {
        await hold.promise
        return { status: 'done', hostQueue: '' }
      }
      await send(env.client, id, message('t1'))
      const first = await runIdOf(id)
      await poll(() => turns.length, 1)
      await enqueue(id, queued('q1'))
      await enqueue(id, queued('q2'))
      const t1 = hold
      hold = held()
      t1.release()
      await poll(() => turns.map((t) => t.turnId), ['t1', 'q1'])
      // q1 runs in a new run, which holds q2 and the queue's version.
      const second = await runIdOf(id)
      expect(second).not.toBe(first)
      expect(await handleOf(id).query(TURN_QUERY)).toBe('q1')
      expect(await handleOf(id).query<QueueState>(QUEUE_QUERY)).toMatchObject({ version: 3, queue: [{ id: 'q2' }] })
      // q1's Update sent again in this run (its id is deduped only within a run) does not queue it twice.
      expect(await enqueue(id, queued('q1'))).toEqual({ id: 'q1', position: 0, version: 3 })
      expect(await queueOf(id)).toEqual(['q2'])
      // Still editable there.
      await handleOf(id).executeUpdate(EDIT_QUEUED_UPDATE, { args: [{ id: 'q2', words: 'second, edited' }] })
      hold.release()
      await poll(() => finished.map((f) => f.turnId), ['t1', 'q1', 'q2'])
      expect(turns.at(-1)?.message?.text).toBe('second, edited')
      await handleOf(id).signal('end')
      await handleOf(id).result()
    })

    it('writes the queue change made in the activation where the last write ended: the started head leaves the written queue', async () => {
      const id = randomUUID()
      const hold = holdTurns()
      await send(env.client, id, message('t1'))
      const turnRun = await runIdOf(id)
      await poll(() => turns.length, 1)
      // The queue's write and the turn's end come back together, often in one workflow task (the race;
      // the recorded history queue_publish took it, so its replay pins the patch's branch).
      const both = held()
      queueChanged = async () => {
        await both.promise
        return true
      }
      finish = async (args) => {
        finished.push(args)
        await both.promise
        return args.next ? { written: true, next: 'started' } : true
      }
      await enqueue(id, queued('q1'))
      await poll(() => queueWrites.length, 1)
      hold.release()
      await poll(() => finished.length, 1)
      both.release()
      await poll(() => turns.map((t) => t.turnId), ['t1', 'q1'])
      // The head left the queue: that change is written, the last write is the empty queue.
      await poll(() => queueWrites.at(-1)?.queue, [])
      expect(queueWrites.at(-1)?.version).toBe(2)
      // The run that took QUEUE_PUBLISH_PATCH's branch, replayed as recorded.
      await record('queue_publish', id, turnRun)
      await handleOf(id).signal('end')
      await handleOf(id).result()
    })

    it('a Stop ends the running turn only: the queued messages stay, and the next one starts', async () => {
      const id = randomUUID()
      runTurn = async (input) => {
        if (input.turnId !== 't1') return { status: 'done', hostQueue: '' }
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
      await enqueue(id, queued('q1'))
      await enqueue(id, queued('q2'))
      await handleOf(id).signal('interrupt', { reason: 'stop' })
      await poll(() => finished.map((f) => `${f.turnId} ${f.outcome}`), ['t1 interrupted', 'q1 done', 'q2 done'])
      expect(turns.map((t) => t.turnId)).toEqual(['t1', 'q1', 'q2'])
      await handleOf(id).signal('end')
      await handleOf(id).result()
    })

    it('removes and edits a message before it starts; once started it is neither', async () => {
      const id = randomUUID()
      const hold = holdTurns()
      await send(env.client, id, message('t1'))
      await poll(() => turns.length, 1)
      await enqueue(id, queued('q1'))
      await enqueue(id, queued('q2'))
      await enqueue(id, queued('q3', 'third', { context: 'ctx' }))
      expect((await handleOf(id).executeUpdate<QueueState, [{ id: string }]>(REMOVE_QUEUED_UPDATE, { args: [{ id: 'q2' }] })).queue.map((q) => q.id)).toEqual(['q1', 'q3'])
      const edited = await handleOf(id).executeUpdate<QueueState, [{ id: string; words: string }]>(EDIT_QUEUED_UPDATE, { args: [{ id: 'q3', words: 'third, again' }] })
      expect(edited.queue.map((q) => q.text)).toEqual(['q1', 'third, again'])
      expect(await refusal(handleOf(id).executeUpdate(REMOVE_QUEUED_UPDATE, { args: [{ id: 'q2' }] }))).toBe('not_queued')
      expect(await refusal(handleOf(id).executeUpdate(EDIT_QUEUED_UPDATE, { args: [{ id: 'q1', words: '   ' }] }))).toBe('invalid')
      hold.release()
      await poll(() => finished.map((f) => f.turnId), ['t1', 'q1', 'q3'])
      // The page context stays after the new words.
      expect(turns.map((t) => t.message?.text)).toEqual(['hello', 'q1', 'third, again\n\nctx'])
      expect(await refusal(handleOf(id).executeUpdate(REMOVE_QUEUED_UPDATE, { args: [{ id: 'q1' }] }))).toBe('not_queued')
      expect(await refusal(handleOf(id).executeUpdate(EDIT_QUEUED_UPDATE, { args: [{ id: 'q3', words: 'late' }] }))).toBe('not_queued')
      await handleOf(id).signal('end')
      await handleOf(id).result()
    })

    it('refuses `queue_full` past 20 messages or its size, and `ended` once the session is marked done', async () => {
      const id = randomUUID()
      const hold = holdTurns()
      await send(env.client, id, message('t1'))
      await poll(() => turns.length, 1)
      // One message larger than the queue may hold.
      expect(await refusal(enqueue(id, queued('big', 'x'.repeat(600 * 1024))))).toBe('queue_full')
      // Its size is UTF-8 bytes: 300 Ki two-byte characters are 600 KiB.
      expect(await refusal(enqueue(id, queued('wide', 'é'.repeat(300 * 1024))))).toBe('queue_full')
      for (let i = 1; i <= 20; i++) await enqueue(id, queued(`q${i}`))
      expect(await refusal(enqueue(id, queued('q21')))).toBe('queue_full')
      expect(await queueOf(id)).toHaveLength(20)
      // An edit that would pass the size is refused too.
      expect(await refusal(handleOf(id).executeUpdate(EDIT_QUEUED_UPDATE, { args: [{ id: 'q1', words: 'y'.repeat(600 * 1024) }] }))).toBe('queue_full')
      await handleOf(id).signal('end')
      expect(await refusal(enqueue(id, queued('q22')))).toBe('ended')
      hold.release()
      await handleOf(id).result()
      // The session ended after its turn: the queue went with it.
      expect(turns.map((t) => t.turnId)).toEqual(['t1'])
      expect(queueWrites.at(-1)?.queue).toEqual([])
    })

    it('starts a message queued while no turn runs with session.begin_turn, and one the row is not free for once the turn that took it ends', async () => {
      const id = randomUUID()
      await send(env.client, id, message('t1'))
      await poll(() => finished.length, 1)
      await poll(async () => handleOf(id).query(TURN_QUERY), null)
      // No turn runs: the head is begun at once.
      await enqueue(id, queued('q1'))
      await poll(() => finished.map((f) => f.turnId), ['t1', 'q1'])
      expect(begun).toEqual(['q1'])
      // The row runs another turn (a send claimed it meanwhile): the head waits, and the send's
      // message, taken meanwhile, runs first; the head starts at that turn's end.
      await poll(async () => handleOf(id).query(TURN_QUERY), null)
      begin = async () => 'busy'
      await enqueue(id, queued('q2'))
      await poll(() => begun, ['q1', 'q2'])
      expect(await handleOf(id).executeUpdate(SEND_MESSAGE_UPDATE, { args: [message('t2', 'claimed first')] })).toEqual({ accepted: true, turn_id: 't2' })
      await poll(() => finished.map((f) => f.turnId), ['t1', 'q1', 't2', 'q2'])
      expect(finished[2]?.next?.turn_id).toBe('q2')
      await handleOf(id).signal('end')
      await handleOf(id).result()
    })

    it('drops a head its session cannot run, and starts the next; a worker that answers a bare boolean starts nothing, so the head is begun', async () => {
      const id = randomUUID()
      const hold = holdTurns()
      finish = async (args) => {
        finished.push(args)
        // t1's end comes from an older worker, which ignores `next`; q2 cannot start at q1's end (the session spent its budget, say).
        if (args.next?.turn_id === 'q2') return { written: true, next: 'dropped' }
        return true
      }
      await send(env.client, id, message('t1'))
      await poll(() => turns.length, 1)
      await enqueue(id, queued('q1'))
      await enqueue(id, queued('q2'))
      hold.release()
      await poll(() => finished.map((f) => f.turnId), ['t1', 'q1'])
      expect(begun).toEqual(['q1'])
      expect(finished.map((f) => f.next?.turn_id)).toEqual(['q1', 'q2'])
      await poll(() => queueWrites.at(-1)?.queue, [])
      expect(turns.map((t) => t.turnId)).toEqual(['t1', 'q1'])
      await handleOf(id).signal('end')
      await handleOf(id).result()
    })

    it('a handoff drops the queued messages the new owner did not send', async () => {
      const id = randomUUID()
      const NEXT = { kind: 'bearer', id: 'token:next', label: 'Next agent' } as const
      const hold = holdTurns()
      await send(env.client, id, message('t1'))
      await poll(() => turns.length, 1)
      await enqueue(id, queued('q1'))
      await enqueue(id, { ...queued('q2'), author: NEXT })
      await handleOf(id).signal(OWNER_CHANGED_SIGNAL, { owner: NEXT })
      await poll(() => queueOf(id), ['q2'])
      hold.release()
      await poll(() => finished.map((f) => f.turnId), ['t1', 'q2'])
      await handleOf(id).signal('end')
      await handleOf(id).result()
    })
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
    expect(names).toEqual(expect.arrayContaining(['turn', 'ended', 'park_respond', 'end_while_finishing', 'full_home', 'attention_tab', 'park_record', 'placement_moved', 'queue', 'queue_publish']))
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

    async function durableManager(): Promise<SessionManager> {
      const m = manager({
        sql: db.sql,
        paths: await tempPaths(),
        settings: { get: <T>() => Promise.resolve(undefined as T) },
        run: scriptedRunner(() => ({ throws: 'a durable turn ran in the classic harness' })).runner,
      })
      m.durableTurns = new DurableTurns({ client: env.client, sql: db.sql, events: m.events, sessionQueue: SESSION_QUEUE, liveHosts: polledHosts(env.client, HOST) })
      return m
    }

    const runtimeOf = async (id: string) => (await db.sql<{ runtime: string | null }[]>`SELECT runtime FROM ai_sessions WHERE id = ${id}`)[0]?.runtime
    const describe_ = async (id: string) => {
      const d = await handleOf(id).describe()
      return { type: d.type, taskQueue: d.taskQueue }
    }

    it('starts every new session as AgentSession on agent-sessions, and reads each turn`s end from the event log', async () => {
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

    // Plan Phase 10: #2078 refused a session whose workflow retention removed, for DurableSession,
    // whose history held the chat. An AgentSession's transcript is in ai_session_entries, so its
    // next send starts the workflow again and the turn resumes there (sessions/durable.ts).
    it('starts a session whose workflow retention removed again, and its next turn runs', async () => {
      const m = await durableManager()
      finish = sessionFinishActivities(db.sql, m.events)[SESSION_FINISH_ACTIVITY]
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make me a bracket' })
      expect(await turn!.done).toMatchObject({ kind: 'result' })
      // Ended, then removed by retention: Temporal no longer knows it.
      await handleOf(session.id).terminate('ended')
      await env.client.workflowService.deleteWorkflowExecution({
        namespace: env.client.options.namespace,
        workflowExecution: { workflowId: sessionWorkflowId(session.id) },
      })
      await expect.poll(async () => handleOf(session.id).describe().then(() => 'found', (err: unknown) => (err instanceof WorkflowNotFoundError ? 'gone' : 'found')), { timeout: 10_000 }).toBe('gone')

      const again = await m.send(session.id, browser, 'and a lid')
      expect(await again.done).toMatchObject({ kind: 'result' })
      expect(await describe_(session.id)).toEqual({ type: SESSION_WORKFLOW, taskQueue: SESSION_QUEUE })
      expect(turns.map((t) => t.message?.text)).toEqual(['make me a bracket', 'and a lid'])
      // The turn is a stub here; that a placement resumes from the transcript is warm.ts's
      // (`transcriptExists`), whichever run placed it, as turnHost.pg.test.ts shows.
    })

    it('refuses `closed` a send to a session still recorded as DurableSession, and starts no workflow (plan Step 8.1)', async () => {
      const m = await durableManager()
      finish = sessionFinishActivities(db.sql, m.events)[SESSION_FINISH_ACTIVITY]
      // As the DurableSessions that closed before plan Step 7.2's migration.
      const { session } = await m.start(browser, { origin: 'chat', title: 't' })
      await db.sql`UPDATE ai_sessions SET runtime = 'durable' WHERE id = ${session.id}`
      await expect(m.send(session.id, browser, 'one')).rejects.toMatchObject({ code: 'closed', message: expect.stringMatching(/fork it, or continue in a new chat/) })
      await expect(handleOf(session.id).describe()).rejects.toBeInstanceOf(WorkflowNotFoundError)
      const [row] = await db.sql<{ runtime: string; status: string; durable_turn_id: string | null }[]>`
        SELECT runtime, status, durable_turn_id FROM ai_sessions WHERE id = ${session.id}`
      expect(row).toEqual({ runtime: 'durable', status: 'idle', durable_turn_id: null })
      // No `user.turn` was written for it; the give-back's status is the last event.
      const log = (await m.events.read(session.id)).map((e) => e.event)
      expect(log.filter((e) => e.type === 'user.turn')).toEqual([])
      expect(log.at(-1)).toMatchObject({ type: 'session.status', status: 'idle' })
      expect(turns).toEqual([])
    })

    it('forks a session still recorded as DurableSession, with no transcript, into an AgentSession that runs with no model context (plan Step 8.1)', async () => {
      const m = await durableManager()
      finish = sessionFinishActivities(db.sql, m.events)[SESSION_FINISH_ACTIVITY]
      const { session: parent } = await m.start(browser, { origin: 'chat', title: 'old' })
      await db.sql`UPDATE ai_sessions SET runtime = 'durable' WHERE id = ${parent.id}`
      // Its panel's turns, as the DurableSession's events left them.
      await m.events.append(parent.id, [
        { v: 1, type: 'user.turn', sessionId: parent.id, turnId: 't1', text: 'make a box', author: browser },
        { v: 1, type: 'assistant.text.delta', sessionId: parent.id, messageId: 'durable-2', delta: 'a box' },
        { v: 1, type: 'assistant.text.done', sessionId: parent.id, messageId: 'durable-2' },
        { v: 1, type: 'user.turn', sessionId: parent.id, turnId: 't2', text: 'taller', author: browser },
        { v: 1, type: 'assistant.text.delta', sessionId: parent.id, messageId: 'durable-4', delta: 'taller box' },
        { v: 1, type: 'assistant.text.done', sessionId: parent.id, messageId: 'durable-4' },
      ])
      expect(await m.store.load({ projectKey: '', sessionId: parent.id })).toBeNull()

      const child = await m.fork(parent.id, browser, { upTo: 'durable-2' })
      const [row] = await db.sql<{ runtime: string; fork_parent_id: string | null; fork_cut: number | null; event_cut: boolean; parent_id: string | null }[]>`
        SELECT runtime, fork_parent_id::text AS fork_parent_id, fork_cut, fork_event_cut IS NOT NULL AS event_cut, parent_id
          FROM ai_sessions WHERE id = ${child.id}`
      // An AgentSession linked as its fork that takes none of its parent's transcript (it has none),
      // only its log through the cut (#4212).
      expect(row).toEqual({ runtime: 'unified', fork_parent_id: parent.id, fork_cut: 0, event_cut: true, parent_id: parent.id })
      const texts = (await m.events.read(child.id)).flatMap(({ event: e }) => (e.type === 'user.turn' ? [e.text] : e.type === 'assistant.text.delta' ? [e.delta] : []))
      expect(texts).toEqual(['make a box', 'a box'])
      expect(await m.store.load({ projectKey: '', sessionId: child.id })).toBeNull()
      // So its first turn starts a transcript rather than resuming one (turnRunner.ts, warm.ts).
      expect(await m.store.exists(child.id)).toBe(false)

      // Its first turn runs on its own AgentSession.
      const turn = await m.send(child.id, browser, 'and a lid')
      expect(await turn.done).toMatchObject({ kind: 'result' })
      expect(await describe_(child.id)).toEqual({ type: SESSION_WORKFLOW, taskQueue: SESSION_QUEUE })
      expect(turns.map((t) => [t.sessionId, t.message?.text])).toEqual([[child.id, 'and a lid']])
    })

    it("reads an AgentSession's turn as active while it runs (PR 5042's review)", async () => {
      const m = await durableManager()
      finish = sessionFinishActivities(db.sql, m.events)[SESSION_FINISH_ACTIVITY]
      const running = held()
      const started = held()
      runTurn = async () => {
        started.release()
        await running.promise
        return { status: 'done', hostQueue: '' }
      }
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hold on' })
      await started.promise
      expect((await m.get(session.id, browser)).turnActive).toBe(true)
      running.release()
      await turn!.done
      expect((await m.get(session.id, browser)).turnActive).toBe(false)
    })

    it('a call whose turn ended while it was described writes nothing: the session stays idle and takes the next send', async () => {
      const m = await durableManager()
      finish = sessionFinishActivities(db.sql, m.events)[SESSION_FINISH_ACTIVITY]
      // The real store writes (gate/store.ts), so the row is what a person's panel and the next send see.
      writes = { open: (args) => openInput({ sql: db.sql, events: m.events }, args), resolve: (args) => resolveInput({ sql: db.sql, events: m.events }, args) }
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

    it('queues a send while a turn runs (#6386): its user.turn is written as it starts, done follows it, and the queue reads from the log', async () => {
      const m = await durableManager()
      const real = sessionFinishActivities(db.sql, m.events)
      finish = real[SESSION_FINISH_ACTIVITY]
      begin = real[SESSION_BEGIN_TURN_ACTIVITY]
      queueChanged = real[SESSION_QUEUE_CHANGED_ACTIVITY]
      let hold = held()
      runTurn = async () => {
        await hold.promise
        return { status: 'done', hostQueue: '' }
      }
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
      await poll(() => turns.length, 1)
      // Without `queue` (a flow run, a background task) the send is refused as before.
      await expect(m.send(session.id, browser, 'not queued')).rejects.toMatchObject({ code: 'busy' })
      const second = await m.send(session.id, browser, 'and a lid', { queue: true, context: '[page: /]' })
      const third = await m.send(session.id, browser, 'and a hinge', { queue: true })
      expect([second.queued, third.queued]).toEqual([{ position: 1 }, { position: 2 }])
      await poll(async () => (await m.queued(session.id, browser)).queue.map((q) => q.text), ['and a lid', 'and a hinge'])
      // Only the owner changes it.
      const other = { kind: 'bearer', id: 'token:other', label: 'Other' } as const
      await expect(m.removeQueued(session.id, other, third.turnId)).rejects.toMatchObject({ code: 'not_found' })
      await m.editQueued(session.id, browser, third.turnId, 'and two hinges')
      // Not yet in the transcript.
      const userTurns = async () => (await m.events.read(session.id, 0, 1000)).flatMap(({ event: e }) => (e.type === 'user.turn' ? [e.text] : []))
      expect(await userTurns()).toEqual(['make a box'])
      const t1 = hold
      hold = held()
      hold.release()
      t1.release()
      expect(await turn!.done).toMatchObject({ kind: 'result' })
      expect(await second.done).toMatchObject({ kind: 'result' })
      expect(await third.done).toMatchObject({ kind: 'result' })
      expect(turns.map((t) => t.message?.text)).toEqual(['make a box', 'and a lid\n\n[page: /]', 'and two hinges'])
      expect(await userTurns()).toEqual(['make a box', 'and a lid', 'and two hinges'])
      await poll(async () => (await m.queued(session.id, browser)).queue, [])
      const [row] = await db.sql<{ status: string; durable_turn_id: string | null }[]>`SELECT status, durable_turn_id FROM ai_sessions WHERE id = ${session.id}`
      expect(row).toEqual({ status: 'idle', durable_turn_id: null })
      // A removed message's `done` says it never ran.
      hold = held()
      const fourth = await m.send(session.id, browser, 'hold this', { queue: true })
      expect(fourth.queued).toBeUndefined()
      await poll(() => turns.length, 4)
      const fifth = await m.send(session.id, browser, 'never mind', { queue: true })
      await m.removeQueued(session.id, browser, fifth.turnId)
      await expect(m.removeQueued(session.id, browser, fifth.turnId)).rejects.toMatchObject({ code: 'not_queued' })
      expect(await fifth.done).toMatchObject({ kind: 'failed', message: expect.stringMatching(/left the queue before it started/) })
      hold.release()
      expect(await fourth.done).toMatchObject({ kind: 'result' })
    })

    it('forks a unified session from its transcript; with none written it refuses as classic does (plan Step 6.3)', async () => {
      const m = await durableManager()
      finish = sessionFinishActivities(db.sql, m.events)[SESSION_FINISH_ACTIVITY]
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
      await turn!.done
      const before = await db.sql`SELECT id FROM ai_sessions`
      // Its workflow is not asked: the fork reads ai_session_entries (sessions.e2e forks a real one).
      await expect(m.fork(session.id, browser)).rejects.toMatchObject({ message: expect.stringMatching(/has no transcript to fork yet/) })
      expect(await db.sql`SELECT id FROM ai_sessions`).toHaveLength(before.length)
    })
  })
})
