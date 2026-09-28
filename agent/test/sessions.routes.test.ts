import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
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
  })

  it('refuses writes without the UI origin, reads from another site, and unknown sessions', async () => {
    const bare = await app.request('/api/v1/ai/sessions', { method: 'POST', body: '{}', headers: { 'content-type': 'application/json' } })
    expect(bare.status).toBe(403)
    const cross = await app.request('/api/v1/ai/sessions', { headers: { ...UI_READ, 'sec-fetch-site': 'cross-site' } })
    expect(cross.status).toBe(403)
    const form = await app.request('/api/v1/ai/sessions', { method: 'POST', headers: { ...UI, 'content-type': 'text/plain' }, body: '{"prompt":"x"}' })
    expect(form.status).toBe(415)
    const unknown = await app.request('/api/v1/ai/sessions/00000000-0000-4000-8000-000000000000', { headers: UI_READ })
    expect(unknown.status).toBe(404)
    const junk = await app.request('/api/v1/ai/sessions/not-a-uuid/events', { headers: UI_READ })
    expect(junk.status).toBe(404)
    const badStatus = await app.request('/api/v1/ai/sessions?status=bogus', { headers: UI_READ })
    expect(badStatus.status).toBe(400)
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
