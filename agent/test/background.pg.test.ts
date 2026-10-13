import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { hashTree } from '../src/plugins/packages/hash.js'
import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import { backgroundActivities, BUSY_GIVE_UP_MS, type FireResult, managerSender } from '../src/background/activities.js'
import { BackgroundTasks, type TaskRunner } from '../src/background/service.js'
import { BackgroundTaskStore, MAX_ACTIVE } from '../src/background/store.js'
import { type BuiltinContext, builtinServer } from '../src/builtins/server.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { SessionError, type SessionManager } from '../src/sessions/manager.js'
import { connectInProcess } from '../src/temporal/turnTools.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// A session's background built-ins (src/background/, builtins/background.ts): the
// tools' answers, which are Claude Code's (measured on 2.1.296 in a classic turn) where
// they can be, the activities that fire a task, and the panel's routes. The workflow
// that drives the activities is test/background.temporal.test.ts.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
const NAMES = ['CronCreate', 'CronDelete', 'CronList', 'ScheduleWakeup', 'TaskStop']
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const NOW = new Date('2026-10-13T08:00:30Z')

describe.skipIf(skip !== undefined)(`a session's background tasks${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  let store: BackgroundTaskStore
  let tasks: BackgroundTasks
  let sessionId: string
  let client: Client
  const started: string[] = []
  const cancelled: string[] = []
  const terminated: string[] = []
  const runner: TaskRunner = {
    start: async (t) => void started.push(t.id),
    cancel: async (t) => void cancelled.push(t.id),
    terminate: async (t) => void terminated.push(t.id),
  }

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    started.length = 0
    cancelled.length = 0
    terminated.length = 0
    store = new BackgroundTaskStore(db.sql)
    tasks = new BackgroundTasks({ store, runner })
    m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
    sessionId = (await m.start(browser, { origin: 'chat' })).session.id
    const context = () => ({ background: { tasks, sessionId, owner: browser, tz: 'UTC', now: () => NOW } }) as unknown as BuiltinContext
    client = await connectInProcess(() => builtinServer(NAMES, context))
  }, 60_000)
  afterEach(async () => {
    await client.close()
    m.abortAll()
    await drop()
  })

  async function call(name: string, args: Record<string, unknown>): Promise<{ text: string; isError: boolean }> {
    const result = (await client.callTool({ name, arguments: args })) as { content: { text: string }[]; isError?: boolean }
    return { text: result.content[0]?.text ?? '', isError: result.isError === true }
  }
  const idOf = (text: string) => /(?:job|task) ([0-9a-f]{8})/.exec(text)![1]!

  it('schedules, lists and cancels cron jobs, each its own workflow, in Claude Code\'s words', async () => {
    expect((await call('CronList', {})).text).toBe('No scheduled jobs.')
    const every = await call('CronCreate', { cron: '*/5 * * * *', prompt: 'check the build' })
    expect(every.text).toMatch(/^Scheduled recurring job [0-9a-f]{8} \(Every 5 minutes\)\. Kept by the session .* Auto-expires after 7 days\. Use CronDelete to cancel sooner\. Times are in UTC\.$/)
    const once = await call('CronCreate', { cron: '30 14 28 2 *', prompt: 'remind me', recurring: false })
    expect(once.text).toMatch(/^Scheduled one-shot task [0-9a-f]{8} \(30 14 28 2 \*\)\. .* It will fire once then auto-delete\./)
    const a = idOf(every.text)
    const b = idOf(once.text)
    expect(started).toEqual([a, b])
    expect(await store.get(a)).toMatchObject({
      kind: 'cron',
      state: 'active',
      nextAt: new Date('2026-10-13T08:05:00Z'),
      expiresAt: new Date(NOW.getTime() + 7 * 86_400_000),
      createdBy: { kind: 'browser', id: 'browser' },
      spec: { cron: '*/5 * * * *', prompt: 'check the build', recurring: true, tz: 'UTC' },
    })
    expect((await call('CronList', {})).text).toBe(
      `${a} — Every 5 minutes (recurring) [session]: check the build\n${b} — 30 14 28 2 * (one-shot) [session]: remind me`,
    )
    expect(await call('CronDelete', { id: a })).toEqual({ text: `Cancelled job ${a}.`, isError: false })
    expect(cancelled).toEqual([a])
    expect(await store.get(a)).toMatchObject({ state: 'cancelled', detail: 'cancelled by CronDelete' })
    expect(await call('CronDelete', { id: a })).toEqual({ text: `No scheduled job with id '${a}'`, isError: true })
    expect(await call('CronCreate', { cron: 'bad cron', prompt: 'x' })).toEqual({
      text: "Invalid cron expression 'bad cron'. Expected 5 fields: M H DoM Mon DoW.",
      isError: true,
    })
  })

  it(`keeps at most ${MAX_ACTIVE} tasks running in a session`, async () => {
    for (let i = 0; i < MAX_ACTIVE; i++) expect((await call('CronCreate', { cron: '7 * * * *', prompt: `p${i}` })).isError).toBe(false)
    expect(await call('CronCreate', { cron: '7 * * * *', prompt: 'one more' })).toMatchObject({
      isError: true,
      text: expect.stringContaining(`already has ${MAX_ACTIVE} background tasks`),
    })
  })

  it('keeps one wakeup at a time, clamped to [60, 3600] s, and stops it', async () => {
    const first = await call('ScheduleWakeup', { delaySeconds: 5, reason: 'watching CI', prompt: 'check CI' })
    expect(first.text).toBe(
      'Next wakeup scheduled for 08:01:30 UTC (in 60s). If you owe the user a status update this tick, write it now as ordinary ' +
        'response text; then end the turn — the session wakes you when the wakeup fires.',
    )
    await call('ScheduleWakeup', { delaySeconds: 99_999, reason: 'idle', prompt: 'go on' })
    const active = await store.list(sessionId)
    expect(active).toHaveLength(1)
    expect(active[0]).toMatchObject({ kind: 'wakeup', spec: { delaySeconds: 3600, prompt: 'go on' }, nextAt: new Date(NOW.getTime() + 3_600_000) })
    expect((await call('ScheduleWakeup', { stop: true })).text).toMatch(/^Loop stopped — cancelled 1 pending wakeup\(s\);/)
    expect(await store.list(sessionId)).toEqual([])
    expect(await call('ScheduleWakeup', { delaySeconds: 60 })).toMatchObject({ isError: true })
  })

  it("ends a session's tasks when it is archived, and terminates them when it is forgotten", async () => {
    const make = () =>
      tasks.create({ sessionId, kind: 'wakeup', description: 'w', spec: { prompt: 'p' }, createdBy: browser, nextAt: new Date(NOW.getTime() + 60_000) })
    const a = (await make())!
    expect(await tasks.endSession(sessionId, 'the chat was archived')).toBe(1)
    expect(cancelled).toEqual([a.id])
    expect(await store.get(a.id)).toMatchObject({ state: 'cancelled', detail: 'the chat was archived' })
    const b = (await make())!
    expect(await tasks.endSession(sessionId, 'gone', { terminate: true })).toBe(1)
    expect(terminated).toEqual([b.id])
    expect(await tasks.endSession(sessionId, 'again')).toBe(0)
  })

  it('stops any task by its ID with TaskStop', async () => {
    const id = idOf((await call('CronCreate', { cron: '7 * * * *', prompt: 'p' })).text)
    expect(JSON.parse((await call('TaskStop', { task_id: id })).text)).toEqual({
      message: `Successfully stopped task: ${id} (p)`,
      task_id: id,
      task_type: 'cron',
    })
    expect(await call('TaskStop', { task_id: 'deadbeef' })).toEqual({ text: 'No task found with ID: deadbeef', isError: true })
  })

  it('fires a task through its activities: the next time, a send, and the end', async () => {
    const sent: { prompt: string; context: string }[] = []
    let answer: FireResult = { result: 'sent' }
    let now = NOW
    const acts = backgroundActivities({
      store,
      tz: 'UTC',
      now: () => now,
      send: async (_t, prompt, context) => {
        sent.push({ prompt, context })
        return answer
      },
    })
    const cron = idOf((await call('CronCreate', { cron: '*/5 * * * *', prompt: 'check' })).text)
    expect(await acts.bg_next({ taskId: cron })).toEqual({ at: Date.parse('2026-10-13T08:05:00Z') })
    expect(await acts.bg_fire({ taskId: cron, waitedMs: 0 })).toEqual({ result: 'sent' })
    expect(sent[0]).toEqual({ prompt: 'check', context: expect.stringContaining(`recurring job ${cron} you scheduled with CronCreate`) })
    expect(await store.get(cron)).toMatchObject({ fired: 1, lastFiredAt: NOW, nextAt: null })
    // A busy session: asked again, and after an hour that occurrence is given up.
    answer = { result: 'busy' }
    expect(await acts.bg_fire({ taskId: cron, waitedMs: 1000 })).toEqual({ result: 'busy' })
    expect(await acts.bg_fire({ taskId: cron, waitedMs: BUSY_GIVE_UP_MS })).toEqual({ result: 'skipped' })
    // Past its 7 days, it ends.
    now = new Date(NOW.getTime() + 7 * 86_400_000)
    expect(await acts.bg_next({ taskId: cron })).toEqual({ end: 'expired after 7 days' })
    // A one-shot ends once it fired; a cancelled one is gone.
    now = NOW
    answer = { result: 'sent' }
    const once = idOf((await call('CronCreate', { cron: '0 9 * * *', prompt: 'once', recurring: false })).text)
    await acts.bg_fire({ taskId: once, waitedMs: 0 })
    expect(await acts.bg_next({ taskId: once })).toEqual({ end: 'fired' })
    await acts.bg_finish({ taskId: once, state: 'done', detail: 'fired' })
    expect(await store.get(once)).toMatchObject({ state: 'done', detail: 'fired' })
    await call('CronDelete', { id: cron })
    expect(await acts.bg_next({ taskId: cron })).toEqual({ end: 'gone' })
    expect(await acts.bg_fire({ taskId: cron, waitedMs: 0 })).toMatchObject({ result: 'end' })
    // A wakeup busy for an hour is not fired.
    const wake = (await store.create({ sessionId, kind: 'wakeup', description: 'w', spec: { prompt: 'w', at: NOW.toISOString() }, createdBy: browser }))!.id
    answer = { result: 'busy' }
    expect(await acts.bg_fire({ taskId: wake, waitedMs: BUSY_GIVE_UP_MS })).toEqual({
      result: 'end',
      state: 'done',
      reason: 'not fired: the session was busy for an hour',
    })
  })

  it('sends as the one who made the task, while they own the session; another refusal ends it', async () => {
    const send = managerSender(m)
    const t = (await store.create({ sessionId, kind: 'wakeup', description: 'w', spec: { prompt: 'p' }, createdBy: browser }))!
    expect(await send(t, 'hello from the schedule', 'ctx')).toEqual({ result: 'sent' })
    const busy = managerSender({ send: async () => Promise.reject(new SessionError('busy', 'running a turn')) })
    expect(await busy(t, 'p', 'c')).toEqual({ result: 'busy' })
    // The creator gave the session away: the task cannot speak for them.
    const theirs = (await store.create({ sessionId, kind: 'wakeup', description: 'w', spec: { prompt: 'p' }, createdBy: agentA }))!
    expect(await send(theirs, 'p', 'c')).toMatchObject({ result: 'end', state: 'failed', reason: expect.stringMatching(/^not sent: /) })
  })

  it("gives a classic turn the session's CronCreate in place of Claude Code's, which ends with the turn", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'bg-pkg-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'loop' }))
    const pkg = { name: 'loop', contentHash: (await hashTree(dir)).hash, dir, allowed: true, builtinTools: ['CronCreate', 'Read'] }
    const scripted = scriptedRunner(() => ({ reply: 'ok' }))
    const classic = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: scripted.runner,
      packagePlugins: async () => ({ paths: [], allowedPaths: [dir], builtinTools: pkg.builtinTools, loaded: [pkg], problems: [], release: () => {} }),
      background: { tasks, tz: 'UTC' },
    })
    const { session } = await classic.start(browser, { origin: 'chat' })
    await classic.send(session.id, browser, 'loop it')
    await expect.poll(() => scripted.runs.length).toBe(1)
    const run = scripted.runs[0]!
    expect(run.builtinTools).toEqual(['Read'])
    expect(Object.keys(run.mcpServers ?? {})).toContain('builtin')
    expect(run.systemPromptAppend).toContain('CronCreate (mcp__builtin__CronCreate)')
    classic.abortAll()
  })

  it("gives a classic turn the session's pinned WebFetch in place of Claude Code's (#2684)", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'web-pkg-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'web' }))
    const pkg = { name: 'web', contentHash: (await hashTree(dir)).hash, dir, allowed: true, builtinTools: ['WebFetch', 'WebSearch'] }
    const scripted = scriptedRunner(() => ({ reply: 'ok' }))
    // No background tasks here: WebFetch is served all the same.
    const classic = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: scripted.runner,
      packagePlugins: async () => ({ paths: [], allowedPaths: [dir], builtinTools: pkg.builtinTools, loaded: [pkg], problems: [], release: () => {} }),
    })
    const { session } = await classic.start(browser, { origin: 'chat' })
    await classic.send(session.id, browser, 'fetch it')
    await expect.poll(() => scripted.runs.length).toBe(1)
    const run = scripted.runs[0]!
    // Claude Code's WebSearch stays (a server tool: the API searches, not this pod).
    expect(run.builtinTools).toEqual(['WebSearch'])
    expect(Object.keys(run.mcpServers ?? {})).toContain('builtin')
    expect(run.systemPromptAppend).toContain('WebFetch (mcp__builtin__WebFetch)')
    expect(run.systemPromptAppend).not.toContain('jobs, wakeups')
    classic.abortAll()
  })

  it("counts WebFetch's own model calls in the cost of a turn that ends on its own (#2684)", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'web-pkg-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'web' }))
    const pkg = { name: 'web', contentHash: (await hashTree(dir)).hash, dir, allowed: true, builtinTools: ['WebFetch'] }
    let spend: ((cost: number) => Promise<void>) | undefined
    const scripted = scriptedRunner(() => ({ reply: 'ok', costUsd: 0.01 }))
    const classic = manager({
      sql: db.sql,
      paths: await tempPaths(),
      packagePlugins: async () => ({ paths: [], allowedPaths: [dir], builtinTools: pkg.builtinTools, loaded: [pkg], problems: [], release: () => {} }),
      // The turn's WebFetch answered with a nested query that cost 0.05 before Claude Code's result.
      run: (run) =>
        (async function* () {
          await spend!(0.05)
          yield* scripted.runner(run)
        })(),
      nestedQueries: (deps) => {
        spend = deps.spent
        return { ask: () => Promise.resolve(''), search: () => Promise.reject(new Error('unused')) }
      },
    })
    const { session } = await classic.start(browser, { origin: 'chat' })
    const turn = await classic.send(session.id, browser, 'fetch it')
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success', costUsd: expect.closeTo(0.06) })
    const [row] = await db.sql<{ cost_usd: string; unpriced_cost_usd: string }[]>`
      SELECT cost_usd, unpriced_cost_usd FROM ai_sessions WHERE id = ${session.id}`
    expect(Number(row!.cost_usd)).toBeCloseTo(0.06)
    expect(Number(row!.unpriced_cost_usd)).toBeCloseTo(0.05)
    classic.abortAll()
  })

  it("lists a session's tasks for the panel, and lets its owner cancel one", async () => {
    const deps: AppDeps = {
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      approvals: m.approvals,
      sessions: m,
      backgroundTasks: tasks,
    }
    const app = createApp(deps)
    const id = idOf((await call('CronCreate', { cron: '*/5 * * * *', prompt: 'check the build' })).text)
    const listed = (await (await app.request(`/api/v1/ai/sessions/${sessionId}/background-tasks`, { headers: UI_READ })).json()) as {
      tasks: Record<string, unknown>[]
    }
    expect(listed.tasks).toEqual([
      expect.objectContaining({ id, kind: 'cron', schedule: 'Every 5 minutes', prompt: 'check the build', state: 'active', next_at: '2026-10-13T08:05:00.000Z' }),
    ])
    const del = (path: string, headers: Record<string, string> = UI) => app.request(path, { method: 'DELETE', headers })
    expect((await del(`/api/v1/ai/sessions/${sessionId}/background-tasks/${id}`, UI_READ)).status).toBe(403)
    const res = await del(`/api/v1/ai/sessions/${sessionId}/background-tasks/${id}`)
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({ task: { id, state: 'cancelled', detail: 'cancelled by the user in the panel' } })
    expect((await del(`/api/v1/ai/sessions/${sessionId}/background-tasks/${id}`)).status).toBe(404)
  })
})
