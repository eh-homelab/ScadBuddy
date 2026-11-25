import { createHash, randomUUID } from 'node:crypto'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApprovalService,
  canonicalJson,
  DEFAULT_APPROVAL_EXPIRY_SECONDS,
  inputHash,
  MIN_APPROVAL_EXPIRY_SECONDS,
  SETTING_APPROVAL_EXPIRY_SECONDS,
  summariseInput,
  sweepErrorText,
} from '../src/approvals/service.js'
import type { Database } from './support/database.js'
import type { HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { registerApprovalRoutes } from '../src/routes/approvals.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, agentB, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { AGENT_SESSIONS_SKIP, agentSessions } from './support/agentSessions.js'
import { respond } from '../src/routes/pendingInput.js'
import { sqlSettings } from './support/sql/settings.js'
import { TEST_HASH_KEY } from './support/sql/audit.js'
import { type BeforeAppend, SqlApprovals } from './support/sql/approvals.js'

// The approval store and its rules without the SDK (#258): ai_approvals in
// Postgres, expiry, visibility, the orphan paths, and the HTTP routes. Since plan
// Step 6.3 no turn parks here (an AgentSession's park is the gate's, ai_pending_input);
// these rows are what an earlier release left, which expire, and MCP prepares. The
// parked-turn flows run on the real SDK in test/approvals.e2e.test.ts.

describe('input binding', () => {
  it('HMACs the tool and a canonical form of the input under a server-side key', () => {
    const key = Buffer.alloc(32, 7)
    expect(canonicalJson({ b: 1, a: { d: [1, { f: 2, e: 3 }], c: null } })).toBe('{"a":{"c":null,"d":[1,{"e":3,"f":2}]},"b":1}')
    expect(inputHash(key, 't', { a: 1, b: 2 })).toBe(inputHash(key, 't', { b: 2, a: 1 }))
    expect(inputHash(key, 't', { a: 1 })).not.toBe(inputHash(key, 't', { a: 2 }))
    expect(inputHash(key, 't', { a: 1 })).not.toBe(inputHash(key, 'u', { a: 1 }))
    expect(inputHash(key, 't', { a: 1 })).toMatch(/^[0-9a-f]{64}$/)
    // Not a bare hash: another key gives another value, so the table alone does not reveal an input.
    expect(inputHash(Buffer.alloc(32, 8), 't', { a: 1 })).not.toBe(inputHash(key, 't', { a: 1 }))
    expect(inputHash(key, 't', { a: 1 })).not.toBe(createHash('sha256').update(canonicalJson({ tool: 't', input: { a: 1 } })).digest('hex'))
  })

  it('summarises through scrubForLog: secrets and secret-named arguments never reach the summary', () => {
    const summary = summariseInput('mcp__x__send', { job: 'box', api_key: 'k-123', note: 'with sk-live-999' }, ['sk-live-999'])
    expect(summary).toBe('{"job":"box","api_key":"[redacted]","note":"with [redacted]"}')
  })
})

// #2382: a failed sweep names the statement it came from, never its parameters.
describe('sweepErrorText', () => {
  it('names the query of a Postgres error, folded and cut, and nothing else of it', () => {
    const err = Object.assign(new Error('invalid input syntax for type uuid: "f"'), {
      query: `UPDATE ai_approvals\n   SET decision = $2\n WHERE id = $1`,
      parameters: ['secret-ish', 'expired'],
    })
    expect(sweepErrorText(err)).toBe('invalid input syntax for type uuid: "f" (in: UPDATE ai_approvals SET decision = $2 WHERE id = $1)')
    expect(sweepErrorText(err)).not.toContain('secret-ish')
    const long = Object.assign(new Error('x'), { query: 'SELECT '.repeat(100) })
    expect(sweepErrorText(long).length).toBeLessThan(320)
    expect(sweepErrorText(new Error('plain'))).toBe('plain')
    expect(sweepErrorText('text')).toBe('text')
  })

  it('says how many more failed when a sweep threw several', () => {
    const first = Object.assign(new Error('boom'), { query: 'UPDATE ai_approvals SET x = $1', parameters: ['secret-ish'] })
    const several = new AggregateError([first, new Error('two'), new Error('three')], '3 approvals failed to expire')
    expect(sweepErrorText(several)).toBe('boom (in: UPDATE ai_approvals SET x = $1) (and 2 more)')
    expect(sweepErrorText(several)).not.toContain('secret-ish')
  })
})

describe('the approval sweeper', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('skips a tick while the last sweep is still running', async () => {
    vi.useFakeTimers()
    let sweeps = 0
    let finish: () => void = () => {}
    const service = Object.create(ApprovalService.prototype) as ApprovalService
    service.expireDue = () => {
      sweeps += 1
      return new Promise<number>((resolve) => {
        finish = () => resolve(0)
      })
    }
    const stop = service.startSweeper(100)
    await vi.advanceTimersByTimeAsync(100)
    expect(sweeps).toBe(1)
    await vi.advanceTimersByTimeAsync(500)
    expect(sweeps).toBe(1)
    finish()
    await vi.advanceTimersByTimeAsync(100)
    expect(sweeps).toBe(2)
    stop()
  })
})

