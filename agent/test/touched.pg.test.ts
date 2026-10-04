import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { EXTRACTORS, type Extractor, MAX_TOUCHES_PER_CALL, SessionResources } from '../src/sessions/touched.js'
import { harnessPrincipal } from '../src/auth/principal.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool } from '../src/tools/registry.js'
import { SERVER_NAME } from '../src/tools/projections.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, agentB, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// What a session touched (#931, src/sessions/touched.ts) in Postgres: a
// session's tool calls through the harness projection land in
// ai_session_resources, and GET /api/v1/ai/sessions/:id/resources reads them
// back to whoever may see the session.

const C1 = '1'.repeat(40)
const C2 = '2'.repeat(40)
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

const backend = setupServer()
beforeAll(() => backend.listen({ onUnhandledRequest: 'error' }))
afterEach(() => backend.resetHandlers())
afterAll(() => backend.close())

describe.skipIf(!TEST_DATABASE_URL)(`session resources in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  let store: SessionResources
  const failures: unknown[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    failures.length = 0
    store = new SessionResources(db.sql, (err) => failures.push(err))
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner })
  })
  afterEach(async () => {
    m.abortAll()
    expect(failures).toEqual([])
    await drop()
  })

  async function harness(session: string) {
    const wired = harnessTools(services({ touched: store }))
    const servers = wired.mcpServers({ id: session, owner: browser })
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await servers[SERVER_NAME]!.instance.connect(serverSide)
    const mcp = new Client({ name: 'touched-pg-test', version: '0' })
    await mcp.connect(clientSide)
    return mcp
  }

  it('records an edit → render → save output → print session, in order', async () => {
    backend.use(
      http.put(`${BACKEND}/api/v1/models/box/source`, () => HttpResponse.json({ slug: 'box', version: C2 })),
      http.get(`${BACKEND}/api/v1/models/box/schema`, () => HttpResponse.json({ groups: [], parameters: [] })),
      http.post(`${BACKEND}/api/v1/models/box/render`, () =>
        HttpResponse.json({ job_id: 'job-1', status_url: '/api/v1/jobs/job-1' }, { status: 202 }),
      ),
      http.get(`${BACKEND}/api/v1/jobs/job-1`, () =>
        HttpResponse.json({ id: 'job-1', slug: 'box', status: 'done', model_version: C2, params: {} }),
      ),
      http.post(`${BACKEND}/api/v1/models/box/outputs`, () =>
        HttpResponse.json({ id: 'out-1', slug: 'box', params: {} }, { status: 201 }),
      ),
      http.post(`${BACKEND}/api/v1/prints/7/reprint`, () =>
        HttpResponse.json({ queue_item_id: 12, printer_id: 1, bambuddy_url: 'http://bambuddy.test/queue' }),
      ),
      http.get(`${BACKEND}/api/v1/models/box`, () => HttpResponse.json({ slug: 'box', version: C2 })),
    )
    const { session } = await m.start(browser, { origin: 'chat', title: 'bigger box' })
    const mcp = await harness(session.id)
    const calls: [string, Record<string, unknown>][] = [
      ['update_source', { slug: 'box', source: 'cube(2);', base: C1 }],
      ['render_model', { slug: 'box', save_output: true }],
      ['print_again', { archive_id: 7 }],
      // A read touches nothing.
      ['get_model', { slug: 'box' }],
    ]
    for (const [name, args] of calls) {
      const result = await mcp.callTool({ name, arguments: args })
      expect(result.isError ?? false, JSON.stringify(result)).toBe(false)
    }
    await mcp.close()

    const resources = await m.resources(session.id, browser)
    expect(resources.map(({ at: _at, ...rest }) => rest)).toEqual([
      { type: 'revision', id: C2, action: 'created', model: 'box', before: C1, after: C2, tool: 'update_source' },
      { type: 'render_job', id: 'job-1', action: 'created', model: 'box', before: null, after: C2, tool: 'render_model' },
      { type: 'output', id: 'out-1', action: 'created', model: 'box', before: null, after: null, tool: 'render_model' },
      { type: 'print', id: '12', action: 'created', model: null, before: '7', after: null, tool: 'print_again' },
    ])
    expect(resources.every((r) => !Number.isNaN(Date.parse(r.at)))).toBe(true)
  })

  it('records a write with no extractor as unclassified, naming the tool', async () => {
    const { session } = await m.start(browser, { origin: 'chat' })
    await store.record({ sessionId: session.id, tool: { name: 'set_print_options', risk: 'write' }, input: {}, result: { content: [] } })
    expect(await m.resources(session.id, browser)).toMatchObject([
      { type: 'unclassified', id: null, action: 'modified', tool: 'set_print_options' },
    ])
  })

  it('cuts a long id between code points, never inside a surrogate pair', async () => {
    const { session } = await m.start(browser, { origin: 'chat' })
    // 299 units then a two-unit emoji: a 300-unit cut would keep half of it.
    const long = `${'x'.repeat(299)}😀tail`
    await store.record({
      sessionId: session.id,
      tool: { name: 'delete_model', risk: 'outward' },
      input: { slug: long },
      result: { content: [] },
    })
    const [row] = await m.resources(session.id, browser)
    expect(row!.id).toBe('x'.repeat(299))
  })

  it('records at most MAX_TOUCHES_PER_CALL rows for one call, the last saying the rest were dropped', async () => {
    const { session } = await m.start(browser, { origin: 'chat' })
    const items = Array.from({ length: MAX_TOUCHES_PER_CALL + 50 }, (_, i) => i + 1)
    await store.record({
      sessionId: session.id,
      tool: { name: 'print_output', risk: 'outward' },
      input: { output_id: 'o1' },
      result: { content: [{ type: 'text', text: JSON.stringify({ id: 'r1', result: { queue_item_ids: items } }) }] },
    })
    const rows = await m.resources(session.id, browser)
    expect(rows).toHaveLength(MAX_TOUCHES_PER_CALL)
    expect(rows[0]).toMatchObject({ type: 'print_run', id: 'r1' })
    expect(rows.at(-1)).toMatchObject({ type: 'unclassified', id: null, tool: 'print_output' })
  })

  it('reports a row it cannot write, and never throws', async () => {
    const errors: unknown[] = []
    const broken = new SessionResources(db.sql, (err) => errors.push(err))
    // No such session: the foreign key refuses the row.
    await broken.record({
      sessionId: '33333333-3333-4333-8333-333333333333',
      tool: { name: 'set_print_options', risk: 'write' },
      input: {},
      result: { content: [] },
    })
    expect(errors).toHaveLength(1)
    expect(await db.sql`SELECT 1 FROM ai_session_resources`).toHaveLength(0)
  })

  it('reports a row the CHECK refuses through onError, and writes none of the call', async () => {
    const { session } = await m.start(browser, { origin: 'chat' })
    const errors: unknown[] = []
    const broken = new SessionResources(db.sql, (err) => errors.push(err))
    // An action outside the CHECK: the whole call's INSERT is refused, the valid row with it.
    const bad = { content: [{ type: 'text' as const, text: JSON.stringify({ id: 'p1' }) }] }
    const original = EXTRACTORS.save_preset!
    ;(EXTRACTORS as Record<string, Extractor>).save_preset = (input, result) => [
      ...original(input, result),
      { type: 'preset', id: 'p2', action: 'renamed' as never },
    ]
    try {
      await broken.record({ sessionId: session.id, tool: { name: 'save_preset', risk: 'write' }, input: { slug: 'box' }, result: bad })
    } finally {
      ;(EXTRACTORS as Record<string, Extractor>).save_preset = original
    }
    expect(errors).toHaveLength(1)
    expect(String(errors[0])).toMatch(/check/i)
    expect(await m.resources(session.id, browser)).toEqual([])
  })

  it("goes with its session, and is read only by those who may see the session", async () => {
    const { session } = await m.start(agentA, { origin: 'mcp' })
    await store.record({ sessionId: session.id, tool: { name: 'set_print_options', risk: 'write' }, input: {}, result: { content: [] } })
    // The browser user sees every session (spec §6); another agent does not.
    expect(await m.resources(session.id, browser)).toHaveLength(1)
    await expect(m.resources(session.id, { kind: 'bearer', id: 'token:b', label: 'B' })).rejects.toThrow(/no session/)

    await db.sql`DELETE FROM ai_sessions WHERE id = ${session.id}::uuid`
    expect(await db.sql`SELECT 1 FROM ai_session_resources`).toHaveLength(0)
  })

  it('answers sessions_resources to whoever may see the session', async () => {
    const { session } = await m.start(agentA, { origin: 'mcp' })
    await store.record({ sessionId: session.id, tool: { name: 'set_print_options', risk: 'write' }, input: {}, result: { content: [] } })
    const tool = ALL_TOOLS.find((t) => t.name === 'sessions_resources')!
    const call = (owner: typeof agentA) =>
      runTool(tool, { session_id: session.id }, {
        ...services({ sessions: m }),
        principal: harnessPrincipal(owner),
        progress: async () => {},
        signal: new AbortController().signal,
      })
    expect(firstText(await call(agentA))).toMatchObject({ resources: [{ type: 'unclassified', tool: 'set_print_options' }] })
    const other = await call({ kind: 'bearer', id: 'token:b', label: 'B' })
    expect(other.isError).toBe(true)
  })

  it('serves GET /api/v1/ai/sessions/:id/resources to the UI', async () => {
    const app = createApp({
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      approvals: m.approvals,
      sessions: m,
    })
    const { session } = await m.start(browser, { origin: 'chat' })
    await store.record({
      sessionId: session.id,
      tool: { name: 'delete_model', risk: 'outward' },
      input: { slug: 'box' },
      result: { content: [] },
    })
    const res = await app.request(`/api/v1/ai/sessions/${session.id}/resources`, { headers: UI_READ })
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({
      resources: [{ type: 'model', id: 'box', action: 'deleted', model: 'box', tool: 'delete_model' }],
    })
    const missing = await app.request('/api/v1/ai/sessions/44444444-4444-4444-8444-444444444444/resources', {
      headers: UI_READ,
    })
    expect(missing.status).toBe(404)
    // Not the UI's origin: refused like every other session read.
    const foreign = await app.request(`/api/v1/ai/sessions/${session.id}/resources`, {
      headers: { ...UI_READ, 'sec-fetch-site': 'cross-site' },
    })
    expect(foreign.status).toBe(403)
  })

  // The reverse direction (#931): which sessions touched a resource.
  describe('sessions that touched a resource', () => {
    const touch = (sessionId: string, name: string, input: Record<string, unknown>, result: unknown = {}) =>
      store.record({
        sessionId,
        tool: { name, risk: 'write' },
        input,
        result: { content: [{ type: 'text', text: JSON.stringify(result) }] },
      })

    async function seed() {
      const edit = (await m.start(agentA, { origin: 'mcp', title: 'edit box' })).session
      await touch(edit.id, 'update_source', { slug: 'box', base: C1 }, { slug: 'box', version: C2 })
      const preset = (await m.start(browser, { origin: 'chat', title: 'preset on box' })).session
      await touch(preset.id, 'save_preset', { slug: 'box' }, { id: 'p1' })
      const output = (await m.start(agentB, { origin: 'mcp', title: 'save an output' })).session
      await touch(output.id, 'save_output', { slug: 'lid' }, { id: 'out-1', slug: 'lid' })
      const other = (await m.start(browser, { origin: 'chat', title: 'unrelated' })).session
      await touch(other.id, 'delete_model', { slug: 'boxes' })
      return { edit, preset, output, other }
    }

    function app() {
      return createApp({
        database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
        backend: () => Promise.resolve(true),
        kek: { ok: false, reason: 'unused' },
        credentials: new MemoryCredentials(),
        testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
        remoteAddress: () => '10.0.0.7',
        origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
        approvals: m.approvals,
        sessions: m,
      })
    }

    it('a model matches every row of that model; any other kind matches its id', async () => {
      const { edit, preset, output } = await seed()
      const ids = async (resource: { type: 'model' | 'output' | 'revision' | 'preset'; id: string }) =>
        (await m.list(browser, { resource })).map((s) => s.id).sort()
      expect(await ids({ type: 'model', id: 'box' })).toEqual([edit.id, preset.id].sort())
      expect(await ids({ type: 'revision', id: C2 })).toEqual([edit.id])
      expect(await ids({ type: 'preset', id: 'p1' })).toEqual([preset.id])
      expect(await ids({ type: 'output', id: 'out-1' })).toEqual([output.id])
      expect(await ids({ type: 'output', id: 'out-2' })).toEqual([])
    })

    it('lists a session once however many times it touched the resource', async () => {
      const { edit } = await seed()
      await touch(edit.id, 'update_source', { slug: 'box', base: C2 }, { slug: 'box', version: C1 })
      expect((await m.list(browser, { resource: { type: 'model', id: 'box' } })).filter((s) => s.id === edit.id)).toHaveLength(1)
    })

    it('keeps visibility: an agent sees only the sessions it may see', async () => {
      const { edit } = await seed()
      expect((await m.list(agentA, { resource: { type: 'model', id: 'box' } })).map((s) => s.id)).toEqual([edit.id])
      expect(await m.list(agentB, { resource: { type: 'model', id: 'box' } })).toEqual([])
    })

    it('serves GET /api/v1/ai/resources/:type/:id/sessions to the UI', async () => {
      const { edit, preset } = await seed()
      const res = await app().request('/api/v1/ai/resources/model/box/sessions', { headers: UI_READ })
      expect(res.status).toBe(200)
      const body = (await res.json()) as { sessions: { id: string; title: string }[] }
      expect(body.sessions.map((s) => s.id).sort()).toEqual([edit.id, preset.id].sort())
      expect(body.sessions.find((s) => s.id === edit.id)).toMatchObject({ title: 'edit box', owner: { kind: 'bearer', id: 'token:a' } })

      const limited = await app().request('/api/v1/ai/resources/model/box/sessions?limit=1', { headers: UI_READ })
      expect(((await limited.json()) as { sessions: unknown[] }).sessions).toHaveLength(1)

      // An id with reserved characters arrives percent-encoded and is matched decoded.
      const odd = (await m.start(browser, { origin: 'chat' })).session
      await touch(odd.id, 'save_output', { slug: 'box' }, { id: 'out/7 a', slug: 'box' })
      const encoded = await app().request(`/api/v1/ai/resources/output/${encodeURIComponent('out/7 a')}/sessions`, { headers: UI_READ })
      expect(((await encoded.json()) as { sessions: { id: string }[] }).sessions.map((s) => s.id)).toEqual([odd.id])

      for (const type of ['unclassified', 'nope']) {
        const bad = await app().request(`/api/v1/ai/resources/${type}/box/sessions`, { headers: UI_READ })
        expect(bad.status, type).toBe(400)
      }
      const foreign = await app().request('/api/v1/ai/resources/model/box/sessions', {
        headers: { ...UI_READ, 'sec-fetch-site': 'cross-site' },
      })
      expect(foreign.status).toBe(403)
    })

    it('filters GET /api/v1/ai/sessions by resource_type and resource_id, together only', async () => {
      const { output } = await seed()
      const res = await app().request('/api/v1/ai/sessions?resource_type=output&resource_id=out-1', { headers: UI_READ })
      expect(res.status).toBe(200)
      expect(((await res.json()) as { sessions: { id: string }[] }).sessions.map((s) => s.id)).toEqual([output.id])
      for (const query of ['resource_type=output', 'resource_id=out-1', 'resource_type=print_runs&resource_id=1']) {
        const bad = await app().request(`/api/v1/ai/sessions?${query}`, { headers: UI_READ })
        expect(bad.status, query).toBe(400)
      }
    })

    it('filters sessions_list by resource, as its caller', async () => {
      const { edit } = await seed()
      const tool = ALL_TOOLS.find((t) => t.name === 'sessions_list')!
      const call = async (owner: typeof agentA) =>
        firstText(
          await runTool(tool, { resource: { type: 'model', id: 'box' } }, {
            ...services({ sessions: m }),
            principal: harnessPrincipal(owner),
            progress: async () => {},
            signal: new AbortController().signal,
          }),
        ) as { id: string }[]
      expect((await call(agentA)).map((s) => s.id)).toEqual([edit.id])
      expect(await call(agentB)).toEqual([])
    })
  })
})
