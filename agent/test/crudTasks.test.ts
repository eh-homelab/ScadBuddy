import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { CrudAreas, TASK_AREAS } from '../src/api/crudAreas.js'
import { CrudReadError, wellFormed } from '../src/api/crudRead.js'
import { BackgroundTasks, type TaskRunner } from '../src/background/service.js'
import { BackgroundTaskStore, CrudBackgroundTasks, NOTICE_MAX_CHARS } from '../src/background/store.js'
import { type BackgroundContext, taskStop } from '../src/builtins/background.js'
import { CrudOperations, OperationStore } from '../src/operations/store.js'

// A session's background tasks and the agent's command record through the API's CRUD routes
// (#5299 step 8), against a fake API that answers as the backend's api/crud_agent_tasks.py does
// (the backend's tests/crud/test_crud_agent_tasks.py covers the routes and their SQL). The stores
// keep their interfaces: each method goes to its route once the area is listed, and to its own
// SQL until then.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b0e7c55-6a3e-4a37-9d3c-4b1a1f0e2a10'
const OWNER = { kind: 'browser', id: 'browser', label: 'You' } as const
const KEY = 'ab'.repeat(32)

const WIRE_TASK = {
  id: '0000abcd',
  session_id: SESSION,
  kind: 'cron',
  description: 'every minute',
  spec: { cron: '* * * * *' },
  created_by: OWNER,
  state: 'active',
  next_at: '2026-11-15T08:00:00Z',
  fired: 2,
  last_fired_at: null,
  expires_at: null,
  detail: null,
  created_at: '2026-11-15T07:00:00.123456Z',
  updated_at: '2026-11-15T07:30:00Z',
}

const WIRE_OPERATION = {
  id: 'o1',
  kind: 'plugin_package_install',
  subject: 'hindsight',
  status: 'running',
  result: null,
  error: null,
  created_at: '2026-11-15T07:00:00.123456Z',
  finished_at: null,
  workflow_id: 'agent-op-o1',
  workflow_run_id: 'run1',
}

type Seen = { method: string; path: string; body: unknown }

/** The API's side: each request recorded, each answered from `answers` by `METHOD path` (query included). */
class FakeApi {
  seen: Seen[] = []
  answers = new Map<string, unknown>()
  /** Answer without the CRUD header (a proxy, or an API older than the routes). */
  notCrud = false

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const url = new URL(request.url)
    const path = `${url.pathname.slice(PREFIX.length)}${url.search}`
    const text = request.method === 'POST' ? await request.text() : ''
    this.seen.push({ method: request.method, path, body: text ? JSON.parse(text) : undefined })
    if (this.notCrud) return new Response('not found', { status: 404 })
    const key = `${request.method} ${path}`
    if (!this.answers.has(key)) return new Response(JSON.stringify({ detail: `no answer for ${key}` }), { status: 404, headers: { 'x-scadbuddy-crud': '1' } })
    return new Response(JSON.stringify(this.answers.get(key)), { status: 200, headers: { 'x-scadbuddy-crud': '1' } })
  }

  answer(key: string, body: unknown): this {
    this.answers.set(key, body)
    return this
  }
}

/** A Sql that fails the test if a store runs any SQL on it. */
const noSql = new Proxy(() => {}, {
  apply: () => {
    throw new Error('ran SQL')
  },
  get: () => {
    throw new Error('ran SQL')
  },
}) as unknown as Sql

describe('the background-tasks and operations areas', () => {
  it('are among the areas the agent waits for', async () => {
    expect(TASK_AREAS).toEqual(['background-tasks', 'operations'])
    const api = new FakeApi().answer('GET /areas', { areas: ['settings', 'background-tasks'] })
    const areas = new CrudAreas(BASE, { fetch: api.fetch, wanted: TASK_AREAS, log: () => {} })
    expect(await areas.refresh()).toBe(false)
    expect(areas.has('background-tasks')).toBe(true)
    expect(areas.has('operations')).toBe(false)
  })
})

