import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  ApprovalService,
  canonicalJson,
  DEFAULT_APPROVAL_EXPIRY_SECONDS,
  inputHash,
  MIN_APPROVAL_EXPIRY_SECONDS,
  SETTING_APPROVAL_EXPIRY_SECONDS,
  summariseInput,
} from '../src/approvals/service.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { registerApprovalRoutes } from '../src/routes/approvals.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, agentB, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// The approval store and its rules without the SDK (#258): ai_approvals in
// Postgres, expiry, visibility, the orphan paths, and the HTTP routes. The
// parked-turn flows run on the real SDK in test/approvals.e2e.test.ts.

describe('input binding', () => {
  it('hashes the tool and a canonical form of the input', () => {
    expect(canonicalJson({ b: 1, a: { d: [1, { f: 2, e: 3 }], c: null } })).toBe('{"a":{"c":null,"d":[1,{"e":3,"f":2}]},"b":1}')
    expect(inputHash('t', { a: 1, b: 2 })).toBe(inputHash('t', { b: 2, a: 1 }))
    expect(inputHash('t', { a: 1 })).not.toBe(inputHash('t', { a: 2 }))
    expect(inputHash('t', { a: 1 })).not.toBe(inputHash('u', { a: 1 }))
    expect(inputHash('t', { a: 1 })).toMatch(/^[0-9a-f]{64}$/)
  })

  it('summarises through scrubForLog: secrets and secret-named arguments never reach the summary', () => {
    const summary = summariseInput('mcp__x__send', { job: 'box', api_key: 'k-123', note: 'with sk-live-999' }, ['sk-live-999'])
    expect(summary).toBe('{"job":"box","api_key":"[redacted]","note":"with [redacted]"}')
  })
})

