import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { Worker } from '@temporalio/worker'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import {
  DURABLE_TASK_QUEUE,
  DurableRefused,
  type DurableSessionInput,
  durableWorkflowId,
  STILL_STOPPING,
  TemporalDurableSessions,
} from '../src/durable/client.js'
import { seenQuery } from './support/durableWorkflow.js'
import { TEST_DATABASE_URL, throwawayDatabase } from './support/postgres.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// The agent service's calls to DurableSession on a Temporal dev server (#1056), against a
// TypeScript workflow registered under agent-durable's names (support/durableWorkflow.ts):
// the exact argument shapes the Python workflow is given, its refusals, and a Stop's
// hand-over to the next execution. Postgres serves the (empty) snapshot lookup.

const WORKFLOWS = fileURLToPath(new URL('./support/durableWorkflow.ts', import.meta.url))

describe.skipIf(!TEMPORAL_CLI || !TEST_DATABASE_URL)(`DurableSession over Temporal${TEMPORAL_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  let db: Database
  let drop: () => Promise<void>
  let worker: Worker
  let running: Promise<void>

  beforeAll(async () => {
    env = await localTemporal()
  }, 60_000)
  afterAll(async () => {
    await env?.teardown()
  })
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    worker = await Worker.create({
      connection: env.nativeConnection,
      namespace: 'default',
      taskQueue: DURABLE_TASK_QUEUE,
      workflowsPath: WORKFLOWS,
    })
    running = worker.run()
  }, 90_000)
  afterEach(async () => {
    worker.shutdown()
    await running
    await drop()
  }, 30_000)

  it('starts, attaches, refuses, reviews and hands over with the shapes the workflow expects', async () => {
    const sid = randomUUID()
    const durable = new TemporalDurableSessions(env.client, db.sql)
    const handle = env.client.workflow.getHandle(durableWorkflowId(sid))
    const input: DurableSessionInput = { session_id: sid, max_turns: 30, approval_expiry_seconds: 600, model: null }
    const [hi, more, again, afterStop] = [randomUUID(), randomUUID(), randomUUID(), randomUUID()]

    expect(await durable.send(input, hi)).toEqual({ started: 'fresh', resumedFresh: false })
    // The wire shape agent-durable's models.py takes: [SessionInput, AgentState | None], and
    // the nudge `Nudge(id)`.
    expect(await handle.query(seenQuery)).toEqual({
      args: [{ session_id: sid, max_turns: 30, approval_expiry_seconds: 600, model: null }, null],
      messages: [{ id: hi }],
      reviews: [],
    })
    expect(await durable.send(input, more)).toEqual({ started: 'attached', resumedFresh: false })
    // The same id again (a re-send after a timeout): Temporal dedupes the Update by its id.
    expect(await durable.send(input, more)).toEqual({ started: 'attached', resumedFresh: false })
    expect((await handle.query(seenQuery)).messages).toEqual([{ id: hi }, { id: more }])
    await expect(durable.send(input, again)).rejects.toThrow(new DurableRefused('the session is busy'))

    expect(await durable.pending(sid)).toEqual([{ id: 'toolu_1', name: 'send_to_bambuddy', input: { output: 'box.3mf' } }])
    await durable.review(sid, 'toolu_1', true, 'browser:browser')
    await expect(durable.review(sid, 'toolu_9', true, 'browser:browser')).rejects.toThrow(
      new DurableRefused('No tool call toolu_9 is waiting for approval'),
    )
    expect((await handle.query(seenQuery)).reviews).toEqual([['toolu_1', true, 'browser:browser']])

    // Stop: the execution completes with its state, which the next one is started with.
    expect(await durable.cancel(sid)).toBe(true)
    expect(await handle.result()).toEqual({ handed_over: 2 })
    expect(await durable.cancel(sid)).toBe(false)
    expect(await durable.pending(sid)).toEqual([])
    expect(await durable.send(input, afterStop)).toEqual({ started: 'handed_over', resumedFresh: false })
    expect(await env.client.workflow.getHandle(durableWorkflowId(sid)).query(seenQuery)).toEqual({
      args: [input, { handed_over: 2 }],
      messages: [{ id: afterStop }],
      reviews: [],
    })
    await durable.cancel(sid)
  }, 120_000)

  it('a send while a Stop is still closing the execution waits for it, and is handed over', async () => {
    const sid = randomUUID()
    const durable = new TemporalDurableSessions(env.client, db.sql)
    // The stand-in returns its state this long after the cancel (the plugin's end of task).
    const input = { session_id: sid, max_turns: 5, approval_expiry_seconds: 60, model: null, stop_ms: 2_000 }
    await durable.send(input, randomUUID())
    expect(await durable.cancel(sid)).toBe(true)
    // Sent at once: the stopping execution would accept it, then close and lose it.
    const after = randomUUID()
    const steps: string[] = []
    expect(
      await durable.send(input, after, {
        beforeStart: async (r) => void steps.push(`chose ${r.started}`),
        starting: () => void steps.push('starting'),
      }),
    ).toEqual({ started: 'handed_over', resumedFresh: false })
    // Chosen once the stopped run closed: the next run, with the stream reset.
    expect(steps).toEqual(['chose handed_over', 'starting'])
    const seen = await env.client.workflow.getHandle(durableWorkflowId(sid)).query(seenQuery)
    expect(seen.args[1]).toEqual({ handed_over: 1 })
    expect(seen.messages).toEqual([{ id: after }])
    await durable.cancel(sid)
  }, 120_000)

  it('a stopped execution that does not close within the stopping wait: refused as still stopping, or waited for while delivering', async () => {
    const sid = randomUUID()
    const durable = new TemporalDurableSessions(env.client, db.sql, { stoppingWaitMs: 1_000 })
    const input = { session_id: sid, max_turns: 5, approval_expiry_seconds: 60, model: null, stop_ms: 600_000 }
    const first = randomUUID()
    await durable.send(input, first)
    expect(await durable.cancel(sid)).toBe(true)
    await expect(durable.send(input, randomUUID())).rejects.toThrow(new DurableRefused(STILL_STOPPING))
    // Delivering (the manager's `attempt`), it waits again rather than give the message up.
    let beats = 0
    await expect(durable.send(input, randomUUID(), { attempt: async () => ++beats <= 2 })).rejects.toThrow(
      'the message is no longer waiting to be sent',
    )
    expect(beats).toBe(3)
    const handle = env.client.workflow.getHandle(durableWorkflowId(sid))
    expect((await handle.query(seenQuery)).messages).toEqual([{ id: first }])
    await handle.terminate('test over')
  }, 120_000)

  it('a terminated execution with no snapshot starts from nothing and says so', async () => {
    const sid = randomUUID()
    const durable = new TemporalDurableSessions(env.client, db.sql)
    const input: DurableSessionInput = { session_id: sid, max_turns: 5, approval_expiry_seconds: 60, model: 'm' }
    await durable.send(input, randomUUID())
    await env.client.workflow.getHandle(durableWorkflowId(sid)).terminate('operator')
    expect(await durable.send(input, randomUUID())).toEqual({ started: 'fresh', resumedFresh: true })
    expect((await env.client.workflow.getHandle(durableWorkflowId(sid)).query(seenQuery)).args).toEqual([input, null])
    await durable.cancel(sid)
  }, 120_000)
})
