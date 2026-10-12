import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import { AuditLog, type AuditRecord } from '../src/audit/log.js'
import type { DurableGate } from '../src/gate/durable.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, type FakeTurn, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #792: forking from a message (`up_to`, #793), the fork's audit row and its 409, and
// renaming a session or marking it done (PATCH /api/v1/ai/sessions/:id, #795), over the
// scripted runner against Postgres. The real SDK's fork from a message is
// test/sessions.e2e.test.ts. Design: docs/superpowers/specs/2026-10-09-session-switcher-design.md.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const JSON_UI = { ...UI, 'content-type': 'application/json' }

describe.skipIf(skip !== undefined)(`session edits${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let audit: AuditLog
  let m: SessionManager
  let next: FakeTurn
  let app: ReturnType<typeof createApp>
  const failures: unknown[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    failures.length = 0
    audit = new AuditLog({ sql: db.sql, onError: (err) => failures.push(err) })
    next = { reply: 'hello there' }
    const { runner } = scriptedRunner(() => next)
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, audit })
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
      audit,
    }
    app = createApp(deps)
  })
  afterEach(async () => {
    m.abortAll()
    expect(failures).toEqual([])
    await drop()
  })

  const resourceRows = async (action: string): Promise<AuditRecord[]> =>
    (await audit.list({ kind: 'resource', action })).entries

  const fork = (id: string, body?: unknown, headers: Record<string, string> = JSON_UI) =>
    app.request(`/api/v1/ai/sessions/${id}/fork`, {
      method: 'POST',
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
  const patch = (id: string, body: unknown, headers: Record<string, string> = JSON_UI) =>
    app.request(`/api/v1/ai/sessions/${id}`, { method: 'PATCH', headers, body: JSON.stringify(body) })

  /**
   * What Claude Code writes for `turns` replies: per turn, the prompt and the reply's
   * text block, chained, with the API message id the scripted runner streamed
   * (`msg_fake_<n>`, so the panel's id is `msg_fake_<n>:0`).
   */
  async function transcript(id: string, turns: number): Promise<void> {
    const entries: SessionStoreEntry[] = []
    let parent: string | null = null
    for (let n = 1; n <= turns; n++) {
      const u = `00000000-0000-4000-8000-00000000000${2 * n - 1}`
      const a = `00000000-0000-4000-8000-00000000000${2 * n}`
      entries.push({ type: 'user', uuid: u, parentUuid: parent, sessionId: id, isSidechain: false, message: { role: 'user', content: `prompt ${n}` } })
      entries.push({
        type: 'assistant',
        uuid: a,
        parentUuid: u,
        sessionId: id,
        isSidechain: false,
        message: { id: `msg_fake_${n}`, role: 'assistant', content: [{ type: 'text', text: `reply ${n}` }] },
      })
      parent = a
    }
    await m.store.append({ projectKey: 'p', sessionId: id }, entries)
  }

  /** A browser session with `turns` finished turns and their transcript. */
  async function session(turns: number, owner = browser): Promise<string> {
    const { session: s, turn } = await m.start(owner, { origin: owner === browser ? 'chat' : 'mcp', prompt: 'prompt 1' })
    await turn!.done
    for (let n = 2; n <= turns; n++) await (await m.send(s.id, owner, `prompt ${n}`)).done
    await transcript(s.id, turns)
    return s.id
  }

  describe('fork', () => {
    it('answers 409 for a session with no transcript yet, audited as a failed fork', async () => {
      const { session: s } = await m.start(browser, { origin: 'chat', title: 'empty' })
      const res = await fork(s.id)
      expect(res.status).toBe(409)
      expect(((await res.json()) as { detail: string }).detail).toMatch(/no transcript to fork yet/)
      expect(await resourceRows('session_fork')).toEqual([
        expect.objectContaining({ outcome: 'error', surface: 'http', detail: `POST /api/v1/ai/sessions/${s.id}/fork → 409` }),
      ])
    })

    it('records a fork as a resource row on the child, naming the parent', async () => {
      const parent = await session(1)
      const res = await fork(parent)
      expect(res.status).toBe(201)
      const { session: child } = (await res.json()) as { session: { id: string } }
      expect(await resourceRows('session_fork')).toEqual([
        expect.objectContaining({
          outcome: 'ok',
          surface: 'http',
          session_id: child.id,
          client_ip: '10.0.0.7',
          actor: expect.objectContaining({ kind: 'browser' }),
          detail: `forked from ${parent}`,
        }),
      ])
    })

    it('forks up to a reply: the child has the turns through it, and only those', async () => {
      const parent = await session(3)
      const res = await fork(parent, { up_to: 'msg_fake_2:0' })
      expect(res.status).toBe(201)
      const { session: child } = (await res.json()) as { session: { id: string; parent_id: string } }
      expect(child.parent_id).toBe(parent)
      const events = (await m.events.read(child.id, 0, 1000)).map((e) => e.event)
      await expectPanelAccepts(events)
      expect(events.filter((e) => e.type === 'user.turn').map((e) => ('text' in e ? e.text : ''))).toEqual(['prompt 1', 'prompt 2'])
      expect(events.filter((e) => e.type === 'assistant.text.done').map((e) => ('messageId' in e ? e.messageId : ''))).toEqual([
        'msg_fake_1:0',
        'msg_fake_2:0',
      ])
      expect(events.at(-1)).toMatchObject({ type: 'session.status', status: 'idle' })
      // The SDK's copy stops at that reply too.
      const copied = (await m.store.load({ projectKey: 'p', sessionId: child.id })) ?? []
      const texts = copied.flatMap((e) => {
        const content = (e.message as { content?: unknown } | undefined)?.content
        return typeof content === 'string' ? [content] : Array.isArray(content) ? content.map((b: { text?: string }) => b.text ?? '') : []
      })
      expect(texts).toContain('reply 2')
      expect(texts).not.toContain('prompt 3')
      expect(texts).not.toContain('reply 3')
      expect((await resourceRows('session_fork'))[0]?.detail).toBe(`forked from ${parent} up to msg_fake_2:0`)
    })

    it('refuses an up_to that names no reply of the session with 400, and makes no session', async () => {
      const parent = await session(1)
      const before = await m.list(browser)
      for (const up_to of ['msg_fake_9:0', 'nonsense', '']) {
        const res = await fork(parent, { up_to })
        expect(res.status, up_to).toBe(400)
      }
      expect(await m.list(browser)).toHaveLength(before.length)
    })

    it('forks while the parent runs a turn, from what is stored', async () => {
      const parent = await session(1)
      next = { hang: true }
      await m.send(parent, browser, 'take your time')
      const res = await fork(parent, { up_to: 'msg_fake_1:0' })
      expect(res.status).toBe(201)
    })
  })

  describe('PATCH /api/v1/ai/sessions/:id', () => {
    it('renames a session, trimmed; the snapshot shows it', async () => {
      const id = await session(1)
      const res = await patch(id, { title: '  a better name  ' })
      expect(res.status).toBe(200)
      expect(((await res.json()) as { session: { title: string } }).session.title).toBe('a better name')
      const snapshot = await m.snapshot(browser)
      expect(snapshot).toMatchObject({ sessions: [expect.objectContaining({ sessionId: id, title: 'a better name' })] })
    })

    it('marks a session done: its panels hear it, and a send is refused after', async () => {
      const id = await session(1)
      const res = await patch(id, { done: true })
      expect(res.status).toBe(200)
      expect(((await res.json()) as { session: { status: string } }).session.status).toBe('done')
      const events = (await m.events.read(id, 0, 1000)).map((e) => e.event)
      expect(events.at(-1)).toEqual(expect.objectContaining({ type: 'session.status', sessionId: id, status: 'done' }))
      await expectPanelAccepts(events)
      await expect(m.send(id, browser, 'more')).rejects.toMatchObject({ code: 'closed' })
      // Still forkable: that is how a done chat continues.
      expect((await fork(id)).status).toBe(201)
    })

    it('refuses done while a turn runs, with 409', async () => {
      const id = await session(1)
      next = { hang: true }
      await m.send(id, browser, 'take your time')
      const res = await patch(id, { done: true })
      expect(res.status).toBe(409)
      expect(((await res.json()) as { detail: string }).detail).toMatch(/Stop it first/)
      expect((await m.get(id, browser)).status).toBe('running')
    })

    it('marks a durable session done and ends its workflow (#1056)', async () => {
      const id = await session(1)
      await db.sql`UPDATE ai_sessions SET mode = 'durable' WHERE id = ${id}`
      const ended: string[] = []
      m.durable = { end: (sid: string) => Promise.resolve(void ended.push(sid)) } as unknown as DurableGate
      expect((await patch(id, { done: true })).status).toBe(200)
      expect(ended).toEqual([id])
      expect((await m.get(id, browser)).status).toBe('done')
      expect((await patch(id, { title: 'still renamable' })).status).toBe(200)
      expect(ended).toEqual([id])
    })

    it('leaves a durable session as it was when its workflow cannot be told, with 503', async () => {
      const id = await session(1)
      await db.sql`UPDATE ai_sessions SET mode = 'durable' WHERE id = ${id}`
      m.durable = undefined
      expect((await patch(id, { done: true })).status).toBe(503)
      expect((await m.get(id, browser)).status).not.toBe('done')
    })

    it("refuses another principal's session until it is taken over", async () => {
      const id = await session(1, agentA)
      const res = await patch(id, { title: 'mine now?' })
      expect(res.status).toBe(403)
      expect(((await res.json()) as { detail: string }).detail).toMatch(/take it over/)
      expect((await m.get(id, browser)).title).not.toBe('mine now?')
    })

    it('refuses an empty edit, an empty or long title, done: false, and unknown fields with 400; an unknown session with 404', async () => {
      const id = await session(1)
      for (const body of [{}, { title: '   ' }, { title: 'x'.repeat(201) }, { done: false }, { status: 'done' }]) {
        expect((await patch(id, body)).status, JSON.stringify(body)).toBe(400)
      }
      expect((await patch('00000000-0000-4000-8000-000000000999', { title: 'x' })).status).toBe(404)
    })

    it('refuses a write without the UI origin, audited as refused', async () => {
      const id = await session(1)
      const res = await patch(id, { title: 'x' }, { 'content-type': 'application/json' })
      expect(res.status).toBe(403)
      expect(await resourceRows('session_update')).toEqual([expect.objectContaining({ outcome: 'refused' })])
    })
  })

  it("the snapshot carries each session's parent, last activity and spend for the switcher", async () => {
    const parent = await session(1)
    const { session: child } = (await (await fork(parent)).json()) as { session: { id: string } }
    const snapshot = await m.snapshot(browser)
    if (snapshot.type !== 'sessions.snapshot') throw new Error('unreachable')
    const byId = new Map(snapshot.sessions.map((s) => [s.sessionId, s]))
    expect(byId.get(child.id)).toMatchObject({ parentId: parent, updatedAt: expect.any(String), costUsd: 0, budgetUsd: 1 })
    expect(byId.get(parent)).toMatchObject({ parentId: null, updatedAt: expect.any(String), budgetUsd: 1 })
    await expectPanelAccepts([snapshot])
  })
})
