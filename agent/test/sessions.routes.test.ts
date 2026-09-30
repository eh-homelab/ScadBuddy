import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { JSON_BODY_MAX } from '../src/routes/guard.js'
import { MESSAGE_MAX } from '../src/sessions/clientProtocol.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, type FakeTurn, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// /api/v1/ai/sessions (routes/sessions.ts) over a scripted runner: the routes'
// own behaviour (guard, status codes, the SSE replay). The real SDK path is
// test/chat.e2e.test.ts.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

/** The UI through the TLS ingress (routes/guard.ts). */
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const JSON_UI = { ...UI, 'content-type': 'application/json' }

describe.skipIf(skip !== undefined)(`session routes${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  let next: FakeTurn
  let app: ReturnType<typeof createApp>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    next = { reply: 'hello there' }
    const { runner } = scriptedRunner(() => next)
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner })
    app = createApp(deps())
  })
  afterEach(async () => {
    m.abortAll()
    await drop()
  })

  function deps(overrides: Partial<AppDeps> = {}): AppDeps {
    return {
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      approvals: m.approvals,
      sessions: m,
      ...overrides,
    }
  }

  async function waitIdle(id: string): Promise<void> {
    for (let i = 0; i < 200; i++) {
      const s = await m.get(id, browser)
      if (!s.turnActive && s.status !== 'running') return
      await new Promise((r) => setTimeout(r, 25))
    }
    throw new Error('the turn did not finish')
  }

  it('starts a session with a first turn, lists and reads it, and sends another turn', async () => {
    const res = await app.request('/api/v1/ai/sessions', {
      method: 'POST',
      headers: JSON_UI,
      body: JSON.stringify({ prompt: 'make it bigger' }),
    })
    expect(res.status).toBe(201)
    expect(res.headers.get('x-scadbuddy-service')).toBe('agent')
    const started = (await res.json()) as { session: { id: string; title: string; origin: string }; turn_id: string }
    expect(started.session).toMatchObject({ title: 'make it bigger', origin: 'chat', owner: { kind: 'browser' } })
    expect(started.turn_id).toEqual(expect.any(String))
    const id = started.session.id
    await waitIdle(id)

    const list = (await (await app.request('/api/v1/ai/sessions', { headers: UI_READ })).json()) as { sessions: { id: string }[] }
    expect(list.sessions.map((s) => s.id)).toEqual([id])
    const one = await app.request(`/api/v1/ai/sessions/${id}`, { headers: UI_READ })
    expect(await one.json()).toMatchObject({ id, status: 'idle', turns: 1, running: false })

    const sent = await app.request(`/api/v1/ai/sessions/${id}/messages`, {
      method: 'POST',
      headers: JSON_UI,
      body: JSON.stringify({ text: 'and again' }),
    })
    expect(sent.status).toBe(202)
    await waitIdle(id)
    expect((await m.get(id, browser)).turns).toBe(2)
  })

  it('streams the event log as SSE from ?after= / Last-Event-ID', async () => {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    await turn!.done
    const controller = new AbortController()
    const res = await app.request(`/api/v1/ai/sessions/${session.id}/events?after=2`, {
      headers: UI_READ,
      signal: controller.signal,
    })
    expect(res.status).toBe(200)
    expect(res.headers.get('content-type')).toMatch(/^text\/event-stream/)
    expect(res.headers.get('x-accel-buffering')).toBe('no')
    const reader = res.body!.getReader()
    let text = ''
    // seq 8, the closing session.status, is the last event the turn wrote.
    while (!/^id: 8$/m.test(text)) {
      const { value, done } = await reader.read()
      if (done) break
      text += new TextDecoder().decode(value)
    }
    controller.abort()
    await reader.cancel().catch(() => {})
    const events = text
      .split('\n\n')
      .filter((block) => block.includes('data:'))
      .map((block) => ({
        id: /^id: ?(\d+)$/m.exec(block)?.[1],
        data: JSON.parse(/^data: ?(.*)$/m.exec(block)![1]!) as { type: string },
      }))
    // seq 1 and 2 (session.started, idle) are skipped; the turn follows from 3.
    expect(events[0]).toMatchObject({ id: '3', data: { type: 'user.turn' } })
    expect(events.map((e) => e.data.type)).toContain('assistant.text.delta')
    await expectPanelAccepts(events.map((e) => e.data))

    // Last-Event-ID resumes after the given seq: nothing yet past 7 but the closing status.
    const resume = new AbortController()
    const lastId = await app.request(`/api/v1/ai/sessions/${session.id}/events`, {
      headers: { ...UI_READ, 'last-event-id': '7' },
      signal: resume.signal,
    })
    const first = new TextDecoder().decode((await lastId.body!.getReader().read()).value)
    resume.abort()
    expect(first).toMatch(/^data: .*"status":"idle".*\nid: 8\n\n$/s)

    // An EventSource reconnects to the same URL, ?after= and all, adding
    // Last-Event-ID: that one wins, so nothing already received is replayed.
    const reconnect = new AbortController()
    const both = await app.request(`/api/v1/ai/sessions/${session.id}/events?after=2`, {
      headers: { ...UI_READ, 'last-event-id': '7' },
      signal: reconnect.signal,
    })
    const resumed = new TextDecoder().decode((await both.body!.getReader().read()).value)
    reconnect.abort()
    expect(resumed).toMatch(/^data: .*"status":"idle".*\nid: 8\n\n$/s)
  })

  it("ends an open event stream on the app's close(), so shutdown does not wait on it", async () => {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    await turn!.done
    const res = await app.request(`/api/v1/ai/sessions/${session.id}/events`, { headers: UI_READ })
    const reader = res.body!.getReader()
    let text = ''
    while (!/^id: 8$/m.test(text)) {
      const { value, done } = await reader.read()
      if (done) throw new Error('the stream ended before the replay')
      text += new TextDecoder().decode(value)
    }
    await app.close()
    for (;;) {
      const { done } = await reader.read()
      if (done) break
    }
  })

  it('refuses a resume point that is not a seq with 400, rather than replaying from the start', async () => {
    const { session } = await m.start(browser, { origin: 'chat', title: 'x' })
    for (const after of ['abc', '-1', '1.5', '']) {
      const res = await app.request(`/api/v1/ai/sessions/${session.id}/events?after=${after}`, { headers: UI_READ })
      expect(res.status, `after=${after}`).toBe(400)
      expect(await res.json()).toEqual({ detail: 'after must be a non-negative integer' })
    }
    // Last-Event-ID is read first, so a bad one is refused even beside a good ?after=.
    const header = await app.request(`/api/v1/ai/sessions/${session.id}/events?after=2`, {
      headers: { ...UI_READ, 'last-event-id': 'seven' },
    })
    expect(header.status).toBe(400)
    expect(await header.json()).toEqual({ detail: 'Last-Event-ID must be a non-negative integer' })
  })

  it("limits new sessions per owner across the routes and the manager (the chat socket's path), with 429", async () => {
    m.abortAll()
    const { runner } = scriptedRunner(() => next)
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, newSessions: { max: 3, windowMs: 60_000 } })
    app = createApp(deps())
    const post = () =>
      app.request('/api/v1/ai/sessions', { method: 'POST', headers: JSON_UI, body: JSON.stringify({ title: 't' }) })
    expect((await post()).status).toBe(201)
    expect((await post()).status).toBe(201)
    // What ChatConnection calls for a new chat: counted against the same owner.
    await m.start(browser, { origin: 'chat' })
    const refused = await post()
    expect(refused.status).toBe(429)
    expect(((await refused.json()) as { detail: string }).detail).toMatch(/too many new sessions/)
    await expect(m.start(browser, { origin: 'chat' })).rejects.toMatchObject({ code: 'rate_limited' })
    expect(await m.list(browser)).toHaveLength(3)
    // Another owner has its own window.
    await expect(m.start(agentA, { origin: 'chat' })).resolves.toMatchObject({ session: { id: expect.any(String) } })
  })

  it('counts concurrent new sessions against each other', async () => {
    m.abortAll()
    const { runner } = scriptedRunner(() => next)
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, newSessions: { max: 2, windowMs: 60_000 } })
    const results = await Promise.allSettled(Array.from({ length: 6 }, () => m.start(browser, { origin: 'chat' })))
    expect(results.filter((r) => r.status === 'fulfilled')).toHaveLength(2)
    expect(await m.list(browser)).toHaveLength(2)
  })

  it('counts a fork as a new session (PR #715 review)', async () => {
    m.abortAll()
    const { runner } = scriptedRunner(() => next)
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, newSessions: { max: 2, windowMs: 60_000 } })
    const { session } = await m.start(browser, { origin: 'chat' })
    // The scripted runner writes no SDK transcript; the fork needs one.
    await m.store.append({ projectKey: 'p', sessionId: session.id }, [{ type: 'user', uuid: 'u1', message: {} }])
    await m.fork(session.id, browser)
    await expect(m.fork(session.id, browser)).rejects.toMatchObject({ code: 'rate_limited' })
    expect(await m.list(browser)).toHaveLength(2)
  })

  it('refuses writes without the UI origin, reads from another site, and unknown sessions', async () => {
    const bare = await app.request('/api/v1/ai/sessions', { method: 'POST', body: '{}', headers: { 'content-type': 'application/json' } })
    expect(bare.status).toBe(403)
    const cross = await app.request('/api/v1/ai/sessions', { headers: { ...UI_READ, 'sec-fetch-site': 'cross-site' } })
    expect(cross.status).toBe(403)
    const form = await app.request('/api/v1/ai/sessions', { method: 'POST', headers: { ...UI, 'content-type': 'text/plain' }, body: '{"prompt":"x"}' })
    expect(form.status).toBe(415)
    // The same for a message: only JSON is read.
    const { session } = await m.start(browser, { origin: 'chat', title: 'x' })
    for (const type of ['text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data; boundary=b']) {
      const res = await app.request(`/api/v1/ai/sessions/${session.id}/messages`, {
        method: 'POST',
        headers: { ...UI, 'content-type': type },
        body: '{"text":"hi"}',
      })
      expect(res.status, type).toBe(415)
    }
    const none = await app.request(`/api/v1/ai/sessions/${session.id}/messages`, {
      method: 'POST',
      headers: UI,
      body: '{"text":"hi"}',
    })
    expect(none.status).toBe(415)
    expect((await m.get(session.id, browser)).turns).toBe(0)
    const unknown = await app.request('/api/v1/ai/sessions/00000000-0000-4000-8000-000000000000', { headers: UI_READ })
    expect(unknown.status).toBe(404)
    const junk = await app.request('/api/v1/ai/sessions/not-a-uuid/events', { headers: UI_READ })
    expect(junk.status).toBe(404)
    const badStatus = await app.request('/api/v1/ai/sessions?status=bogus', { headers: UI_READ })
    expect(badStatus.status).toBe(400)
  })

  it('accepts a full-length message in any script, even with every character JSON-escaped', async () => {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    await turn!.done
    // 32 000 CJK characters: 96 000 bytes as UTF-8, 192 000 as \uXXXX escapes.
    const text = '漢'.repeat(MESSAGE_MAX)
    const raw = await app.request(`/api/v1/ai/sessions/${session.id}/messages`, {
      method: 'POST',
      headers: JSON_UI,
      body: JSON.stringify({ text }),
    })
    expect(raw.status).toBe(202)
    await waitIdle(session.id)
    const escaped = `{"text":"${'\\u6f22'.repeat(MESSAGE_MAX)}"}`
    expect(escaped.length).toBeLessThanOrEqual(JSON_BODY_MAX)
    const res = await app.request(`/api/v1/ai/sessions/${session.id}/messages`, {
      method: 'POST',
      headers: { ...JSON_UI, 'content-length': String(escaped.length) },
      body: escaped,
    })
    expect(res.status).toBe(202)
    await waitIdle(session.id)
    // One character more is the schema's 400, not the byte cap's 413.
    const over = await app.request(`/api/v1/ai/sessions/${session.id}/messages`, {
      method: 'POST',
      headers: JSON_UI,
      body: JSON.stringify({ text: '漢'.repeat(MESSAGE_MAX + 1) }),
    })
    expect(over.status).toBe(400)
  })

  it('refuses a list limit that is not an integer from 1 to 500, as it does a bad status', async () => {
    for (const limit of ['0', '-1', 'abc', '1.5', '501', '']) {
      const res = await app.request(`/api/v1/ai/sessions?limit=${limit}`, { headers: UI_READ })
      expect(res.status, `limit=${limit}`).toBe(400)
      expect(await res.json()).toEqual({ detail: 'limit must be an integer from 1 to 500' })
    }
    await m.start(browser, { origin: 'chat', title: 'a' })
    await m.start(browser, { origin: 'chat', title: 'b' })
    const one = (await (await app.request('/api/v1/ai/sessions?limit=1', { headers: UI_READ })).json()) as { sessions: unknown[] }
    expect(one.sessions).toHaveLength(1)
  })

  it('answers 409 to a send while a turn runs, and interrupts it', async () => {
    next = { hang: true }
    const res = await app.request('/api/v1/ai/sessions', { method: 'POST', headers: JSON_UI, body: JSON.stringify({ prompt: 'wait' }) })
    const { session } = (await res.json()) as { session: { id: string } }
    const busy = await app.request(`/api/v1/ai/sessions/${session.id}/messages`, {
      method: 'POST',
      headers: JSON_UI,
      body: JSON.stringify({ text: 'again' }),
    })
    expect(busy.status).toBe(409)
    expect(((await busy.json()) as { detail: string }).detail).toMatch(/already running/)
    const stop = await app.request(`/api/v1/ai/sessions/${session.id}/interrupt`, { method: 'POST', headers: UI })
    expect(await stop.json()).toEqual({ interrupted: true })
    await waitIdle(session.id)
  })

  it('settles once the aborted turns have appended their last events (#802)', async () => {
    next = { hang: true }
    const { session } = await m.start(browser, { origin: 'chat', prompt: 'wait' })
    m.abortAll()
    await m.settled()
    // Nothing left to append: a shutdown may close the database now.
    const events = await m.events.read(session.id, 0)
    expect(events.at(-1)?.event).toMatchObject({ type: 'session.status', status: 'idle' })
    await expect(m.settled()).resolves.toBeUndefined()
  })

  it('takes over a session another principal controls', async () => {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 'theirs' })
    const res = await app.request(`/api/v1/ai/sessions/${session.id}/handoff`, { method: 'POST', headers: UI })
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({ owner: { kind: 'browser', id: 'browser' } })
  })

  it('answers 503 without sessions (no database)', async () => {
    const off = createApp(deps({ sessions: undefined }))
    expect((await off.request('/api/v1/ai/sessions', { headers: UI_READ })).status).toBe(503)
  })
})
