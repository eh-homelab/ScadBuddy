import { historyToJSON } from '@temporalio/common/lib/proto-utils.js'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { Worker } from '@temporalio/worker'
import { randomUUID } from 'node:crypto'
import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { AlreadyClosedError, startCommand } from '../src/operations/command.js'
import { AgentCommands } from '../src/operations/run.js'
import { type OperationKind, refusal } from '../src/operations/kinds.js'
import { OperationStore } from '../src/operations/store.js'
import type { OperationInput } from '../src/temporal/names.js'
import { operationActivities } from '../src/temporal/operationActivities.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { TEST_DATABASE_URL, throwawayDatabase } from './support/postgres.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// AgentOperation, the agent's command shape (spec 2026-10-01 §4.2, §8; #1055), on a
// Temporal dev server and Postgres, with kinds made for the test.

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))
const HISTORY = fileURLToPath(new URL('./fixtures/agent_operation_histories/succeeded.json', import.meta.url))

let runs = 0
let release: (() => void) | undefined
const kinds: OperationKind[] = [
  {
    name: 'ok',
    subject: () => 's',
    check: async (request) => ({ checked: request.n }),
    run: async (_request, checked) => {
      runs++
      return { ran: checked }
    },
    runAttempts: 1,
    runTimeoutS: 60,
  },
  {
    name: 'refuse',
    subject: () => 's',
    check: async () => {
      throw refusal(422, 'the plugin package is refused', { problems: ['a hook runs a command'] })
    },
    run: async () => {
      runs++
    },
    runAttempts: 1,
    runTimeoutS: 60,
  },
  {
    name: 'fail',
    subject: () => 's',
    check: async () => null,
    run: async () => {
      runs++
      throw refusal(409, 'already installed')
    },
    runAttempts: 1,
    runTimeoutS: 60,
  },
  {
    name: 'boom',
    subject: () => 's',
    check: async () => null,
    run: async () => {
      runs++
      throw new Error('secret path /var/x leaked')
    },
    runAttempts: 1,
    runTimeoutS: 60,
  },
  {
    name: 'slow',
    subject: () => 's',
    check: async () => null,
    run: async () => {
      runs++
      await new Promise<void>((resolve) => (release = resolve))
      return { slow: true }
    },
    runAttempts: 1,
    runTimeoutS: 60,
  },
]

function input(kind: string, request: Record<string, unknown> = {}): OperationInput {
  return { kind, subject: 's', key: randomUUID(), request, runAttempts: 1, runTimeoutS: 60, searchAttributes: false }
}

describe.skipIf(!TEMPORAL_CLI || !TEST_DATABASE_URL)(`AgentOperation${TEMPORAL_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  let db: Database
  let drop: () => Promise<void>
  let store: OperationStore
  let agent: AgentWorker

  beforeAll(async () => {
    env = await localTemporal()
  }, 60_000)
  afterAll(async () => {
    await env?.teardown()
  })
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new OperationStore(db.sql)
    runs = 0
    agent = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      activities: operationActivities(kinds, store),
      workflows: { workflowsPath: WORKFLOWS },
    })
    await agent.running(60_000)
  }, 90_000)
  afterEach(async () => {
    await agent.stop()
    await drop()
  })

  it('runs the effect, records it, and answers with the record', async () => {
    const id = `op-ok-${randomUUID()}`
    const answer = await startCommand(env.client, id, input('ok', { n: 3 }), 30_000)
    expect(answer).toMatchObject({ refusal: null, repeated: false, operation: { status: 'succeeded', result: { ran: { checked: 3 } } } })
    expect(await store.get(answer.operation!.id)).toMatchObject({ status: 'succeeded', kind: 'ok' })
    // Closed: the same key cannot start it again, so the route answers from the record.
    await expect(startCommand(env.client, id, input('ok', { n: 3 }), 30_000)).rejects.toBeInstanceOf(AlreadyClosedError)
    expect(runs).toBe(1)
    if (process.env.RECORD_AGENT_OPERATION_HISTORY) {
      const history = await env.client.workflow.getHandle(id).fetchHistory()
      await writeFile(HISTORY, `${historyToJSON(history)}\n`)
    }
  }, 60_000)

  it('answers a refusal, writes nothing, and lets the same key start again', async () => {
    const id = `op-refuse-${randomUUID()}`
    const answer = await startCommand(env.client, id, input('refuse'), 30_000)
    expect(answer).toMatchObject({
      operation: null,
      refusal: { status: 422, detail: 'the plugin package is refused', extensions: { problems: ['a hook runs a command'] } },
    })
    await expect(env.client.workflow.getHandle(id).result()).rejects.toThrow()
    expect(await db.sql`SELECT 1 FROM ai_operations`).toHaveLength(0)
    // Failed in Temporal's sense, so the same request may run again (and is refused again).
    expect((await startCommand(env.client, id, input('refuse'), 30_000)).refusal?.status).toBe(422)
    expect(runs).toBe(0)
  }, 60_000)

  it("records a run's failure in its own words, and an unexpected one without its text", async () => {
    const failed = await startCommand(env.client, `op-fail-${randomUUID()}`, input('fail'), 30_000)
    expect(failed.operation).toMatchObject({ status: 'failed', error: { status: 409, detail: 'already installed' } })
    const boom = await startCommand(env.client, `op-boom-${randomUUID()}`, input('boom'), 30_000)
    expect(boom.operation).toMatchObject({ status: 'failed', error: { status: 500 } })
    expect(JSON.stringify(boom.operation)).not.toContain('/var/x')
  }, 60_000)

  it('attaches a second request to the running execution: one effect, both answered', async () => {
    const id = `op-slow-${randomUUID()}`
    const first = startCommand(env.client, id, input('slow'), 30_000)
    await expect.poll(() => runs, { timeout: 20_000 }).toBe(1)
    const second = startCommand(env.client, id, input('slow'), 30_000)
    release!()
    const [a, b] = await Promise.all([first, second])
    expect(a.operation?.id).toBe(b.operation?.id)
    expect([a.repeated, b.repeated].sort()).toEqual([false, true])
    expect(runs).toBe(1)
  }, 60_000)

  it('answers 202 past the deadline, and records an execution terminated after its record as lost', async () => {
    const commands = new AgentCommands({ client: env.client, store, kinds, searchAttributes: false, deadlineMs: 1_000 })
    const outcome = await commands.run('slow', {}, 'lost-key')
    expect(outcome).toMatchObject({ status: 'running', operation: { kind: 'slow', status: 'running' } })
    const op = (outcome as { operation: { id: string } }).operation
    expect(await commands.get(op.id)).toMatchObject({ status: 'running' })
    const execution = (await store.execution(op.id))!
    await env.client.workflow.getHandle(execution.workflowId).terminate('test')
    release!()
    expect(await commands.get(op.id)).toMatchObject({ status: 'failed', error: { status: 500 } })
    // The same press answers from the record.
    expect(await commands.run('slow', {}, 'lost-key')).toMatchObject({ status: 'problem', problem: { status: 500 } })
  }, 60_000)

  it('replays its recorded history', async () => {
    const history = JSON.parse(await readFile(HISTORY, 'utf8')) as unknown
    await Worker.runReplayHistory({ workflowsPath: WORKFLOWS }, history)
  }, 60_000)
})
