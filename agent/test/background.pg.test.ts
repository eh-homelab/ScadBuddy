import { randomUUID } from 'node:crypto'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { hashTree } from '../src/plugins/packages/hash.js'
import type { McpSdkServerConfigWithInstance, SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { MockActivityEnvironment } from '@temporalio/testing'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import { AuditLog } from '../src/audit/log.js'
import { backgroundActivities, BUSY_GIVE_UP_MS, type FireResult, managerSender } from '../src/background/activities.js'
import { BackgroundTasks, type TaskRunner } from '../src/background/service.js'
import { BackgroundTaskStore, MAX_ACTIVE } from '../src/background/store.js'
import { type BuiltinContext, builtinServer, classicBuiltinServer } from '../src/builtins/server.js'
import type { WebContext } from '../src/builtins/web.js'
import type { Database } from '../src/db.js'
import { NoUsableCredentialError } from '../src/harness/fallback.js'
import { originPolicy } from '../src/http/origins.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import { SessionError, type SessionManager } from '../src/sessions/manager.js'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import { TurnHost } from '../src/sessions/turnHost.js'
import { connectInProcess } from '../src/temporal/turnTools.js'
import { SessionFiles } from '../src/workspace/store.js'
import { managerAgents } from '../src/background/workflowAgents.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, promptText, scriptedRunner, tempPaths } from './support/sessions.js'
import { AGENT_SESSIONS_SKIP, agentSessions } from './support/agentSessions.js'

// A session's background built-ins (src/background/, builtins/background.ts): the
// tools' answers, which are Claude Code's (measured on 2.1.296 in a classic turn) where
// they can be, the activities that fire a task, and the panel's routes. The workflow
// that drives the activities is test/background.temporal.test.ts.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
const NAMES = ['CronCreate', 'CronDelete', 'CronList', 'ScheduleWakeup', 'Monitor', 'Workflow', 'TaskStop']
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const NOW = new Date('2026-10-13T08:00:30Z')

describe.skipIf((skip !== undefined) || AGENT_SESSIONS_SKIP !== '')(`a session's background tasks${skip ? ` (skipped: ${skip})` : ''}${AGENT_SESSIONS_SKIP}`, () => {
  agentSessions()
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
  // What the stand-in runner answers a start: undefined, or a task with nothing to run.
  let startEnd: { end: string } | undefined
  const runner: TaskRunner = {
    start: async (t) => {
      started.push(t.id)
      return startEnd
    },
    cancel: async (t) => void cancelled.push(t.id),
    terminate: async (t) => void terminated.push(t.id),
  }

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    started.length = 0
    startEnd = undefined
    cancelled.length = 0
    terminated.length = 0
    store = new BackgroundTaskStore(db.sql)
    tasks = new BackgroundTasks({ store, runner })
    m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
    sessionId = (await m.start(browser, { origin: 'chat' })).session.id
    const context = () =>
      ({
        background: { tasks, sessionId, owner: browser, tz: 'UTC', now: () => NOW, commandQueue: 'agent-tools.pod-a', workspace: new SessionFiles(db.sql) },
      }) as unknown as BuiltinContext
    client = await connectInProcess(() => builtinServer(NAMES, context))
  }, 60_000)
  afterEach(async () => {
    await client.close()
    m.abortAll()
    await drop()
  }, 60_000)

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
    // What a Temporal Schedule refuses or reads otherwise (background/schedule.ts).
    for (const [cron, field] of [['0 0 L * *', "field 3 ('L')"], ['0 9 * * 5#3', "field 5 ('5#3')"], ['0 0 ? * MON', "field 3 ('?')"], ['0 0 15W * *', "field 3 ('15W')"]] as const) {
      expect(await call('CronCreate', { cron, prompt: 'x' })).toMatchObject({ isError: true, text: expect.stringContaining(field) })
    }
    // A recurring job with no occurrence before its expiry is not kept, and says why.
    startEnd = { end: 'expired after 7 days' }
    const monthly = await call('CronCreate', { cron: '0 9 1 * *', prompt: 'monthly' })
    expect(monthly).toMatchObject({ isError: true })
    expect(monthly.text).toMatch(/^Recurring job [0-9a-f]{8} \(.*\) was not kept: expired after 7 days\. Times are in UTC\.$/)
    expect(await store.get(idOf(monthly.text))).toMatchObject({ state: 'done', detail: 'expired after 7 days' })
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

  it('starts a monitor where the session\'s Bash runs, in Claude Code\'s words', async () => {
    const started_ = await call('Monitor', { description: 'errors in deploy.log', timeout_ms: 99_999_999, command: 'tail -f deploy.log | grep --line-buffered ERROR' })
    expect(started_.text).toMatch(/^Monitor started \(task [0-9a-f]{8}, expires in 30m unless the source ends first; you get one notice at expiry/)
    const id = idOf(started_.text)
    expect(await store.get(id)).toMatchObject({
      kind: 'monitor',
      description: 'errors in deploy.log',
      spec: { command: 'tail -f deploy.log | grep --line-buffered ERROR', queue: 'agent-tools.pod-a', timeoutMs: 1_800_000 },
      expiresAt: new Date(NOW.getTime() + 1_800_000),
    })
    expect(await call('Monitor', { description: 'x', timeout_ms: 1000 })).toMatchObject({ isError: true, text: 'Give exactly one of command and ws.' })
    expect(await call('Monitor', { description: 'x', timeout_ms: 1000, ws: { url: 'ws://127.0.0.1:9/' } })).toMatchObject({
      isError: true,
      text: expect.stringContaining('loopback'),
    })
    expect(
      await call('Monitor', { description: 'x', timeout_ms: 1000, ws: { url: 'wss://events.example.com/', protocols: ['v1', 'a b'] } }),
    ).toMatchObject({ isError: true, text: '"a b" is not a WebSocket subprotocol: use only token characters.' })
    expect(JSON.parse((await call('TaskStop', { task_id: id })).text)).toMatchObject({
      message: `Successfully stopped task: ${id} (tail -f deploy.log | grep --line-buffered ERROR)`,
      task_type: 'monitor',
    })
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

  it("starts a Workflow in Claude Code's words, keeping its script in the workspace to edit and resume", async () => {
    const script = "export const meta = { name: 'Find Bugs', description: 'find them' }\nreturn await agent('look')"
    const started_ = await call('Workflow', { script, args: { dir: 'src' } })
    const id = /Task ID: ([0-9a-f]{8})/.exec(started_.text)![1]!
    const scriptPath = `/workspace/_claude/workflows/find-bugs-wf_${id}.js`
    expect(started_.text).toMatch(new RegExp(`^Workflow launched in background\\. Task ID: ${id}\\nSummary: find them\\nScript file: ${scriptPath}\\n`))
    expect(started_.text).toContain(`Run ID: wf_${id}`)
    expect(await store.get(id)).toMatchObject({
      kind: 'workflow',
      description: 'find them',
      spec: { name: 'Find Bugs', body: "\nreturn await agent('look')", args: { dir: 'src' }, scriptPath },
    })
    const kept = await new SessionFiles(db.sql).read({ kind: 'session', sessionId }, scriptPath.slice('/workspace/'.length))
    expect(kept?.bytes.toString('utf8')).toBe(script)
    // Still running: no resume until it is stopped; then one from the kept script.
    expect(await call('Workflow', { scriptPath, resumeFromRunId: `wf_${id}` })).toMatchObject({ isError: true, text: expect.stringContaining('still running') })
    await call('TaskStop', { task_id: id })
    const resumed = await call('Workflow', { scriptPath, resumeFromRunId: `wf_${id}` })
    expect(resumed.isError).toBe(false)
    expect(await store.get(/Task ID: ([0-9a-f]{8})/.exec(resumed.text)![1]!)).toMatchObject({ spec: { resumeFrom: id } })
    // What it refuses, as Claude Code would.
    expect(await call('Workflow', { script: 'return 1' })).toMatchObject({ isError: true, text: expect.stringContaining('export const meta') })
    expect(await call('Workflow', { script: "export const meta = { name: N, description: 'd' }\n" })).toMatchObject({ isError: true, text: expect.stringContaining('N is a variable') })
    expect(await call('Workflow', { name: 'saved' })).toMatchObject({ isError: true, text: expect.stringContaining('No saved workflow named "saved"') })
    expect(await call('Workflow', { scriptPath: '/workspace/nope.js' })).toMatchObject({ isError: true })
  })

  it("ends the child chats a dead attempt left open before a retried run's script starts (#3395)", async () => {
    // Attempt 1's agent hangs in its chat's turn; its worker "dies": the attempt is never awaited, its finally never runs.
    const scripted = scriptedRunner((_run, prompt) => (prompt.includes('first') ? { hang: true } : { reply: 'done again' }))
    const mm = manager({ sql: db.sql, paths: await tempPaths(), run: scripted.runner })
    const parent = (await mm.start(browser, { origin: 'chat' })).session.id
    const acts = backgroundActivities({ store, tz: 'UTC', paths: await tempPaths(), send: async () => ({ result: 'sent' }), agents: managerAgents(mm) })
    const t = (await store.create({
      sessionId: parent,
      kind: 'workflow',
      description: 'retry',
      spec: { name: 'retry', description: 'retry', body: `return await agent('first')`, scriptPath: '/workspace/_claude/workflows/retry-wf_x.js' },
      createdBy: browser,
    }))!
    const info = { activityType: 'wf_run', workflowExecution: { workflowId: `bgtask-${parent}-${t.id}`, runId: 'r' }, taskQueue: 'agent-tools' }
    const first = new MockActivityEnvironment({ ...info, attempt: 1 })
    const abandoned = first.run(acts.wf_run!, { taskId: t.id }).catch(() => undefined)
    await expect.poll(() => scripted.prompts.length).toBe(1)
    const [orphan] = await db.sql<{ id: string }[]>`SELECT id FROM ai_sessions WHERE parent_id = ${parent}`
    await expect.poll(async () => (await mm.get(orphan!.id, browser)).status).toBe('running')

    // The retry: its script differs only so its own agent can answer.
    await db.sql`UPDATE ai_background_tasks SET spec = spec || ${db.sql.json({ body: `return await agent('second')` } as never)} WHERE id = ${t.id}`
    const out = await new MockActivityEnvironment({ ...info, attempt: 2 }).run(acts.wf_run!, { taskId: t.id })
    expect(out).toMatchObject({ status: 'completed', result: 'done again' })
    expect((await mm.get(orphan!.id, browser)).status).toBe('done')
    first.cancel()
    await abandoned
    mm.abortAll()
  }, 60_000)

  it("runs an agent() as a child chat on the session's budget, its reply the result, a schema checked and asked again", async () => {
    // The turn's message, as the warm process was sent it.
    const scripted = scriptedRunner((_run, prompt) => {
      if (prompt.includes('give n')) return { reply: 'not json' }
      if (prompt.startsWith('Your reply did not match the schema')) return { reply: '```json\n{"n": 7}\n```' }
      return { reply: 'hello from the agent' }
    })
    const mm = manager({ sql: db.sql, paths: await tempPaths(), run: scripted.runner })
    const parent = (await mm.start(browser, { origin: 'chat' })).session.id
    const agents = managerAgents(mm)
    const signal = new AbortController().signal
    const text = await agents.run({ sessionId: parent, owner: browser, workflow: 'probe', prompt: 'say hi', opts: { label: 'hi' }, signal })
    expect(text).toMatchObject({ ok: true, result: 'hello from the agent' })
    const child = await mm.get(text.childId!, browser)
    expect(child).toMatchObject({ parentId: parent, title: 'probe · hi', tags: ['workflow'], status: 'done' })
    const [row] = await db.sql<{ root: string }[]>`SELECT budget_root_id AS root FROM ai_sessions WHERE id = ${child.id}`
    expect(row!.root).toBe(parent)
    // Claude Code's frame and instructions reach the child's turn.
    expect(promptText(scripted.prompts[0]!)).toMatch(/^\[Workflow harness — computed task\][\s\S]*follows:\n {2}say hi\n\nYou are a subagent spawned by a workflow orchestration script/)
    const schema = { type: 'object', properties: { n: { type: 'number' } }, required: ['n'] }
    const obj = await agents.run({ sessionId: parent, owner: browser, workflow: 'probe', prompt: 'give n', opts: { schema }, signal })
    expect(obj, JSON.stringify(obj)).toMatchObject({ ok: true, result: { n: 7 } })
    expect(promptText(scripted.prompts.at(-1)!)).toMatch(/^Your reply did not match the schema:/)

    // Its options act as Claude Code's: the chat's own model, effort and subagent definition.
    const typed = managerAgents(mm, { agentTypes: async () => ['model-author', 'print-analyst'] })
    const tuned = await typed.run({
      sessionId: parent,
      owner: browser,
      workflow: 'probe',
      prompt: 'say hi',
      opts: { model: 'claude-haiku-test', effort: 'low', agentType: 'scadbuddy:model-author' },
      signal,
    })
    expect(tuned.ok).toBe(true)
    const [own] = await db.sql<{ agent_model: string; agent_effort: string; agent_type: string }[]>`
      SELECT agent_model, agent_effort, agent_type FROM ai_sessions WHERE id = ${tuned.childId!}`
    expect(own).toEqual({ agent_model: 'claude-haiku-test', agent_effort: 'low', agent_type: 'model-author' })
    expect(scripted.runs.at(-1)).toMatchObject({ model: 'claude-haiku-test', effort: 'low' })
    // A stopped run starts no chat.
    const before = (await db.sql<{ n: number }[]>`SELECT count(*)::int AS n FROM ai_sessions`)[0]!.n
    expect(await typed.run({ sessionId: parent, owner: browser, workflow: 'p', prompt: 'x', opts: {}, signal: AbortSignal.abort() })).toMatchObject({
      ok: false,
      error: 'stopped',
    })
    expect((await db.sql<{ n: number }[]>`SELECT count(*)::int AS n FROM ai_sessions`)[0]!.n).toBe(before)
    expect(await typed.run({ sessionId: parent, owner: browser, workflow: 'p', prompt: 'x', opts: { agentType: 'nope' }, signal })).toMatchObject({
      ok: false,
      error: "Agent type 'nope' not found. Available agents: general-purpose, model-author, print-analyst",
    })
    expect(await typed.run({ sessionId: parent, owner: browser, workflow: 'p', prompt: 'x', opts: { isolation: 'worktree' }, signal })).toMatchObject({
      ok: false,
      error: expect.stringContaining('Cannot create a worktree: not in a git repository'),
    })
    mm.abortAll()
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
    const pkg = { name: 'loop', contentHash: (await hashTree(dir)).hash, dir, allowed: true, builtinTools: ['CronCreate', 'Monitor', 'Read'] }
    const scripted = scriptedRunner(() => ({ reply: 'ok' }))
    const classic = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: scripted.runner,
      packagePlugins: async () => ({ paths: [], allowedPaths: [dir], builtinTools: pkg.builtinTools, loaded: [pkg], problems: [], release: () => {} }),
      background: { tasks, tz: 'UTC', commandQueue: 'agent-tools.pod-b' },
    })
    const { session } = await classic.start(browser, { origin: 'chat' })
    await classic.send(session.id, browser, 'loop it')
    await expect.poll(() => scripted.runs.length).toBe(1)
    const run = scripted.runs[0]!
    expect(run.builtinTools).toEqual(['Read'])
    expect(Object.keys(run.mcpServers ?? {})).toContain('builtin')
    expect(run.systemPromptAppend).toContain('CronCreate (mcp__builtin__CronCreate)')
    // The turn's Monitor runs its command on this replica's own queue, where the turn's Bash runs.
    const turnClient = await connectInProcess(() => (run.mcpServers!.builtin as McpSdkServerConfigWithInstance).instance)
    const watch = (await turnClient.callTool({ name: 'Monitor', arguments: { description: 'log', command: 'tail -f x.log', timeout_ms: 60_000 } })) as {
      content: { text: string }[]
    }
    expect((await store.get(idOf(watch.content[0]!.text)))!.spec).toMatchObject({ queue: 'agent-tools.pod-b' })
    await turnClient.close()
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

  it("serves a classic turn's WebFetch through the pinned fetch, refusing loopback unless allowed (#2974)", async () => {
    const page = createServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'text/html' })
      res.end('<html><body><h1>Pinned</h1></body></html>')
    })
    await new Promise<void>((r) => page.listen(0, '127.0.0.1', r))
    const port = (page.address() as AddressInfo).port
    const asked: string[] = []
    const web = (allowLoopback: boolean): WebContext => ({
      nested: { ask: async (prompt) => (asked.push(prompt), 'the answer'), search: () => Promise.reject(new Error('unused')) },
      secrets: () => [],
      actor: browser,
      signal: new AbortController().signal,
      resolve: async () => ['127.0.0.1'],
      allowLoopback,
    })
    const fetchVia = async (allowLoopback: boolean, page: string) => {
      const client = await connectInProcess(() => classicBuiltinServer(['WebFetch'], { web: web(allowLoopback) }).instance)
      try {
        const result = (await client.callTool({ name: 'WebFetch', arguments: { url: `http://pages.test:${port}/${page}`, prompt: 'title?' } })) as {
          content: { text: string }[]
          isError?: boolean
        }
        return { text: result.content[0]?.text ?? '', isError: result.isError === true }
      } finally {
        await client.close()
      }
    }
    try {
      const served = await fetchVia(true, 'a')
      expect(served.isError).toBe(false)
      expect(unwrapUntrusted(served.text)).toBe('the answer')
      expect(asked[0]).toContain('# Pinned')
      // The name resolves to loopback, which the pinned fetch refuses without the test switch
      // (another page: WebFetch caches the first).
      expect(await fetchVia(false, 'b')).toMatchObject({ isError: true })
      expect(asked).toHaveLength(1)
    } finally {
      await new Promise((r) => page.close(r))
    }
  })

  it("sends WebFetch's model call on the credential a turn has failed over to (#2973)", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'web-pkg-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'web' }))
    const pkg = { name: 'web', contentHash: (await hashTree(dir)).hash, dir, allowed: true, builtinTools: ['WebFetch'] }
    const pooled = (id: string, secret: string) => ({ id, epoch: 0, label: `credential ${id}`, credential: { kind: 'anthropic_api_key' as const, secret } })
    let credential: (() => Promise<{ secret: string }>) | undefined
    const used: string[] = []
    let attempts = 0
    const classic = manager({
      sql: db.sql,
      paths: await tempPaths(),
      packagePlugins: async () => ({ paths: [], allowedPaths: [dir], builtinTools: pkg.builtinTools, loaded: [pkg], problems: [], release: () => {} }),
      // The first credential is refused after its init; the second attempt's WebFetch asks.
      run: (run) =>
        (async function* () {
          attempts += 1
          const session_id = run.sessionId ?? run.resume ?? 'unknown'
          yield { type: 'system', subtype: 'init', session_id, model: 'claude-sonnet-4-5', mcp_servers: [] } as unknown as SDKMessage
          // Each attempt asks on the credential it is on, not only the last (#3508 review).
          used.push((await credential!()).secret)
          if (attempts === 1) {
            yield { type: 'system', subtype: 'api_retry', attempt: 1, max_retries: 10, retry_delay_ms: 500, error_status: 401, error: 'authentication_failed' } as unknown as SDKMessage
            return
          }
          yield { type: 'result', subtype: 'success', is_error: false, result: 'ok', num_turns: 1, total_cost_usd: 0.01, session_id } as unknown as SDKMessage
        })(),
      nestedQueries: (deps) => {
        credential = deps.credential
        return { ask: () => Promise.resolve(''), search: () => Promise.reject(new Error('unused')) }
      },
      credentials: {
        candidates: () => Promise.resolve([pooled('a', 'sk-ant-key-aaaa'), pooled('b', 'sk-ant-key-bbbb')]),
        reporter: () => () => Promise.resolve(),
      },
      probe: () => Promise.resolve({ verdict: 'refused', reason: 'HTTP 401 again' }),
    })
    const { session } = await classic.start(browser, { origin: 'chat' })
    const turn = await classic.send(session.id, browser, 'fetch it')
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(attempts).toBe(2)
    expect(used).toEqual(['sk-ant-key-aaaa', 'sk-ant-key-bbbb'])
    classic.abortAll()
  })

  it("refuses WebFetch's model call when the turn has no credential to be on (#3508 review)", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'web-pkg-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'web' }))
    const pkg = { name: 'web', contentHash: (await hashTree(dir)).hash, dir, allowed: true, builtinTools: ['WebFetch'] }
    let credential: (() => Promise<unknown>) | undefined
    let ran = false
    const classic = manager({
      sql: db.sql,
      paths: await tempPaths(),
      packagePlugins: async () => ({ paths: [], allowedPaths: [dir], builtinTools: pkg.builtinTools, loaded: [pkg], problems: [], release: () => {} }),
      run: () =>
        (async function* () {
          ran = true
          yield* []
        })(),
      nestedQueries: (deps) => {
        credential = deps.credential
        return { ask: () => Promise.resolve(''), search: () => Promise.reject(new Error('unused')) }
      },
      // An empty pool: the turn starts no attempt, so it is on no credential.
      credentials: { candidates: () => Promise.resolve([]), reporter: () => () => Promise.resolve() },
    })
    const { session } = await classic.start(browser, { origin: 'chat' })
    const turn = await classic.send(session.id, browser, 'fetch it')
    // The turn itself ends on that cause, not quietly (#3933).
    expect(await turn.done).toEqual({ kind: 'failed', message: 'no Claude credential is usable' })
    expect(ran).toBe(false)
    expect(credential).toBeDefined()
    await expect(credential!()).rejects.toBeInstanceOf(NoUsableCredentialError)
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

  it("counts WebFetch's own model calls in the cost of an AgentSession turn: its result, the row and its audit row (#2684)", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'web-pkg-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'web' }))
    const pkg = { name: 'web', contentHash: (await hashTree(dir)).hash, dir, allowed: true, builtinTools: ['WebFetch'] }
    let spend: ((cost: number) => Promise<void>) | undefined
    const scripted = scriptedRunner(() => ({ reply: 'ok', costUsd: 0.01 }))
    const unified = manager({
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
      audit: new AuditLog({ sql: db.sql }),
    })
    const { session } = await unified.start(browser, { origin: 'chat' })
    // The row runs the turn as DurableTurns.send claims it; session_turn runs it here as TurnHost.
    const turnId = randomUUID()
    await db.sql`
      UPDATE ai_sessions SET status = 'running', mode = 'durable', runtime = 'unified', durable_turn_id = ${turnId}, updated_at = now()
      WHERE id = ${session.id}`
    const host = new TurnHost({ core: unified.turnCore, sql: db.sql, images: new SessionBlobs(db.sql), hostQueue: 'agent-sessions.pod-a' })
    const result = await host.run(
      { sessionId: session.id, turnId, attempt: 'run:0', message: { turn_id: turnId, author: browser, text: 'fetch it', images: [], system_append: null } },
      { heartbeat: () => {}, cancelled: new AbortController().signal, cancelReason: () => 'interrupt' },
    )
    // Each of the three places on its own (soft): the turn's result, as session.finish_turn writes it.
    expect(result).toMatchObject({ status: 'done' })
    expect.soft(result.tail?.find((e) => e.type === 'session.result')).toMatchObject({ costUsd: expect.closeTo(0.06) })
    const [row] = await db.sql<{ cost_usd: string; unpriced_cost_usd: string }[]>`
      SELECT cost_usd, unpriced_cost_usd FROM ai_sessions WHERE id = ${session.id}`
    expect.soft(Number(row!.cost_usd)).toBeCloseTo(0.06)
    expect.soft(Number(row!.unpriced_cost_usd)).toBeCloseTo(0.05)
    // Its audit row (#1922), as a classic turn's.
    const audit = await db.sql<{ cost_usd: string; cost_estimated_usd: string }[]>`
      SELECT cost_usd, cost_estimated_usd FROM ai_audit WHERE session_id = ${session.id} AND kind = 'turn'`
    expect.soft(audit.map((a) => [Number(a.cost_usd), Number(a.cost_estimated_usd)])).toEqual([[expect.closeTo(0.06), expect.closeTo(0.05)]])
    await host.stop({ graceMs: 0, abortWaitMs: 0 })
    unified.abortAll()
  })

  it("recycles the next turn's warm process for its budget after WebFetch spend, and logs why (#3597)", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'web-pkg-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'web' }))
    const pkg = { name: 'web', contentHash: (await hashTree(dir)).hash, dir, allowed: true, builtinTools: ['WebFetch'] }
    let spend: ((cost: number) => Promise<void>) | undefined
    let fetches = 1
    const logs: string[] = []
    const scripted = scriptedRunner(() => ({ reply: 'ok', costUsd: 0.01 }))
    const classic = manager({
      sql: db.sql,
      paths: await tempPaths(),
      packagePlugins: async () => ({ paths: [], allowedPaths: [dir], builtinTools: pkg.builtinTools, loaded: [pkg], problems: [], release: () => {} }),
      // Only the first turn's WebFetch makes a nested query; the warm process's cap never saw it.
      run: (run) =>
        (async function* () {
          if (fetches-- > 0) await spend!(0.05)
          yield* scripted.runner(run)
        })(),
      nestedQueries: (deps) => {
        spend = deps.spent
        return { ask: () => Promise.resolve(''), search: () => Promise.reject(new Error('unused')) }
      },
      warm: { log: (m) => logs.push(m) },
    })
    const { session } = await classic.start(browser, { origin: 'chat' })
    expect(await (await classic.send(session.id, browser, 'fetch it')).done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(await (await classic.send(session.id, browser, 'again')).done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(logs).toContainEqual(expect.stringMatching(/closed session .*'s process: recycled: budget/))
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
