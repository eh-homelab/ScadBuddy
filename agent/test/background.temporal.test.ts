import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { mkdtemp, readFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { backgroundActivities, type FireResult } from '../src/background/activities.js'
import { BACKGROUND_WORKFLOW, BackgroundTasks, backgroundWorkflowId } from '../src/background/service.js'
import { eventNotice } from '../src/background/monitor.js'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import { BackgroundTaskStore, DELIVER_MAX_CHARS } from '../src/background/store.js'
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
        paths: await tempPaths(),
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

  it("runs a monitor's command, sends its events and its end once the session is idle, then ends", async () => {
    const t = (await store.create({
      sessionId,
      kind: 'monitor',
      description: 'build',
      spec: { description: 'build', command: 'echo step one; sleep 0.6; echo step two; exit 2', queue: TASK_QUEUE, timeoutMs: 20_000 },
      createdBy: browser,
    }))!
    answers = [{ result: 'busy' }]
    await (
      await env.client.workflow.start(BACKGROUND_WORKFLOW, {
        workflowId: backgroundWorkflowId(sessionId, t.id),
        taskQueue: TASK_QUEUE,
        args: [{ taskId: t.id, kind: 'monitor', retryMs: 200 }],
      })
    ).result()
    // The events reach the model as data, in the untrusted envelope.
    expect(sent.every((m) => m.startsWith('{\n  "untrusted_data"'))).toBe(true)
    const all = sent.map((m) => unwrapUntrusted(m)).join('\n\n')
    expect(all).toContain(eventNotice(t.id, 'build', 'step one'))
    expect(all).toContain(eventNotice(t.id, 'build', 'step two'))
    expect(all).toContain(`<summary>Monitor "build" stream ended</summary>\n<event>exit code 2 · 2 events</event>`)
    expect(await store.pendingEvents(t.id)).toEqual([])
    expect(await store.get(t.id)).toMatchObject({ state: 'done', detail: 'exited' })
  }, 60_000)

  it('sends a backlog past one delivery\'s budget over several messages, none past it', async () => {
    // 12 events of ~7000 characters each, recorded while the session is busy: ~84k
    // characters, more than DELIVER_MAX_CHARS, wait for one idle delivery.
    const t = (await store.create({
      sessionId,
      kind: 'monitor',
      description: 'chatty',
      spec: {
        description: 'chatty',
        command: "for i in $(seq 1 12); do echo \"line$i $(head -c 7000 /dev/zero | tr '\\0' x)\"; sleep 0.25; done",
        queue: TASK_QUEUE,
        timeoutMs: 20_000,
      },
      createdBy: browser,
    }))!
    answers = Array.from({ length: 25 }, () => ({ result: 'busy' }) as const)
    await (
      await env.client.workflow.start(BACKGROUND_WORKFLOW, {
        workflowId: backgroundWorkflowId(sessionId, t.id),
        taskQueue: TASK_QUEUE,
        args: [{ taskId: t.id, kind: 'monitor', retryMs: 200 }],
      })
    ).result()
    const texts = sent.map((m) => unwrapUntrusted(m))
    expect(texts.length).toBeGreaterThan(1)
    for (const text of texts) expect(text.length).toBeLessThanOrEqual(DELIVER_MAX_CHARS + 200)
    expect(texts[0]).toMatch(/\(\d+ more events follow in the next message\)$/)
    const all = texts.join('\n\n')
    for (let i = 1; i <= 12; i++) expect(all).toContain(`line${i} `)
    expect(await store.pendingEvents(t.id)).toEqual([])
  }, 60_000)

  it('takes the oldest pending event even when it alone is past the budget', async () => {
    const t = (await store.create({
      sessionId,
      kind: 'monitor',
      description: 'm',
      spec: { description: 'm', command: 'true', queue: TASK_QUEUE, timeoutMs: 20_000 },
      createdBy: browser,
    }))!
    for (const c of ['a', 'b', 'c']) await store.appendEvent(t.id, c.repeat(100))
    expect((await store.pendingEvents(t.id, 50)).map((e) => e.text[0])).toEqual(['a'])
    expect((await store.pendingEvents(t.id, 200)).map((e) => e.text[0])).toEqual(['a', 'b'])
    expect(await store.pendingCount(t.id)).toBe(3)
  })

  it('stops a running watch, killing its command, when the session takes no more messages', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'watch-'))
    const t = (await store.create({
      sessionId,
      kind: 'monitor',
      description: 'tail',
      spec: { description: 'tail', command: `echo $$ > ${dir}/pid; while true; do echo x; sleep 0.3; done`, queue: TASK_QUEUE, timeoutMs: 60_000 },
      createdBy: browser,
    }))!
    answers = [{ result: 'end', state: 'failed', reason: 'not sent: the session is archived' }]
    await (
      await env.client.workflow.start(BACKGROUND_WORKFLOW, {
        workflowId: backgroundWorkflowId(sessionId, t.id),
        taskQueue: TASK_QUEUE,
        args: [{ taskId: t.id, kind: 'monitor', retryMs: 200 }],
      })
    ).result()
    expect(await store.get(t.id)).toMatchObject({ state: 'failed', detail: 'not sent: the session is archived' })
    const pid = Number((await readFile(path.join(dir, 'pid'), 'utf8')).trim())
    await expect.poll(() => { try { process.kill(pid, 0); return true } catch { return false } }, { timeout: 10_000 }).toBe(false)
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
