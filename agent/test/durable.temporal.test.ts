import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { Worker } from '@temporalio/worker'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import {
  DURABLE_TASK_QUEUE,
  DurableRefused,
  type DurableSendResult,
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

    expect(await durable.send(input, { text: 'hi', context: null })).toEqual({ started: 'fresh', resumedFresh: false })
    expect(await handle.query(seenQuery)).toEqual({
      args: [{ session_id: sid, max_turns: 30, approval_expiry_seconds: 600, model: null }, null, null],
      messages: [{ text: 'hi', context: null }],
      reviews: [],
    })
    expect(await durable.send(input, { text: 'more', context: 'route: /' })).toEqual({ started: 'attached', resumedFresh: false })
    await expect(durable.send(input, { text: 'again', context: null })).rejects.toThrow(new DurableRefused('the session is busy'))

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
    expect(await durable.send(input, { text: 'after stop', context: null })).toEqual({
      started: 'handed_over',
      resumedFresh: false,
    })
    expect(await env.client.workflow.getHandle(durableWorkflowId(sid)).query(seenQuery)).toEqual({
      args: [input, { handed_over: 2 }, null],
      messages: [{ text: 'after stop', context: null }],
      reviews: [],
    })
    await durable.cancel(sid)
  }, 120_000)

  it('a send while a Stop is still closing the execution waits for it, and is handed over', async () => {
    const sid = randomUUID()
    const durable = new TemporalDurableSessions(env.client, db.sql)
    // The stand-in returns its state this long after the cancel (the plugin's end of task).
    const input = { session_id: sid, max_turns: 5, approval_expiry_seconds: 60, model: null, stop_ms: 2_000 }
    await durable.send(input, { text: 'first', context: null })
    expect(await durable.cancel(sid)).toBe(true)
    // Sent at once: the stopping execution would accept it, then close and lose it.
    const steps: string[] = []
    expect(
      await durable.send(
        input,
        { text: 'after stop', context: null },
        {
          beforeStart: async (r) => void steps.push(`claim ${r.started}`),
          newRun: async () => void steps.push('new run'),
          starting: () => void steps.push('starting'),
        },
      ),
    ).toEqual({ started: 'handed_over', resumedFresh: false })
    // Claimed while the stopped run was the latest; its stream is reset once it closed.
    expect(steps).toEqual(['claim attached', 'new run', 'starting'])
    const seen = await env.client.workflow.getHandle(durableWorkflowId(sid)).query(seenQuery)
    expect(seen.args[1]).toEqual({ handed_over: 1 })
    expect(seen.messages).toEqual([{ text: 'after stop', context: null }])
    await durable.cancel(sid)
  }, 120_000)

  it('a stopped execution that does not close within D: claimed, then refused as still stopping', async () => {
    const sid = randomUUID()
    const durable = new TemporalDurableSessions(env.client, db.sql, { sendDeadlineMs: 1_000 })
    const input = { session_id: sid, max_turns: 5, approval_expiry_seconds: 60, model: null, stop_ms: 600_000 }
    await durable.send(input, { text: 'first', context: null })
    expect(await durable.cancel(sid)).toBe(true)
    const planned: DurableSendResult[] = []
    await expect(
      durable.send(input, { text: 'too soon', context: null }, { beforeStart: async (r) => void planned.push(r) }),
    ).rejects.toThrow(new DurableRefused(STILL_STOPPING))
    // Claimed before the wait (as an attach: the stopped run is still the latest), so the
    // sender was answered without it.
    expect(planned).toEqual([{ started: 'attached', resumedFresh: false }])
    const handle = env.client.workflow.getHandle(durableWorkflowId(sid))
    expect((await handle.query(seenQuery)).messages).toEqual([{ text: 'first', context: null }])
    await handle.terminate('test over')
  }, 120_000)

  it('a terminated execution with no snapshot starts from nothing and says so', async () => {
    const sid = randomUUID()
    const durable = new TemporalDurableSessions(env.client, db.sql)
    const input: DurableSessionInput = { session_id: sid, max_turns: 5, approval_expiry_seconds: 60, model: 'm' }
    await durable.send(input, { text: 'hi', context: null })
    await env.client.workflow.getHandle(durableWorkflowId(sid)).terminate('operator')
    expect(await durable.send(input, { text: 'hi again', context: null })).toEqual({ started: 'fresh', resumedFresh: true })
    expect((await env.client.workflow.getHandle(durableWorkflowId(sid)).query(seenQuery)).args).toEqual([input, null, null])
    await durable.cancel(sid)
  }, 120_000)
})
