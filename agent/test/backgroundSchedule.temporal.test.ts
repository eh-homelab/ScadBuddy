import { ScheduleNotFoundError } from '@temporalio/client'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { backgroundActivities, type FireResult } from '../src/background/activities.js'
import { BACKGROUND_WORKFLOW, BackgroundTasks, backgroundWorkflowId } from '../src/background/service.js'
import { BackgroundTaskStore } from '../src/background/store.js'
import { convertCronTasks, temporalTaskRunner } from '../src/background/temporal.js'
import type { Database } from '../src/db.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { TASK_QUEUE } from '../src/temporal/names.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { TEST_DATABASE_URL, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// Recurring cron jobs as Temporal Schedules (unified runtime plan 2026-10-13 Step 9.8, R6 of
// #4773): CronCreate makes a Schedule `bgtask-<session>-<id>` whose every fire is a short
// BackgroundFire workflow; CronDelete, TaskStop and forget delete it; the start-time pass of
// decision Q11 converts the jobs made before; a wakeup stays its own timer workflow.

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))

describe.skipIf(!TEMPORAL_CLI || !TEST_DATABASE_URL)(`recurring cron jobs as Schedules${TEMPORAL_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  let db: Database
  let drop: () => Promise<void>
  let store: BackgroundTaskStore
  let tasks: BackgroundTasks
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
    tasks = new BackgroundTasks({ store, runner: temporalTaskRunner(env.client, TASK_QUEUE, 200) })
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
    for await (const s of env.client.schedule.list()) await env.client.schedule.getHandle(s.scheduleId).delete().catch(() => {})
    await worker.stop()
    await drop()
  }, 30_000)

  const cron = (expression = '* * * * *', expiresAt = new Date(Date.now() + 7 * 24 * 3_600_000)) =>
    tasks.create({
      sessionId,
      kind: 'cron',
      description: 'c',
      spec: { cron: expression, prompt: 'tick', recurring: true, tz: 'Europe/London' },
      createdBy: browser,
      nextAt: new Date(Date.now() + 60_000),
      expiresAt,
    })
  const schedule = (taskId: string) => env.client.schedule.getHandle(backgroundWorkflowId(sessionId, taskId))
  const gone = async (taskId: string) => {
    try {
      await schedule(taskId).describe()
      return false
    } catch (err) {
      if (err instanceof ScheduleNotFoundError) return true
      throw err
    }
  }

  it('makes a Schedule of the cron in its timezone, ending at its expiry, whose fire sends the prompt as the next message', async () => {
    const expires = new Date(Date.now() + 7 * 24 * 3_600_000)
    const t = (await cron('* * * * *', expires))!
    const described = await schedule(t.id).describe()
    expect(described.spec.timezone).toBe('Europe/London')
    expect(described.spec.endAt?.getTime()).toBe(expires.getTime())
    expect(described.action).toMatchObject({ type: 'startWorkflow', workflowType: 'BackgroundFire', taskQueue: TASK_QUEUE })
    expect(described.policies.overlap).toBe('SKIP')
    // On schedule: the next minute's fire, with nobody triggering it.
    await expect.poll(() => sent.length, { timeout: 75_000, interval: 500 }).toBe(1)
    expect(sent).toEqual(['tick'])
    await expect.poll(async () => (await store.get(t.id))?.fired, { timeout: 10_000 }).toBe(1)
    const row = (await store.get(t.id))!
    expect(row.state).toBe('active')
    expect(row.nextAt!.getTime()).toBeGreaterThan(Date.now())
  }, 120_000)

  it('asks a busy session again, then sends', async () => {
    const t = (await cron('0 0 1 1 *'))!
    answers = [{ result: 'busy' }, { result: 'busy' }]
    await schedule(t.id).trigger()
    await expect.poll(() => sent, { timeout: 30_000 }).toEqual(['tick'])
    expect(answers).toEqual([])
  }, 60_000)

  it('ends the row and deletes the Schedule once the job is past its 7 days', async () => {
    const t = (await cron('* * * * *', new Date(Date.now() + 2_000)))!
    await new Promise((r) => setTimeout(r, 2_500))
    await schedule(t.id).trigger()
    await expect.poll(async () => (await store.get(t.id))?.state, { timeout: 30_000 }).toBe('done')
    expect((await store.get(t.id))!.detail).toBe('expired after 7 days')
    await expect.poll(() => gone(t.id), { timeout: 10_000 }).toBe(true)
  }, 60_000)

  it('CronDelete (the service cancel) deletes the Schedule, and a fire after it sends nothing', async () => {
    const t = (await cron())!
    expect(await tasks.cancel(sessionId, t.id, 'CronDelete')).toMatchObject({ state: 'cancelled' })
    expect(await gone(t.id)).toBe(true)
    expect(sent).toEqual([])
  }, 60_000)

  it('forgetting the session deletes the Schedule', async () => {
    const t = (await cron())!
    expect(await gone(t.id)).toBe(false)
    await forgetSubject({ sql: db.sql, client: env.client }, `session-${sessionId}`)
    expect(await gone(t.id)).toBe(true)
    // And the session's rows with it.
    expect(await store.get(t.id)).toBeUndefined()
  }, 60_000)

  it('the start-time pass makes each older job a Schedule and ends its workflow, and a second pass changes nothing', async () => {
    // A job made before Step 9.8: a row whose workflow sleeps until its next occurrence.
    const t = (await store.create({
      sessionId,
      kind: 'cron',
      description: 'c',
      spec: { cron: '0 0 1 1 *', prompt: 'tick', recurring: true, tz: 'UTC' },
      createdBy: browser,
      expiresAt: new Date(Date.now() + 7 * 24 * 3_600_000),
    }))!
    expect(await convertCronTasks({ client: env.client, store, taskQueue: TASK_QUEUE })).toBe(1)
    expect(await gone(t.id)).toBe(false)
    expect(await convertCronTasks({ client: env.client, store, taskQueue: TASK_QUEUE })).toBe(0)
    expect(await store.get(t.id)).toMatchObject({ state: 'active' })
    expect(sent).toEqual([])
  }, 60_000)

  it("an older job's own workflow hands its fires to a Schedule, then ends, its row still active", async () => {
    const t = (await store.create({
      sessionId,
      kind: 'cron',
      description: 'c',
      spec: { cron: '0 0 1 1 *', prompt: 'tick', recurring: true, tz: 'UTC' },
      createdBy: browser,
      expiresAt: new Date(Date.now() + 7 * 24 * 3_600_000),
    }))!
    const handle = await env.client.workflow.start(BACKGROUND_WORKFLOW, {
      workflowId: backgroundWorkflowId(sessionId, t.id),
      taskQueue: TASK_QUEUE,
      args: [{ taskId: t.id, kind: 'cron' }],
    })
    await handle.result()
    expect(await gone(t.id)).toBe(false)
    expect(await store.get(t.id)).toMatchObject({ state: 'active' })
    expect(sent).toEqual([])
  }, 60_000)

  it('a ScheduleWakeup is still its own workflow and fires once', async () => {
    const t = (await tasks.create({
      sessionId,
      kind: 'wakeup',
      description: 'w',
      spec: { prompt: 'wake up', at: new Date(Date.now() + 500).toISOString() },
      createdBy: browser,
    }))!
    await env.client.workflow.getHandle(backgroundWorkflowId(sessionId, t.id)).result()
    expect(sent).toEqual(['wake up'])
    expect(await store.get(t.id)).toMatchObject({ state: 'done', fired: 1 })
    expect(await gone(t.id)).toBe(true)
  }, 60_000)

  it('a one-shot cron job is still its own workflow and fires once', async () => {
    const t = (await tasks.create({
      sessionId,
      kind: 'cron',
      description: 'once',
      spec: { cron: '* * * * *', prompt: 'once', recurring: false, tz: 'UTC' },
      createdBy: browser,
    }))!
    expect(await gone(t.id)).toBe(true)
    await env.client.workflow.getHandle(backgroundWorkflowId(sessionId, t.id)).result()
    expect(sent).toEqual(['once'])
    expect(await store.get(t.id)).toMatchObject({ state: 'done', fired: 1 })
  }, 120_000)
})
