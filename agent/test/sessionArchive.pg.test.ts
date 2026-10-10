import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import { AuditLog } from '../src/audit/log.js'
import { principalFor } from '../src/auth/tokens.js'
import type { Database } from '../src/db.js'
import { ATTENTION_TOOL } from '../src/harness/questions.js'
import { originPolicy } from '../src/http/origins.js'
import { ChatConnection } from '../src/routes/chat.js'
import { pendingInput } from '../src/routes/pendingInput.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type ToolContext } from '../src/tools/registry.js'
import { firstText, services } from './helpers/mcp.js'
import { expectPanelAccepts, frontendClientMessages } from './support/frontendProtocol.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, type FakeTurn, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #1885: archiving a chat (PATCH /api/v1/ai/sessions/:id {archived}). An archived
// chat leaves the panel's snapshot and the default lists, is read-only (a send or a
// handoff is refused with `archived`, a fork is not), and is refused while a turn
// runs or anything is parked; an undismissed `done` summary is dismissed with it.
// Design: docs/superpowers/specs/2026-10-09-session-switcher-design.md, "Archive".

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const JSON_UI = { ...UI, 'content-type': 'application/json' }

type View = { id: string; archived: boolean; archived_at: string | null; status: string }
type Result = { isError?: boolean; content: { type: string; text?: string }[] }