describe.skipIf(!TEST_DATABASE_URL)(`approvals in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  let values: Map<string, unknown>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    values = new Map()
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: runner,
      settings: { get: <T>(key: string) => Promise.resolve(values.get(key) as T) },
    })
  })
  afterEach(async () => {
    await drop()
  })

  /** A session with an approval left pending by a turn that is gone (a restart). */
  async function orphan(owner = agentA, input: Record<string, unknown> = { job: 'box.3mf' }) {
    const { session } = await m.start(owner, { origin: 'mcp', title: 't' })
    await db.sql`UPDATE ai_sessions SET status = 'waiting_approval' WHERE id = ${session.id}`
    const approval = await m.approvals.create({
      sessionId: session.id,
      turnId: null,
      toolUseId: 'toolu_1',
      tool: 'mcp__stub__print',
      input,
      tier: 'outward',
      requestedBy: owner,
    })
    return { session, approval }
  }

  it('reads the expiry window from ai_settings, within bounds', async () => {
    expect(await m.approvals.expirySeconds()).toBe(DEFAULT_APPROVAL_EXPIRY_SECONDS)
    const settings = new SettingsStore(db.sql)
    const service = new ApprovalService({ sql: db.sql, events: m.events, settings })
    await settings.set(SETTING_APPROVAL_EXPIRY_SECONDS, 120)
    expect(await service.expirySeconds()).toBe(120)
    await settings.set(SETTING_APPROVAL_EXPIRY_SECONDS, 1)
    expect(await service.expirySeconds()).toBe(MIN_APPROVAL_EXPIRY_SECONDS)

    values.set(SETTING_APPROVAL_EXPIRY_SECONDS, 120)
    const { approval } = await orphan()
    const window = (Date.parse(approval.expiresAt) - Date.parse(approval.createdAt)) / 1000
    expect(window).toBeCloseTo(120, 0)
  })

  it('stores the scrubbed summary and the hash, never the input itself', async () => {
    const { approval } = await orphan(agentA, { job: 'box.3mf', password: 'hunter2-long-secret' })
    expect(approval).toMatchObject({
      decision: null,
      tier: 'outward',
      requestedBy: agentA,
      inputSummary: '{"job":"box.3mf","password":"[redacted]"}',
      inputHash: inputHash('mcp__stub__print', { job: 'box.3mf', password: 'hunter2-long-secret' }),
    })
    const [raw] = await db.sql`SELECT row_to_json(a)::text AS row FROM ai_approvals a WHERE id = ${approval.id}`
    expect(String(raw?.row)).not.toContain('hunter2-long-secret')
  })

  it('expires an orphan that nobody decided, and the session goes back to idle', async () => {
    const { session, approval } = await orphan()
    expect(await m.approvals.expireDue()).toBe(0)
    await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = ${approval.id}`
    expect(await m.approvals.expireDue()).toBe(1)
    expect(await m.approvals.get(approval.id, browser)).toMatchObject({ decision: 'expired', reason: 'no decision before it expired' })
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle' })
    await expect(m.approvals.decide(browser, approval.id, true)).rejects.toMatchObject({ code: 'expired' })
    const events = (await m.events.read(session.id, 0, 1000)).map((e) => e.event)
    await expectPanelAccepts(events)
    expect(events.slice(-2)).toEqual([
      { v: 1, type: 'approval.resolved', sessionId: session.id, id: approval.id, approved: false },
      { v: 1, type: 'session.status', sessionId: session.id, status: 'idle' },
    ])
  })

  it('an approval decided just as it expires cannot be approved', async () => {
    const { approval } = await orphan()
    await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = ${approval.id}`
    // Not swept yet: the decision itself finds it due.
    await expect(m.approvals.decide(browser, approval.id, true)).rejects.toMatchObject({ code: 'expired', status: 410 })
    expect((await m.approvals.get(approval.id, browser)).decision).toBe('expired')
  })

  it('denying an orphan settles the session without a turn', async () => {
    const { session, approval } = await orphan()
    expect(await m.approvals.decide(browser, approval.id, false)).toMatchObject({ decision: 'denied', decidedBy: browser })
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle', turns: 0 })
  })

  it('an approved orphan is used once, by the same tool and input only', async () => {
    const { session, approval } = await orphan()
    // No resume in this service (the manager's is covered end to end).
    const service = new ApprovalService({ sql: db.sql, events: m.events })
    await service.decide(browser, approval.id, true)
    const hash = inputHash('mcp__stub__print', { job: 'box.3mf' })
    expect(await service.consume(session.id, 'mcp__stub__print', inputHash('mcp__stub__print', { job: 'other' }))).toBeUndefined()
    expect(await service.consume(session.id, 'mcp__other__print', hash)).toBeUndefined()
    expect(await service.consume(session.id, 'mcp__stub__print', hash)).toMatchObject({ id: approval.id })
    expect(await service.consume(session.id, 'mcp__stub__print', hash)).toBeUndefined()
    expect(await service.consumeById(approval.id)).toBeUndefined()
  })

  it('interrupting a session with an orphan cancels it; a new turn supersedes one', async () => {
    const first = await orphan()
    expect(await m.interrupt(first.session.id, browser)).toBe(true)
    expect(await m.approvals.get(first.approval.id, browser)).toMatchObject({ decision: 'cancelled', reason: 'interrupted by You' })
    expect(await m.get(first.session.id, agentA)).toMatchObject({ status: 'idle' })

    const second = await orphan()
    const turn = await m.send(second.session.id, agentA, 'never mind, do something else')
    await turn.done
    expect(await m.approvals.get(second.approval.id, browser)).toMatchObject({
      decision: 'cancelled',
      reason: 'superseded by a new turn',
    })
  })

  it('handing off a session cancels its pending approval', async () => {
    const { session, approval } = await orphan()
    await m.handoff(session.id, agentA, agentB)
    expect(await m.approvals.get(approval.id, browser)).toMatchObject({ decision: 'cancelled', reason: 'the session was handed off to Agent B' })
  })

  it('lists by visibility: the browser sees all, an agent only its own sessions', async () => {
    const mine = await orphan(agentA)
    const theirs = await orphan(agentB)
    expect((await m.approvals.list(browser, { pending: true })).map((a) => a.id).sort()).toEqual([mine.approval.id, theirs.approval.id].sort())
    expect((await m.approvals.list(agentA, { sessionId: mine.session.id })).map((a) => a.id)).toEqual([mine.approval.id])
    await expect(m.approvals.list(agentA, { sessionId: theirs.session.id })).rejects.toMatchObject({ code: 'not_found' })
    await expect(m.approvals.list(agentA)).rejects.toMatchObject({ code: 'invalid' })
    await expect(m.approvals.get(theirs.approval.id, agentA)).rejects.toMatchObject({ code: 'not_found' })
    await expect(m.approvals.decide(browser, 'not-a-uuid', true)).rejects.toMatchObject({ code: 'not_found' })
    // The panel's decision names its session; a mismatched one is not found.
    await expect(
      m.approvals.decision(browser, { v: 1, type: 'approval.decision', sessionId: theirs.session.id, id: mine.approval.id, approve: true }),
    ).rejects.toMatchObject({ code: 'not_found' })
  })

  it('a sessionless approval (#251 prepare/confirm) is visible to the browser and its requester, and consumed by id', async () => {
    const approval = await m.approvals.create({
      sessionId: null,
      turnId: null,
      toolUseId: 'prepare-1',
      tool: 'mcp__scadbuddy__print_output',
      input: { output: 'o1' },
      tier: 'outward',
      requestedBy: agentA,
    })
    await expect(m.approvals.get(approval.id, agentB)).rejects.toMatchObject({ code: 'not_found' })
    await expect(m.approvals.decide(agentA, approval.id, true)).rejects.toMatchObject({ code: 'forbidden' })
    const waiting = m.approvals.waitFor(approval.id, new AbortController().signal)
    await m.approvals.decide(browser, approval.id, true)
    expect(await waiting).toMatchObject({ decision: 'approved' })
    expect(await m.approvals.consumeById(approval.id)).toMatchObject({ id: approval.id })
    expect(await m.approvals.consumeById(approval.id)).toBeUndefined()
  })

  describe('HTTP routes', () => {
    const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
    function app(ready = true): Hono {
      const a = new Hono()
      registerApprovalRoutes(a, {
        approvals: m.approvals,
        ready: () => Promise.resolve(ready),
        remoteAddress: () => '10.0.0.7',
        origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      })
      return a
    }

    it('guards the writes and answers with { detail }', async () => {
      const { approval } = await orphan()
      const url = `/api/v1/ai/approvals/${approval.id}/deny`
      for (const headers of [{}, { ...UI, origin: 'https://evil.example' }, { ...UI, 'x-forwarded-proto': 'http' }]) {
        const res = await app().request(url, { method: 'POST', headers })
        expect(res.status).toBe(403)
        expect(((await res.json()) as { detail: string }).detail).toMatch(/^approval decisions must come/)
      }
      expect((await app().request(url, { method: 'POST', headers: { ...UI, 'content-type': 'text/plain' }, body: 'x' })).status).toBe(415)
      expect(
        (await app().request(url, { method: 'POST', headers: { ...UI, 'content-type': 'application/json' }, body: '{"input_hash":"nope"}' })).status,
      ).toBe(400)
      expect((await app(false).request(url, { method: 'POST', headers: UI })).status).toBe(503)
      expect((await app().request(`/api/v1/ai/approvals/${approval.id}/maybe`, { method: 'POST', headers: UI })).status).toBe(404)
      // Still pending after all of that.
      expect((await m.approvals.get(approval.id, browser)).decision).toBeNull()

      const denied = await app().request(url, { method: 'POST', headers: UI })
      expect(denied.status).toBe(200)
      expect(await denied.json()).toMatchObject({ id: approval.id, decision: 'denied', decided_by: browser, used: false })
      const again = await app().request(url, { method: 'POST', headers: UI })
      expect(again.status).toBe(409)
      expect(await again.json()).toEqual({ detail: `approval ${approval.id} was already denied` })
    })

    it('refuses a mismatched input hash with 409', async () => {
      const { approval } = await orphan()
      const res = await app().request(`/api/v1/ai/approvals/${approval.id}/approve`, {
        method: 'POST',
        headers: { ...UI, 'content-type': 'application/json' },
        body: JSON.stringify({ input_hash: inputHash('mcp__stub__print', { job: 'other.3mf' }) }),
      })
      expect(res.status).toBe(409)
      expect((await m.approvals.get(approval.id, browser)).decision).toBeNull()
    })

    it('lists a session’s approvals, or 404 for an unknown session', async () => {
      const { session, approval } = await orphan()
      const res = await app().request(`/api/v1/ai/approvals?session=${session.id}`)
      expect(res.status).toBe(200)
      expect(await res.json()).toEqual({
        approvals: [
          expect.objectContaining({ id: approval.id, session_id: session.id, tool: 'mcp__stub__print', decision: null, used: false }),
        ],
      })
      expect((await app().request('/api/v1/ai/approvals?session=00000000-0000-4000-8000-000000000000')).status).toBe(404)
      expect((await app(false).request('/api/v1/ai/approvals')).status).toBe(503)
    })
  })
})