describe("a session's background tasks through the CRUD routes", () => {
  const store = (api: FakeApi, listed: { on: boolean }) =>
    new BackgroundTaskStore(noSql, { crud: new CrudBackgroundTasks(BASE, { fetch: api.fetch }), use: () => listed.on })

  it('runs its own SQL until the area is listed, then only the routes', async () => {
    const api = new FakeApi().answer('GET /background-tasks/0000abcd', { task: WIRE_TASK })
    const listed = { on: false }
    const tasks = store(api, listed)
    await expect(tasks.get('0000abcd')).rejects.toThrow('ran SQL')
    expect(api.seen).toEqual([])
    listed.on = true
    const task = await tasks.get('0000abcd')
    expect(task).toEqual({
      id: '0000abcd',
      sessionId: SESSION,
      kind: 'cron',
      description: 'every minute',
      spec: { cron: '* * * * *' },
      createdBy: OWNER,
      state: 'active',
      nextAt: new Date('2026-11-15T08:00:00Z'),
      fired: 2,
      lastFiredAt: null,
      expiresAt: null,
      detail: null,
      createdAt: new Date('2026-11-15T07:00:00.123Z'),
      updatedAt: new Date('2026-11-15T07:30:00Z'),
    })
  })

  it('creates a task, or none at the cap, and reads and lists them', async () => {
    const api = new FakeApi()
      .answer('POST /background-tasks', { task: WIRE_TASK })
      .answer(`GET /background-tasks?session_id=${SESSION}`, { tasks: [WIRE_TASK] })
      .answer(`GET /background-tasks?session_id=${SESSION}&all=true`, { tasks: [] })
      .answer('GET /background-tasks/recurring-crons', { tasks: [WIRE_TASK] })
      .answer('GET /background-tasks/ffffffff', { task: null })
    const tasks = store(api, { on: true })
    const made = await tasks.create({
      sessionId: SESSION,
      kind: 'cron',
      description: 'every minute',
      spec: { cron: '* * * * *' },
      createdBy: OWNER,
      nextAt: new Date('2026-11-15T08:00:00Z'),
    })
    expect(made?.id).toBe('0000abcd')
    expect(api.seen[0]).toEqual({
      method: 'POST',
      path: '/background-tasks',
      body: {
        session_id: SESSION,
        kind: 'cron',
        description: 'every minute',
        spec: { cron: '* * * * *' },
        created_by: OWNER,
        next_at: '2026-11-15T08:00:00.000Z',
        expires_at: null,
      },
    })
    api.answer('POST /background-tasks', { task: null })
    expect(await tasks.create({ sessionId: SESSION, kind: 'wakeup', description: 'x', spec: {}, createdBy: OWNER })).toBeUndefined()
    expect((await tasks.list(SESSION)).map((t) => t.id)).toEqual(['0000abcd'])
    expect(await tasks.list(SESSION, { all: true })).toEqual([])
    expect((await tasks.activeRecurringCrons())[0]?.nextAt).toEqual(new Date('2026-11-15T08:00:00Z'))
    expect(await tasks.get('ffffffff')).toBeUndefined()
  })

  it("sends a task's writes to its own routes", async () => {
    const api = new FakeApi()
      .answer('POST /background-tasks/0000abcd/scheduled', { ok: true })
      .answer('POST /background-tasks/0000abcd/fired', { ok: true })
      .answer('POST /background-tasks/0000abcd/finish', { finished: false })
      .answer('POST /background-tasks/0000abcd/spec', { ok: true })
      .answer('POST /background-tasks/0000abcd/progress', { ok: true })
    const tasks = store(api, { on: true })
    await tasks.scheduled('0000abcd', new Date('2026-11-15T09:00:00Z'))
    await tasks.fired('0000abcd', new Date('2026-11-15T09:00:01Z'))
    expect(await tasks.finish('0000abcd', 'cancelled', 'cancelled by You')).toBe(false)
    await tasks.finish('0000abcd', 'done')
    await tasks.setSpec('0000abcd', { scriptPath: 'a.js' })
    await tasks.progress('0000abcd', 'phase 2')
    expect(api.seen.map((s) => [s.path, s.body])).toEqual([
      ['/background-tasks/0000abcd/scheduled', { at: '2026-11-15T09:00:00.000Z' }],
      ['/background-tasks/0000abcd/fired', { at: '2026-11-15T09:00:01.000Z' }],
      ['/background-tasks/0000abcd/finish', { state: 'cancelled', detail: 'cancelled by You' }],
      ['/background-tasks/0000abcd/finish', { state: 'done', detail: null }],
      ['/background-tasks/0000abcd/spec', { spec: { scriptPath: 'a.js' } }],
      ['/background-tasks/0000abcd/progress', { detail: 'phase 2' }],
    ])
  })

  it("records a monitor's events, cut to the column's cap and well-formed, and delivers them", async () => {
    const api = new FakeApi()
      .answer('POST /background-tasks/0000abcd/events', { appended: false })
      .answer(`GET /background-tasks/0000abcd/events/pending?max_chars=20`, { events: [{ seq: '9', text: 'a' }, { seq: '10', text: 'b' }] })
      .answer('GET /background-tasks/0000abcd/events/pending?max_chars=64000', { events: [] })
      .answer('GET /background-tasks/0000abcd/events/count', { count: 7 })
      .answer('GET /background-tasks/0000abcd/events/count?pending=true', { count: 3 })
      .answer('POST /background-tasks/0000abcd/events/delivered', { ok: true })
    const tasks = store(api, { on: true })
    expect(await tasks.appendEvent('0000abcd', 'x'.repeat(NOTICE_MAX_CHARS + 10))).toBe(false)
    // A JavaScript string cut between the halves of a pair.
    await tasks.appendNotice('0000abcd', 'done \ud83d')
    expect(api.seen[0]?.body).toEqual({ text: 'x'.repeat(NOTICE_MAX_CHARS) })
    expect(api.seen[1]?.body).toEqual({ text: 'done �', notice: true })
    expect(await tasks.pendingEvents('0000abcd', 20)).toEqual([
      { seq: '9', text: 'a' },
      { seq: '10', text: 'b' },
    ])
    expect(await tasks.pendingEvents('0000abcd')).toEqual([])
    expect(await tasks.eventCount('0000abcd')).toBe(7)
    expect(await tasks.pendingCount('0000abcd')).toBe(3)
    await tasks.delivered('0000abcd', '10')
    expect(api.seen.at(-1)).toEqual({ method: 'POST', path: '/background-tasks/0000abcd/events/delivered', body: { up_to: '10' } })
  })

  it("keeps a Workflow's journal and child chats", async () => {
    const child = '5d1c0e8a-0f6b-4c1e-9b1a-2f6a1c3d4e5f'
    const api = new FakeApi()
      .answer(`GET /background-tasks/0000abcd/calls?key=${KEY}&occurrence=0`, { call: { result: null, ok: true } })
      .answer(`GET /background-tasks/0000abcd/calls?key=${KEY}&occurrence=1`, { call: null })
      .answer('POST /background-tasks/0000abcd/calls', { ok: true })
      .answer('POST /background-tasks/0000abcd/children', { ok: true })
      .answer('GET /background-tasks/0000abcd/children/open', { children: [child] })
    const tasks = store(api, { on: true })
    // A null result is a journalled call too.
    expect(await tasks.journalled('0000abcd', KEY, 0)).toEqual({ result: null, ok: true })
    expect(await tasks.journalled('0000abcd', KEY, 1)).toBeUndefined()
    await tasks.journal('0000abcd', KEY, 1, { result: { a: 1 }, ok: false, childId: child })
    await tasks.journal('0000abcd', KEY, 2, { result: 'x', ok: true })
    await tasks.recordChild('0000abcd', child)
    expect(await tasks.openChildren('0000abcd')).toEqual([child])
    expect(api.seen.filter((s) => s.method === 'POST').map((s) => s.body)).toEqual([
      { key: KEY, occurrence: 1, result: { a: 1 }, ok: false, child_id: child },
      { key: KEY, occurrence: 2, result: 'x', ok: true, child_id: null },
      { child_id: child },
    ])
  })

  it.each(['wf_0a1b2c3d', '', 'bash_1', '0000ABCD', '0000abcd0'])(
    'reads %j, which names no task, as no task, with no call (#6374 review)',
    async (id) => {
      // As the SQL found no row: TaskStop's "No task found" and the panel's 404 stand.
      const api = new FakeApi()
      const tasks = store(api, { on: true })
      expect(await tasks.get(id)).toBeUndefined()
      expect(await tasks.finish(id, 'cancelled', 'cancelled by You')).toBe(false)
      expect(await tasks.pendingEvents(id)).toEqual([])
      expect(await tasks.eventCount(id)).toBe(0)
      expect(await tasks.pendingCount(id)).toBe(0)
      expect(await tasks.journalled(id, KEY, 0)).toBeUndefined()
      expect(await tasks.openChildren(id)).toEqual([])
      await tasks.scheduled(id, new Date())
      await tasks.fired(id, new Date())
      await tasks.progress(id, 'x')
      await tasks.setSpec(id, {})
      await tasks.delivered(id, '1')
      expect(api.seen).toEqual([])
    },
  )

  it.each(['wf_0a1b2c3d', '', 'bash_1', '0000ABCD', '0000abcd0'])(
    'throws on a write that inserts for %j, which names no task, as the SQL did on the foreign key (#6374 review)',
    async (id) => {
      // The route refuses an id that is not 8 hex characters (422); the client sends it as it is.
      const paths: string[] = []
      const refusing: typeof fetch = async (input) => {
        paths.push(new URL(new Request(input).url).pathname.slice(PREFIX.length))
        return new Response(JSON.stringify({ detail: 'task_id' }), { status: 422, headers: { 'x-scadbuddy-crud': '1' } })
      }
      const tasks = new BackgroundTaskStore(noSql, { crud: new CrudBackgroundTasks(BASE, { fetch: refusing }), use: () => true })
      await expect(tasks.appendEvent(id, 'x')).rejects.toThrow()
      await expect(tasks.appendNotice(id, 'x')).rejects.toThrow()
      await expect(tasks.journal(id, KEY, 0, { result: 'x', ok: true })).rejects.toThrow()
      await expect(tasks.recordChild(id, '5d1c0e8a-0f6b-4c1e-9b1a-2f6a1c3d4e5f')).rejects.toThrow()
      const at = `/background-tasks/${encodeURIComponent(id)}`
      expect(paths).toEqual([`${at}/events`, `${at}/events`, `${at}/calls`, `${at}/children`])
    },
  )

  it.each([{ task_id: 'wf_0a1b2c3d' }, { shell_id: 'bash_1' }, {}])(
    "answers TaskStop %j with Claude Code's No task found, not a backend error (#6374 review)",
    async (input) => {
      const api = new FakeApi()
      const runner: TaskRunner = { start: async () => undefined, cancel: async () => {}, terminate: async () => {} }
      const tasks = new BackgroundTasks({ store: store(api, { on: true }), runner })
      const ctx = { tasks, sessionId: SESSION } as unknown as BackgroundContext
      const id = 'task_id' in input ? input.task_id : 'shell_id' in input ? input.shell_id : ''
      expect(await taskStop(ctx, input)).toMatchObject({ isError: true, text: `No task found with ID: ${id}` })
      expect(api.seen).toEqual([])
    },
  )

  it('refuses an answer that is not the route\'s', async () => {
    const api = new FakeApi()
    api.notCrud = true
    await expect(store(api, { on: true }).get('0000abcd')).rejects.toBeInstanceOf(CrudReadError)
  })
})

