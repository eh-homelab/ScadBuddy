import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { backgroundActivities, type FireResult } from '../src/background/activities.js'
import { BACKGROUND_WORKFLOW, BackgroundTasks, backgroundWorkflowId } from '../src/background/service.js'
import { BackgroundTaskStore } from '../src/background/store.js'
import { temporalTaskRunner } from '../src/background/temporal.js'
import type { Database } from '../src/db.js'
import { TASK_QUEUE } from '../src/temporal/names.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { TEST_DATABASE_URL, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// A background task's workflow (temporal/backgroundTask.ts) on a Temporal dev server and
// Postgres: it waits for the task's time, sends its prompt once the session is not busy,
// and ends; a cancel ends it wherever it waits.

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))

describe.skipIf(!TEMPORAL_CLI || !TEST_DATABASE_URL)(`a background task's workflow${TEMPORAL_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  let db: Database
  let drop: () => Promise<void>
  let store: BackgroundTaskStore
  let worker: AgentWorker
  let sessionId: string
  let answers: FireResult[]
  const sent: string[] = []

  beforeAll(async () => {
    env = await localTemporal()
  }, 60_000)
  afterAll(async () => {
    await env?.teardown()
  })
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new BackgroundTaskStore(db.sql)
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
    sessionId = (await m.start(browser, { origin: 'chat' })).session.id
    sent.length = 0
    answers = []
    worker = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      activities: backgroundActivities({
        store,
        tz: 'UTC',
        send: async (_t, prompt) => {
          const answer = answers.shift() ?? { result: 'sent' }
          if (answer.result === 'sent') sent.push(prompt)
          return answer
        },
      }),
      workflows: { workflowsPath: WORKFLOWS },
    })
    await worker.running(60_000)
  }, 90_000)
  afterEach(async () => {
    await worker.stop()
    await drop()
  }, 30_000)

  const wakeup = async (inMs: number) =>
    (await store.create({
      sessionId,
      kind: 'wakeup',
      description: 'w',
      spec: { prompt: 'wake up', at: new Date(Date.now() + inMs).toISOString() },
      createdBy: browser,
    }))!
  const start = (taskId: string) =>
    env.client.workflow.start(BACKGROUND_WORKFLOW, { workflowId: backgroundWorkflowId(sessionId, taskId), taskQueue: TASK_QUEUE, args: [{ taskId, retryMs: 200 }] })

  it('sends a wakeup once it is due and the session is no longer busy, then ends', async () => {
    const t = await wakeup(1000)
    answers = [{ result: 'busy' }, { result: 'busy' }]
    const handle = await start(t.id)
    await handle.result()
    expect(sent).toEqual(['wake up'])
    expect(await store.get(t.id)).toMatchObject({ state: 'done', fired: 1, detail: 'fired' })
  }, 60_000)

  it('ends a task the session refused, and says why', async () => {
    const t = await wakeup(0)
    answers = [{ result: 'end', state: 'failed', reason: 'not sent: the session is archived' }]
    await (await start(t.id)).result()
    expect(sent).toEqual([])
    expect(await store.get(t.id)).toMatchObject({ state: 'failed', detail: 'not sent: the session is archived' })
  }, 60_000)

  it('is cancelled while it waits, through the service, and never fires', async () => {
    const tasks = new BackgroundTasks({ store, runner: temporalTaskRunner(env.client, TASK_QUEUE) })
    const t = (await tasks.create({
      sessionId,
      kind: 'wakeup',
      description: 'w',
      spec: { prompt: 'never', at: new Date(Date.now() + 3_600_000).toISOString() },
      createdBy: browser,
    }))!
    const handle = env.client.workflow.getHandle(backgroundWorkflowId(sessionId, t.id))
    await expect.poll(async () => (await store.get(t.id))?.nextAt?.getTime(), { timeout: 20_000 }).toBeDefined()
    expect(await tasks.cancel(sessionId, t.id, 'the test')).toMatchObject({ state: 'cancelled' })
    // It handles its cancellation (it ends the row) and completes.
    await expect.poll(async () => (await handle.describe()).status.name, { timeout: 20_000 }).toBe('COMPLETED')
    expect(sent).toEqual([])
    expect(await store.get(t.id)).toMatchObject({ state: 'cancelled', detail: 'cancelled by the test' })
  }, 60_000)
})