describe.skipIf(skip !== undefined)(`session archive${skip ? ` (skipped: ${skip})` : ''}`, () => {
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

  const patch = (id: string, body: unknown) =>
    app.request(`/api/v1/ai/sessions/${id}`, { method: 'PATCH', headers: JSON_UI, body: JSON.stringify(body) })
  const list = async (query = ''): Promise<View[]> => {
    const res = await app.request(`/api/v1/ai/sessions${query}`, { headers: UI })
    expect(res.status).toBe(200)
    return ((await res.json()) as { sessions: View[] }).sessions
  }
  const snapshotIds = async (): Promise<string[]> => {
    const snapshot = await m.snapshot(browser)
    if (snapshot.type !== 'sessions.snapshot') throw new Error('unreachable')
    return snapshot.sessions.map((s) => s.sessionId)
  }

  /** A browser session with one finished turn. */
  async function session(owner = browser): Promise<string> {
    const { session: s, turn } = await m.start(owner, { origin: owner === browser ? 'chat' : 'mcp', prompt: 'prompt 1' })
    await turn!.done
    return s.id
  }

  async function archived(id: string): Promise<View> {
    const res = await patch(id, { archived: true })
    expect(res.status, await res.clone().text()).toBe(200)
    return ((await res.json()) as { session: View }).session
  }

  it('archives a chat: it leaves the snapshot and the default list, and ?archived=true lists it', async () => {
    const kept = await session()
    const id = await session()
    const view = await archived(id)
    expect(view).toMatchObject({ id, archived: true, archived_at: expect.any(String) })
    expect(await snapshotIds()).toEqual([kept])
    expect((await list()).map((s) => s.id)).toEqual([kept])
    expect((await list('?archived=false')).map((s) => s.id)).toEqual([kept])
    expect((await list('?archived=true')).map((s) => s.id)).toEqual([id])
    expect((await list('?archived=include')).map((s) => s.id).sort()).toEqual([id, kept].sort())
    expect((await app.request('/api/v1/ai/sessions?archived=maybe', { headers: UI })).status).toBe(400)
    // Still readable by id, with its transcript.
    expect((await m.get(id, browser)).archivedAt).not.toBeNull()
    expect((await m.events.read(id, 0, 1000)).some((e) => e.event.type === 'user.turn')).toBe(true)
  })

  it('is read-only: a send is refused with `archived` (409 over HTTP) and never unarchives it', async () => {
    const id = await session()
    await archived(id)
    await expect(m.send(id, browser, 'more')).rejects.toMatchObject({ code: 'archived', status: 409 })
    const res = await app.request(`/api/v1/ai/sessions/${id}/messages`, {
      method: 'POST',
      headers: JSON_UI,
      body: JSON.stringify({ text: 'more' }),
    })
    expect(res.status).toBe(409)
    expect(((await res.json()) as { detail: string }).detail).toMatch(/archived.*[Uu]narchive/)
    expect((await m.get(id, browser)).archivedAt).not.toBeNull()
    await expect(m.handoff(id, browser, agentA)).rejects.toMatchObject({ code: 'archived' })
  })

  it('answers a send over the chat socket with an `archived` error that says how to continue', async () => {
    const id = await session()
    await archived(id)
    const out: { type: string; code?: string; message?: string; sessionId?: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e as never))
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(JSON.stringify(clientMessage({ type: 'user.message', sessionId: id, text: 'more', context: { route: '/' } })))
    connection.close()
    expect(out.filter((e) => e.type === 'error')).toEqual([
      expect.objectContaining({ sessionId: id, code: 'archived', message: expect.stringMatching(/archived.*unarchive it.*fork it/) }),
    ])
    await expectPanelAccepts(out as never)
  })

  it('a durable archived session refuses a send before it reaches the workflow', async () => {
    const id = await session()
    await archived(id)
    await db.sql`UPDATE ai_sessions SET mode = 'durable' WHERE id = ${id}`
    await expect(m.send(id, browser, 'more')).rejects.toMatchObject({ code: 'archived' })
  })

  it('forks an archived chat; the fork is not archived', async () => {
    const id = await session()
    await m.store.append({ projectKey: 'p', sessionId: id }, [
      { type: 'user', uuid: '00000000-0000-4000-8000-000000000001', parentUuid: null, sessionId: id, isSidechain: false, message: { role: 'user', content: 'p' } },
      {
        type: 'assistant',
        uuid: '00000000-0000-4000-8000-000000000002',
        parentUuid: '00000000-0000-4000-8000-000000000001',
        sessionId: id,
        isSidechain: false,
        message: { id: 'msg_fake_1', role: 'assistant', content: [{ type: 'text', text: 'r' }] },
      },
    ])
    await archived(id)
    const res = await app.request(`/api/v1/ai/sessions/${id}/fork`, { method: 'POST', headers: JSON_UI })
    expect(res.status).toBe(201)
    const { session: child } = (await res.json()) as { session: View }
    expect(child.archived).toBe(false)
    expect(await snapshotIds()).toEqual([child.id])
  })

  it('unarchives: back in the snapshot and the list, and it takes messages again', async () => {
    const id = await session()
    await archived(id)
    const res = await patch(id, { archived: false })
    expect(res.status).toBe(200)
    expect(((await res.json()) as { session: View }).session).toMatchObject({ archived: false, archived_at: null })
    expect(await snapshotIds()).toEqual([id])
    expect((await list()).map((s) => s.id)).toEqual([id])
    await (await m.send(id, browser, 'again')).done
  })

  it('refuses to archive while a turn runs, with 409', async () => {
    const id = await session()
    next = { hang: true }
    await m.send(id, browser, 'take your time')
    const res = await patch(id, { archived: true })
    expect(res.status).toBe(409)
    expect(((await res.json()) as { detail: string }).detail).toMatch(/Stop it first/)
    expect((await m.get(id, browser)).archivedAt).toBeNull()
  })

  it('refuses to archive while an approval, a question or a durable request is parked', async () => {
    const parked = {
      approval: async (id: string) => db.sql`
        INSERT INTO ai_approvals (id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
                                  requested_by_kind, requested_by_id, requested_by_label, expires_at)
        VALUES (gen_random_uuid(), ${id}, gen_random_uuid(), 'toolu_out', 'send_to_bambuddy', 'send', 'h', 'outward',
                'browser', 'browser', 'you', now() + interval '10 minutes')`,
      question: async (id: string) => db.sql`
        INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, expires_at)
        VALUES (gen_random_uuid(), ${id}, gen_random_uuid(), 'AskUserQuestion', 'toolu_q', '[]', now() + interval '10 minutes')`,
      attention: async (id: string) => db.sql`
        INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, on_timeout, expires_at)
        VALUES (gen_random_uuid(), ${id}, gen_random_uuid(), ${ATTENTION_TOOL}, 'toolu_a', '[]', 'attention', 'blocked', 'wait',
                now() + interval '10 minutes')`,
      durable: async (id: string) => db.sql`
        INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id, kind, tool, responders, expires_at)
        VALUES ('durable:' || ${id} || ':run:toolu_d', ${id}, 'session-' || ${id}, 'run', 'answer', 'AskUserQuestion',
                ARRAY['browser'], now() + interval '10 minutes')`,
    }
    for (const [what, park] of Object.entries(parked)) {
      const id = await session()
      await park(id)
      const res = await patch(id, { archived: true })
      expect(res.status, what).toBe(409)
      expect(((await res.json()) as { detail: string }).detail, what).toMatch(/waiting for you/)
      expect((await m.get(id, browser)).archivedAt, what).toBeNull()
    }
  })

  it('dismisses an undismissed done summary with the archive: nothing of it is left on the badge', async () => {
    const id = await session()
    const [done] = await db.sql<{ id: string }[]>`
      INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, summary)
      VALUES (gen_random_uuid(), ${id}, gen_random_uuid(), ${ATTENTION_TOOL}, 'toolu_done', '[]', 'attention', 'done', 'touched nothing')
      RETURNING id`
    expect((await pendingInput(m)).entries.map((e) => e.session_id)).toEqual([id])
    await archived(id)
    expect((await pendingInput(m)).entries).toEqual([])
    const [row] = await db.sql`SELECT outcome, reason FROM ai_questions WHERE id = ${done!.id}`
    expect(row).toEqual({ outcome: 'cancelled', reason: 'the chat was archived' })
    const events = (await m.events.read(id, 0, 1000)).map((e) => e.event)
    expect(events.slice(-2)).toEqual([
      expect.objectContaining({ type: 'question.resolved', id: done!.id, answered: false }),
      expect.objectContaining({ type: 'input.resolved', id: `question:${done!.id}`, outcome: 'cancelled' }),
    ])
    await expectPanelAccepts(events)
    expect((await audit.list({ kind: 'question', action: 'cancelled' })).entries).toEqual([
      expect.objectContaining({ session_id: id, detail: expect.stringMatching(/the chat was archived/) }),
    ])
  })

  it("refuses another principal's session until it is taken over (owner-only)", async () => {
    const id = await session(agentA)
    expect((await patch(id, { archived: true })).status).toBe(403)
    expect((await m.get(id, browser)).archivedAt).toBeNull()
  })

  it('refuses a non-boolean archived with 400', async () => {
    const id = await session()
    for (const body of [{ archived: 'yes' }, { archived: null }]) {
      expect((await patch(id, body)).status, JSON.stringify(body)).toBe(400)
    }
  })

  describe('sessions_* tools', () => {
    const byName = new Map(ALL_TOOLS.map((t) => [t.name, t]))
    const ctxOf = (principal: ToolContext['principal']): ToolContext => ({
      ...services({ sessions: m }),
      principal,
      progress: async () => {},
      signal: new AbortController().signal,
    })
    const call = (ctx: ToolContext, name: string, args: Record<string, unknown>) =>
      runTool(byName.get(name)!, args, ctx) as Promise<Result>
    const text = (r: Result) => r.content.map((c) => c.text ?? '').join('\n')

    it('sessions_list hides archived sessions unless asked, and flags each row', async () => {
      const agent = ctxOf(principalFor('a', 'write'))
      // Started by the agent, taken over by the user and archived: the agent still sees it as its creator.
      const id = await session(agentA)
      await m.handoff(id, browser, browser)
      const kept = await session(agentA)
      await archived(id)
      const listed = async (args: Record<string, unknown>) =>
        (firstText(await call(agent, 'sessions_list', args)) as { id: string; archived: boolean }[]).map((s) => [s.id, s.archived])
      expect(await listed({})).toEqual([[kept, false]])
      expect(await listed({ archived: 'only' })).toEqual([[id, true]])
      expect((await listed({ archived: 'include' })).sort()).toEqual([[id, true], [kept, false]].sort())
    })

    it('sessions_send and sessions_handoff refuse an archived session; sessions_fork does not', async () => {
      const you = ctxOf({ id: 'browser', kind: 'browser', tiers: ['read', 'write', 'outward'] })
      const id = await session()
      await archived(id)
      const sent = await call(you, 'sessions_send', { session_id: id, text: 'more', wait_seconds: 0 })
      expect(sent.isError).toBe(true)
      expect(text(sent)).toMatch(/archived/)
      const handed = await call(you, 'sessions_handoff', { session_id: id, to: 'token:0e5a3c1e-1111-4222-8333-944455556666' })
      expect(handed.isError).toBe(true)
      expect(text(handed)).toMatch(/archived/)
      const forked = await call(you, 'sessions_fork', { session_id: id })
      // No transcript in this scripted session: the fork gets as far as that, past any archive check.
      expect(text(forked)).not.toMatch(/archived/)
    })
  })
})
