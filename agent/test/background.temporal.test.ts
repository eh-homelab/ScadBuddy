import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { mkdtemp, readFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { backgroundActivities, type FireResult } from '../src/background/activities.js'
import type { AgentResult } from '../src/background/workflowAgents.js'
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
  // agent() calls the stand-in agents saw, and what each answers.
  const ran: { prompt: string; opts: Record<string, unknown> }[] = []
  let agentAnswer: (prompt: string) => AgentResult = (p) => ({ ok: true, result: `did ${p}` })

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
    ran.length = 0
    agentAnswer = (p) => ({ ok: true, result: `did ${p}` })
    worker = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      activities: backgroundActivities({
        store,
        tz: 'UTC',
        paths: await tempPaths(),
        agents: {
          run: async (call) => {
            expect(call.sessionId).toBe(sessionId)
            ran.push({ prompt: call.prompt, opts: call.opts as Record<string, unknown> })
            if (call.prompt === 'hang') {
              await new Promise((r) => call.signal.addEventListener('abort', r, { once: true }))
              return { ok: false, result: null, error: 'stopped' }
            }
            await new Promise((r) => setTimeout(r, 20))
            return agentAnswer(call.prompt)
          },
        },
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

  const runWorkflow = async (body: string, extra: Record<string, unknown> = {}, expiresAt?: Date) => {
    const t = (await store.create({
      sessionId,
      kind: 'workflow',
      description: 'probe it',
      spec: { name: 'probe', description: 'probe it', body, args: { x: 1 }, scriptPath: '/workspace/_claude/workflows/probe-wf_x.js', ...extra },
      createdBy: browser,
      ...(expiresAt ? { expiresAt } : {}),
    }))!
    await (
      await env.client.workflow.start(BACKGROUND_WORKFLOW, {
        workflowId: backgroundWorkflowId(sessionId, t.id),
        taskQueue: TASK_QUEUE,
        args: [{ taskId: t.id, kind: 'workflow', retryMs: 200 }],
      })
    ).result()
    return t
  }

  it("runs a Workflow's script, its agents through the journal, and sends Claude Code's notice of its end", async () => {
    answers = [{ result: 'busy' }]
    const t = await runWorkflow(
      `phase('One')
       const [a, b] = await parallel([() => agent('one', { label: 'a' }), () => agent('two')])
       const c = await pipeline(['p', 'q'], (x) => agent(x))
       return { a, b, c, args }`,
    )
    expect(ran.map((r) => r.prompt).sort()).toEqual(['one', 'p', 'q', 'two'])
    expect(sent).toHaveLength(1)
    const notice = unwrapUntrusted(sent[0]!)
    expect(notice).toContain(`<task-id>${t.id}</task-id>\n<status>completed</status>\n<summary>Dynamic workflow "probe it" completed</summary>`)
    expect(notice).toContain('<result>{"a":"did one","b":"did two","c":["did p","did q"],"args":{"x":1}}</result>')
    expect(notice).toContain(`resumeFromRunId: 'wf_${t.id}', args: {"x":1}`)
    expect(notice).toContain('<agent_count>4</agent_count><agents_done>4</agents_done><agents_error>0</agents_error>')
    expect(await store.get(t.id)).toMatchObject({ state: 'done', detail: 'completed' })

    // A resume answers the calls already made from the journal; a new one runs.
    ran.length = 0
    const again = await runWorkflow(`const a = await agent('one', { label: 'a' }); const n = await agent('new'); return [a, n]`, { resumeFrom: t.id })
    expect(ran.map((r) => r.prompt)).toEqual(['new'])
    expect(unwrapUntrusted(sent[1]!)).toContain('<result>["did one","did new"]</result>')
    expect(await store.get(again.id)).toMatchObject({ state: 'done' })
  }, 60_000)

  it('stops a run at its expiry, its agents with it, and says so', async () => {
    const started = Date.now()
    const t = await runWorkflow(`const a = await agent('hang'); return a`, {}, new Date(Date.now() + 1500))
    expect(Date.now() - started).toBeLessThan(20_000)
    expect(unwrapUntrusted(sent[0]!)).toContain('<status>failed</status>\n<summary>Dynamic workflow "probe it" failed</summary>\n<error>the workflow expired after 6 hours</error>')
    expect(await store.get(t.id)).toMatchObject({ state: 'failed', detail: 'expired' })
  }, 60_000)

  it('starts none of the agents still queued when a run is stopped', async () => {
    // 20 at once: 4 run (WORKFLOW_CONCURRENCY), 16 wait for a slot when the expiry stops the run.
    const t = await runWorkflow(`return await parallel(Array.from({ length: 20 }, (_, i) => () => agent('hang', { label: 'n' + i })))`, {}, new Date(Date.now() + 1500))
    expect(await store.get(t.id)).toMatchObject({ state: 'failed', detail: 'expired' })
    await new Promise((r) => setTimeout(r, 1000))
    expect(ran).toHaveLength(4)
  }, 60_000)

  it('reports a failed agent as null and a script that throws as failed', async () => {
    agentAnswer = () => ({ ok: false, result: null, error: 'boom' })
    const t = await runWorkflow(`const a = await agent('x'); if (a === null) throw new Error('no answer'); return a`)
    const notice = unwrapUntrusted(sent[0]!)
    expect(notice).toContain('<status>failed</status>\n<summary>Dynamic workflow "probe it" failed</summary>\n<error>Error: no answer</error>')
    expect(notice).toContain('<agents_error>1</agents_error>')
    expect(await store.get(t.id)).toMatchObject({ state: 'failed' })
  }, 60_000)

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