describe.skipIf(!TEST_DATABASE_URL)(`approvals in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  let values: Map<string, unknown>
  /** A fault in the approvals' routes, in a write's transaction before its events are appended. */
  let fault: BeforeAppend | undefined

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    values = new Map()
    fault = undefined
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: runner,
      settings: { get: <T>(key: string) => Promise.resolve(values.get(key) as T) },
      approvalRoutes: new SqlApprovals(db.sql, { beforeAppend: (sessionId, events) => fault?.(sessionId, events) ?? Promise.resolve() }),
    })
  })
  afterEach(async () => {
    await drop()
  })

  /** A pending MCP prepare of `owner`'s (#251): no session, as every approval now (#7323). */
  async function orphan(owner = agentA, input: Record<string, unknown> = { job: 'box.3mf' }) {
    const approval = await m.approvals.create({
      sessionId: null,
      turnId: null,
      toolUseId: 'toolu_1',
      tool: 'mcp__stub__print',
      input,
      tier: 'outward',
      requestedBy: owner,
    })
    return { approval }
  }

  /** A pending approval of a session an earlier release's turn left (none is written now). */
  async function sessionRow(owner = agentA) {
    const { session } = await m.start(owner, { origin: 'mcp', title: 't' })
    const approval = await m.approvals.create({
      sessionId: session.id,
      turnId: null,
      toolUseId: 'toolu_1',
      tool: 'mcp__stub__print',
      input: { job: 'box.3mf' },
      tier: 'outward',
      requestedBy: owner,
    })
    return { session, approval }
  }

  it('reads the expiry window from ai_settings, within bounds', async () => {
    expect(await m.approvals.expirySeconds()).toBe(DEFAULT_APPROVAL_EXPIRY_SECONDS)
    const settings = sqlSettings(db.sql)
    const service = new ApprovalService({ routes: new SqlApprovals(db.sql), events: m.events, settings, hashKey: () => TEST_HASH_KEY })
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
      inputHash: m.approvals.hash('mcp__stub__print', { job: 'box.3mf', password: 'hunter2-long-secret' }),
    })
    const [raw] = await db.sql`SELECT row_to_json(a)::text AS row FROM ai_approvals a WHERE id = ${approval.id}`
    expect(String(raw?.row)).not.toContain('hunter2-long-secret')
  })

  // #4442: a tool that resolved its input parks its own line, redacted; the hash is of the resolved input.
  it("stores a resolving tool's own line, redacted and on one line, above every argument, and the hash of what it resolved", async () => {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    const input = { printer: 'p', printer_id: 2, bed_type: 'Cool Plate' }
    const approval = await m.approvals.create({
      sessionId: session.id,
      turnId: null,
      toolUseId: 'toolu_9',
      tool: 'mcp__scadbuddy__print_output',
      input,
      summary: 'Print box on printer "Garage"\nApproved: yes with hunter2-long-secret',
      secrets: ['hunter2-long-secret'],
      tier: 'outward',
      requestedBy: agentA,
    })
    const [line, rest, ...more] = approval.inputSummary.split('\n')
    expect(line).toBe('Print box on printer "Garage" Approved: yes with [redacted]')
    expect(JSON.parse(rest!)).toEqual(input)
    expect(more).toEqual([])
    expect(approval.inputHash).toBe(m.approvals.hash('mcp__scadbuddy__print_output', input))
  })

  // #4442 gate re-review N1: the card reads approval.required's capped summary; a resolving
  // tool's line must not take the room its arguments had.
  it("keeps the arguments' full room in approval.required beside a resolving tool's line", async () => {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    const input = { printer: 'p', note: 'x'.repeat(1000) }
    await m.approvals.create({
      sessionId: session.id,
      turnId: null,
      toolUseId: 'toolu_10',
      tool: 'mcp__scadbuddy__print_output',
      input,
      summary: `Print on printer "${'G'.repeat(400)}"`,
      tier: 'outward',
      requestedBy: agentA,
    })
    const events = (await m.events.read(session.id, 0, 1000)).map((e) => e.event)
    const required = events.find((e) => e.type === 'approval.required') as { summary: string }
    const [line, args] = required.summary.split('\n')
    expect(line!.length).toBe('mcp__scadbuddy__print_output '.length + 200)
    // As much as without a line: the cap is 500 for the tool's name and its arguments.
    expect(args!.length).toBe(500 - 'mcp__scadbuddy__print_output '.length)
    expect(args!.endsWith('…')).toBe(true)
  })

  // #2382: while one row throws, no other due approval must stay unexpired.
  it('expires every other due approval when one of them fails, and reports the failure', async () => {
    // The fault is in a session row's events, so these are rows an earlier release left.
    const first = await sessionRow()
    const second = await sessionRow(agentB)
    await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 second'`
    const boom = new Error('invalid input syntax for type uuid: "f"')
    let failed = false
    // The first one's expiry fails in its transaction, so it rolls back.
    fault = async (sessionId) => {
      if (sessionId === first.session.id && !failed) {
        failed = true
        throw boom
      }
    }
    await expect(m.approvals.expireDue()).rejects.toBe(boom)
    const decisions = async () =>
      Object.fromEntries(
        (await db.sql<{ id: string; decision: string | null }[]>`SELECT id, decision FROM ai_approvals`).map((r) => [r.id, r.decision]),
      )
    expect(await decisions()).toEqual({ [first.approval.id]: null, [second.approval.id]: 'expired' })
    // The failed one is tried again on the next sweep.
    expect(await m.approvals.expireDue()).toBe(1)
    expect((await decisions())[first.approval.id]).toBe('expired')
  })

  // #2382: a page of rows that keep failing never holds back the ones after it.
  it('pages past due approvals that keep failing to the ones after them', async () => {
    const stuck = [await sessionRow(), await sessionRow(agentB)]
    const later = await sessionRow()
    await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 minute' WHERE id IN ${db.sql(stuck.map((o) => o.approval.id))}`
    await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = ${later.approval.id}`
    const failing = new Set(stuck.map((o) => o.session.id))
    fault = async (sessionId) => {
      if (failing.has(sessionId)) throw new Error('boom')
    }
    const thrown = await m.approvals.expireDue(2).then(
      () => undefined,
      (e: unknown) => e,
    )
    expect(thrown).toBeInstanceOf(AggregateError)
    expect((thrown as AggregateError).errors).toHaveLength(2)
    const [row] = await db.sql<{ decision: string | null }[]>`SELECT decision FROM ai_approvals WHERE id = ${later.approval.id}`
    expect(row!.decision).toBe('expired')
  })

  // #2949 review: one sweep takes a bounded number of pages; the next takes the rest.
  it('leaves what is past its page budget to the next sweep, and says so', async () => {
    const made = []
    for (let i = 0; i < 5; i += 1) made.push(await orphan(i % 2 ? agentB : agentA))
    // Oldest first: the last made is the newest due, and the one left over.
    for (const [i, o] of made.entries()) {
      await db.sql`UPDATE ai_approvals SET expires_at = now() - make_interval(secs => ${10 - i}) WHERE id = ${o.approval.id}`
    }
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      expect(await m.approvals.expireDue(2, 2)).toBe(4)
      expect(warn).toHaveBeenCalledWith(expect.stringContaining('stopped at its cap of 2 pages of 2'))
      const pending = await db.sql<{ id: string }[]>`SELECT id FROM ai_approvals WHERE decision IS NULL`
      expect(pending.map((r) => r.id)).toEqual([made[4]!.approval.id])
      warn.mockClear()
      expect(await m.approvals.expireDue(2, 2)).toBe(1)
      expect(warn).not.toHaveBeenCalled()
    } finally {
      warn.mockRestore()
    }
  })

  it('expires an approval that nobody decided', async () => {
    const { approval } = await orphan()
    expect(await m.approvals.expireDue()).toBe(0)
    await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = ${approval.id}`
    expect(await m.approvals.expireDue()).toBe(1)
    expect(await m.approvals.get(approval.id, browser)).toMatchObject({ decision: 'expired', reason: 'no decision before it expired' })
    await expect(m.approvals.decide(browser, approval.id, true)).rejects.toMatchObject({ code: 'expired' })
  })

  it('an approval decided just as it expires cannot be approved', async () => {
    const { approval } = await orphan()
    await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = ${approval.id}`
    // Not swept yet: the decision itself finds it due.
    await expect(m.approvals.decide(browser, approval.id, true)).rejects.toMatchObject({ code: 'expired', status: 410 })
    expect((await m.approvals.get(approval.id, browser)).decision).toBe('expired')
  })

  it('lists by visibility: the browser sees all, an agent only its own', async () => {
    const mine = await orphan(agentA)
    const theirs = await orphan(agentB)
    expect((await m.approvals.listVisiblePending(browser)).map((a) => a.id).sort()).toEqual([mine.approval.id, theirs.approval.id].sort())
    expect((await m.approvals.listVisiblePending(agentA)).map((a) => a.id)).toEqual([mine.approval.id])
    await expect(m.approvals.get(theirs.approval.id, agentA)).rejects.toMatchObject({ code: 'not_found' })
    await expect(m.approvals.decide(browser, 'not-a-uuid', true)).rejects.toMatchObject({ code: 'not_found' })
    // The panel's old decision frame names a session, which no approval has.
    await expect(
      m.approvals.decision(browser, { v: 1, type: 'approval.decision', sessionId: randomUUID(), id: mine.approval.id, approve: true }),
    ).rejects.toMatchObject({ code: 'not_found' })
  })

  // #7323: a session's rows are its history. Nothing parks on one now (prod had none pending
  // on 2026-11-23); one left is not listed, read or decided, and the sweep expires it.
  it("never lists, shows or decides a session's row, and still expires it", async () => {
    const { approval } = await sessionRow(agentA)
    expect(await m.approvals.listVisiblePending(browser)).toEqual([])
    expect(await m.approvals.listVisiblePending(agentA)).toEqual([])
    await expect(m.approvals.get(approval.id, browser)).rejects.toMatchObject({ code: 'not_found' })
    await expect(m.approvals.decide(browser, approval.id, true)).rejects.toMatchObject({ code: 'not_found' })
    await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = ${approval.id}`
    expect(await m.approvals.expireDue()).toBe(1)
    expect((await db.sql<{ decision: string }[]>`SELECT decision FROM ai_approvals WHERE id = ${approval.id}`)[0]?.decision).toBe('expired')
  })

  it('a sessionless approval (#251 prepare/confirm) is visible to the browser and its requester, and consumed once by its requester', async () => {
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
    expect(await m.approvals.decide(browser, approval.id, true)).toMatchObject({ decision: 'approved' })
    const hash = m.approvals.hash('mcp__scadbuddy__print_output', { output: 'o1' })
    expect(await m.approvals.consumePrepared(approval.id, agentB, hash)).toBeUndefined()
    expect(await m.approvals.consumePrepared(approval.id, agentA, hash)).toMatchObject({ id: approval.id })
    expect(await m.approvals.consumePrepared(approval.id, agentA, hash)).toBeUndefined()
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
        body: JSON.stringify({ input_hash: m.approvals.hash('mcp__stub__print', { job: 'other.3mf' }) }),
      })
      expect(res.status).toBe(409)
      expect((await m.approvals.get(approval.id, browser)).decision).toBeNull()
    })

    it('lists the pending approvals to the UI', async () => {
      const { approval } = await orphan()
      // A same-origin GET carries no Origin: the Host (via the trusted ingress) decides.
      const read = { host: UI.host, 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
      const res = await app().request('/api/v1/ai/approvals', { headers: read })
      expect(res.status).toBe(200)
      expect(await res.json()).toEqual({
        approvals: [expect.objectContaining({ id: approval.id, session_id: null, tool: 'mcp__stub__print', decision: null, used: false, voided: false })],
      })
      expect((await app().request('/api/v1/ai/approvals', { headers: UI })).status).toBe(200)
      expect((await app(false).request('/api/v1/ai/approvals', { headers: read })).status).toBe(503)
    })

    it('guards the reads with the same transport and origin rules', async () => {
      await orphan()
      const url = '/api/v1/ai/approvals'
      const refused = [
        {}, // no ingress, not loopback
        { host: UI.host, 'x-forwarded-proto': 'http' }, // plain HTTP
        { host: 'evil.example', 'x-forwarded-proto': 'https' }, // rebinding: another Host
        { ...UI, origin: 'https://evil.example' }, // another page's fetch
        { host: UI.host, 'x-forwarded-proto': 'https', 'sec-fetch-site': 'cross-site' },
      ]
      for (const headers of refused) {
        const res = await app().request(url, { headers })
        expect(res.status).toBe(403)
        expect(((await res.json()) as { detail: string }).detail).toMatch(/^approval reads must/)
      }
    })
  })
})