describe("the agent's command record through the CRUD routes", () => {
  const store = (api: FakeApi, listed: { on: boolean }) =>
    new OperationStore(noSql, { crud: new CrudOperations(BASE, { fetch: api.fetch }), use: () => listed.on })

  it('runs its own SQL until the area is listed, then reads an operation and its execution', async () => {
    const api = new FakeApi()
      .answer('GET /operations/o1', { operation: WIRE_OPERATION })
      .answer(`GET /operations/by-key/${KEY}`, { operation: WIRE_OPERATION })
      .answer('GET /operations/none', { operation: null })
    const listed = { on: false }
    const operations = store(api, listed)
    await expect(operations.get('o1')).rejects.toThrow('ran SQL')
    listed.on = true
    const view = {
      id: 'o1',
      kind: 'plugin_package_install',
      subject: 'hindsight',
      status: 'running',
      result: null,
      error: null,
      created_at: '2026-11-15T07:00:00.123Z',
      finished_at: null,
    }
    expect(await operations.get('o1')).toEqual(view)
    expect(await operations.find(KEY)).toEqual(view)
    expect(await operations.execution('o1')).toEqual({ workflowId: 'agent-op-o1', workflowRunId: 'run1' })
    expect(await operations.get('none')).toBeUndefined()
    expect(await operations.execution('none')).toBeUndefined()
  })

  it('records an execution and ends it', async () => {
    const finished = { ...WIRE_OPERATION, status: 'failed', error: { status: 409, title: 'Conflict', detail: 'no' }, finished_at: '2026-11-15T07:01:00Z' }
    const api = new FakeApi()
      .answer('POST /operations', { operation: WIRE_OPERATION })
      .answer('POST /operations/o1/finish', { operation: finished })
    const operations = store(api, { on: true })
    const op = await operations.insert(
      {
        id: 'o1',
        kind: 'plugin_package_install',
        subject: 'hindsight',
        operationKey: KEY,
        request: { source: 'x' },
        workflowId: 'agent-op-o1',
        workflowRunId: 'run1',
        actor: { principal: 'token:t1', viaTool: 'install' },
      },
      3600,
    )
    expect(op.status).toBe('running')
    expect(api.seen[0]?.body).toEqual({
      id: 'o1',
      kind: 'plugin_package_install',
      subject: 'hindsight',
      operation_key: KEY,
      request: { source: 'x' },
      workflow_id: 'agent-op-o1',
      workflow_run_id: 'run1',
      actor: { principal: 'token:t1', session: null, via_tool: 'install' },
      retention_seconds: 3600,
    })
    const ended = await operations.finish('o1', { error: { status: 409, title: 'Conflict', detail: 'no' } })
    expect([ended.status, ended.finished_at]).toEqual(['failed', '2026-11-15T07:01:00.000Z'])
    await operations.finish('o1', { result: undefined })
    expect(api.seen.slice(1).map((s) => s.body)).toEqual([{ error: { status: 409, title: 'Conflict', detail: 'no' } }, { result: null }])
  })
})

describe('wellFormed', () => {
  it('replaces lone surrogates in every string of a body, and keeps the rest', () => {
    expect(wellFormed({ a: ['ok \ud83d', 1, null, { b: '\udc00x' }], c: true, d: '😀' })).toEqual({
      a: ['ok �', 1, null, { b: '�x' }],
      c: true,
      d: '😀',
    })
  })

  it("replaces lone surrogates in an object's keys too (#6374 review)", () => {
    // A spec or a journalled result from the model can carry one in a key.
    expect(wellFormed({ 'k \ud83d': { '\udc00': 1 }, '😀': 2 })).toEqual({ 'k �': { '�': 1 }, '😀': 2 })
  })
})