// #3583 and the review of #3685, for an AgentSession's park (plan Step 6.3): the decision
// and the session's move back to `running` are one commit, and two overlapping decisions in
// one session still move it back. The park is the gate's (ai_pending_input), answered
// through `respond`; the scripted turn runs through AgentSession (support/agentSessions.ts).
describe.skipIf(!TEST_DATABASE_URL || AGENT_SESSIONS_SKIP !== '')(
  `an AgentSession's approvals in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}${AGENT_SESSIONS_SKIP}`,
  () => {
    agentSessions()
    let db: Database
    let drop: () => Promise<void>

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
    })
    afterEach(async () => {
      await drop()
    })

    const result = (run: HarnessRun) =>
      ({ type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume }) as unknown as SDKMessage
    /** A manager whose turn parks on each of `jobs` at once, then ends with a result once `held` resolves. */
    async function parking(jobs: string[], held: Promise<void> = Promise.resolve()) {
      const runner = (run: HarnessRun): AsyncIterable<SDKMessage> =>
        (async function* () {
          await Promise.resolve()
          await Promise.all(
            jobs.map((job) =>
              run.approvalGate!({ toolName: 'mcp__stub__print', input: { job }, toolUseId: `toolu_${job}`, tier: 'outward', signal: run.signal! }),
            ),
          )
          await held
          yield result(run)
        })()
      const p = manager({ sql: db.sql, paths: await tempPaths(), run: runner })
      const { session, turn } = await p.start(agentA, { origin: 'mcp', prompt: 'print' })
      let ids: string[] = []
      await expect
        .poll(async () => (ids = (await db.sql<{ request_id: string }[]>`
          SELECT request_id FROM ai_pending_input WHERE session_id = ${session.id} ORDER BY request_id`).map((r) => r.request_id)).length)
        .toBe(jobs.length)
      return { p, session, turn: turn!, ids }
    }
    const statuses = async (mm: SessionManager, sessionId: string) =>
      (await mm.events.read(sessionId, 0, 1000)).flatMap(({ event: e }) => (e.type === 'session.status' ? [e.status] : []))
    /** Each logged event's type, status and the transaction that wrote it (`xmin`). */
    const logged = (sessionId: string) =>
      db.sql<{ type: string; status: string | null; tx: string }[]>`
        SELECT event::jsonb->>'type' AS type, event::jsonb->>'status' AS status, xmin::text AS tx
          FROM ai_session_events WHERE session_id = ${sessionId} ORDER BY seq`

    it('logs the move back to running in the same commit as the decision (#3583)', async () => {
      const { p, session, turn, ids } = await parking(['box.3mf'])
      expect(await respond(p, browser, ids[0]!, { kind: 'approval', decision: 'approve' })).toMatchObject({ outcome: 'approved' })
      expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })

      const rows = await logged(session.id)
      const decision = rows.find((r) => r.type === 'approval.resolved')!
      expect(rows.filter((r) => r.tx === decision.tx && r.type === 'session.status').map((r) => r.status)).toEqual(['running'])
      expect(await statuses(p, session.id)).toEqual(['idle', 'running', 'waiting_approval', 'running', 'idle'])
    })

    it('two overlapping decisions in one session move it back from waiting_approval (review of #3685)', async () => {
      let hold!: () => void
      const held = new Promise<void>((r) => (hold = r))
      const { p, session, turn, ids } = await parking(['a.3mf', 'b.3mf'], held)
      expect((await p.get(session.id, agentA)).status).toBe('waiting_approval')

      const outcomes = await Promise.all(ids.map((id) => respond(p, browser, id, { kind: 'approval', decision: 'approve' })))
      expect(outcomes.map((o) => o.outcome)).toEqual(['approved', 'approved'])
      expect(await db.sql`SELECT 1 FROM ai_pending_input WHERE session_id = ${session.id}`).toHaveLength(0)
      // Moved back once, by whichever decision committed last, and never left waiting.
      await expect.poll(async () => (await p.get(session.id, agentA)).status).toBe('running')
      expect((await logged(session.id)).filter((r) => r.type === 'session.status' && r.status === 'running')).toHaveLength(2)
      hold()
      expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    })
  },
)
