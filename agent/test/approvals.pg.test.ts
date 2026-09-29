import { createHash, randomBytes } from 'node:crypto'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  approvalHashKey,
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
import type { HarnessRun } from '../src/harness/run.js'
import { kekFromBase64 } from '../src/secrets.js'
import { originPolicy } from '../src/http/origins.js'
import { registerApprovalRoutes } from '../src/routes/approvals.js'
import type { EventLog } from '../src/sessions/eventLog.js'
import type { SessionManager, TurnPrincipal } from '../src/sessions/manager.js'
import type { Owner } from '../src/sessions/protocol.js'
import { hasTier, type Tier } from '../src/auth/principal.js'
import { turnPrincipal } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { SERVER_NAME } from '../src/tools/projections.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, agentB, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// The approval store and its rules without the SDK (#258): ai_approvals in
// Postgres, expiry, visibility, the orphan paths, and the HTTP routes. The
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

  it('derives the key from the KEK, the same across restarts and different from the KEK', () => {
    const kek = kekFromBase64(randomBytes(32).toString('base64'))
    expect(approvalHashKey(kek).equals(approvalHashKey(kek))).toBe(true)
    expect(approvalHashKey(kek)).toHaveLength(32)
    expect(approvalHashKey(kek).equals(kek.key)).toBe(false)
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
      inputHash: m.approvals.hash('mcp__stub__print', { job: 'box.3mf', password: 'hunter2-long-secret' }),
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

  it('a decision racing create cannot log approval.resolved before approval.required', async () => {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    // An event log that tries to decide the approval just before
    // `approval.required` is written: the fastest possible decision.
    let raced: unknown
    const events = Object.create(m.events) as EventLog
    events.append = async (sessionId, batch, tx) => {
      const required = batch.find((e) => e.type === 'approval.required')
      if (required && 'id' in required) {
        raced = await service.decide(browser, String(required.id), false).catch((err: unknown) => err)
      }
      return m.events.append(sessionId, batch, tx)
    }
    const service = new ApprovalService({ sql: db.sql, events })
    const approval = await service.create({
      sessionId: session.id,
      turnId: null,
      toolUseId: 'toolu_1',
      tool: 'mcp__stub__print',
      input: { job: 'box.3mf' },
      tier: 'outward',
      requestedBy: agentA,
    })
    // The row is not there to decide until it commits with its event.
    expect(raced).toMatchObject({ code: 'not_found' })
    await service.decide(browser, approval.id, false)
    const types = (await m.events.read(session.id, 0, 1000)).map((e) => e.event.type)
    expect(types.filter((t) => t.startsWith('approval.'))).toEqual(['approval.required', 'approval.resolved'])
  })

  it('an approved orphan is used once, only by the turn it is bound to, with the same tool and input', async () => {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    const turnA = '11111111-1111-4111-8111-111111111111'
    const turnB = '22222222-2222-4222-8222-222222222222'
    // A service whose resume only binds, to look at the row in between.
    const service: ApprovalService = new ApprovalService({
      sql: db.sql,
      events: m.events,
      hashKey: Buffer.alloc(32, 1),
      resume: async (a) => ((await service.bindResume(a.id, turnA)) ? { resumed: true } : { resumed: false, reason: 'x' }),
    })
    const fresh = await service.create({
      sessionId: session.id,
      turnId: null,
      toolUseId: 'toolu_2',
      tool: 'mcp__stub__print',
      input: { job: 'box.3mf' },
      tier: 'outward',
      requestedBy: agentA,
    })
    await service.decide(browser, fresh.id, true)
    const decided = await service.get(fresh.id, browser)
    expect(decided).toMatchObject({ decision: 'approved', resumeTurnId: turnA })
    expect(Date.parse(decided.usableUntil!) - Date.parse(decided.decidedAt!)).toBeCloseTo(DEFAULT_APPROVAL_EXPIRY_SECONDS * 1000, -3)

    const hash = service.hash('mcp__stub__print', { job: 'box.3mf' })
    // Another turn (a new owner's, after a handoff; the next turn): never.
    expect(await service.consume(session.id, turnB, 'mcp__stub__print', hash)).toBeUndefined()
    expect(await service.consume(session.id, turnA, 'mcp__stub__print', service.hash('mcp__stub__print', { job: 'other' }))).toBeUndefined()
    expect(await service.consume(session.id, turnA, 'mcp__other__print', hash)).toBeUndefined()
    expect(await service.consume(session.id, turnA, 'mcp__stub__print', hash)).toMatchObject({ id: fresh.id })
    expect(await service.consume(session.id, turnA, 'mcp__stub__print', hash)).toBeUndefined()
    expect(await service.consumeById(fresh.id)).toBeUndefined()
  })

  it('a decision after turn A released the session resumes turn B, and A’s trailing clean-up leaves B’s approval alone', async () => {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    const turnA = '55555555-5555-4555-8555-555555555555'
    const turnB = '66666666-6666-4666-8666-666666666666'
    const service: ApprovalService = new ApprovalService({
      sql: db.sql,
      events: m.events,
      resume: async (a) => ((await service.bindResume(a.id, turnB)) ? { resumed: true } : { resumed: false, reason: 'x' }),
    })
    const input = { job: 'box.3mf' }
    const parkedOnA = await service.create({
      sessionId: session.id,
      turnId: turnA,
      toolUseId: 'toolu_a',
      tool: 'mcp__stub__print',
      input,
      tier: 'outward',
      requestedBy: agentA,
    })
    // Turn A has released the session (no claim): the decision is an orphan's and resumes turn B.
    await service.decide(browser, parkedOnA.id, true)
    expect(await service.get(parkedOnA.id, browser)).toMatchObject({ resumeTurnId: turnB, revokedAt: null })
    // Then A's finish runs its post-release clean-up.
    expect(await service.revokeUnused(session.id, 'it was decided as its turn ended', { turnId: turnA })).toBe(0)
    expect(await service.consume(session.id, turnB, 'mcp__stub__print', service.hash('mcp__stub__print', input))).toMatchObject({
      id: parkedOnA.id,
    })

    // F1/F3 intact: an unbound approval of turn A is still voided by the same clean-up.
    const unbound = await service.create({
      sessionId: session.id,
      turnId: turnA,
      toolUseId: 'toolu_a2',
      tool: 'mcp__stub__print',
      input,
      tier: 'outward',
      requestedBy: agentA,
    })
    await db.sql`UPDATE ai_approvals SET decision = 'approved', decided_at = now(), usable_until = now() + interval '1 hour'
                 WHERE id = ${unbound.id}`
    expect(await service.revokeUnused(session.id, 'it was decided as its turn ended', { turnId: turnA })).toBe(1)
    expect((await service.get(unbound.id, browser)).revokedAt).not.toBeNull()
  })

  it('an approval stays usable only until the usable_until fixed at its decision', async () => {
    const { session } = await orphan()
    const turnA = '11111111-1111-4111-8111-111111111111'
    const service: ApprovalService = new ApprovalService({
      sql: db.sql,
      events: m.events,
      resume: async (a) => ((await service.bindResume(a.id, turnA)) ? { resumed: true } : { resumed: false, reason: 'x' }),
    })
    const [pending] = await service.list(browser, { sessionId: session.id, pending: true })
    await service.decide(browser, pending!.id, true)
    // Changing the setting afterwards does not move it; time passing does.
    values.set(SETTING_APPROVAL_EXPIRY_SECONDS, 86_400)
    await db.sql`UPDATE ai_approvals SET usable_until = now() - interval '1 second' WHERE id = ${pending!.id}`
    expect(await service.consume(session.id, turnA, pending!.tool, pending!.inputHash)).toBeUndefined()
  })

  it('an approved orphan whose session cannot resume is voided at once, and the session is told', async () => {
    const { session, approval } = await orphan()
    // Another replica holds the session with a live turn: the resume cannot claim it.
    await db.sql`UPDATE ai_sessions SET turn_id = gen_random_uuid(), lease_until = now() + interval '1 minute' WHERE id = ${session.id}`
    await m.approvals.decide(browser, approval.id, true)
    const after = await m.approvals.get(approval.id, browser)
    expect(after).toMatchObject({ decision: 'approved', consumedAt: null, resumeTurnId: null })
    expect(after.revokedAt).not.toBeNull()
    expect(after.reason).toMatch(/could not resume: it is running another turn/)
    const events = (await m.events.read(session.id, 0, 1000)).map((e) => e.event)
    await expectPanelAccepts(events)
    expect(events.at(-1)).toMatchObject({ type: 'error', code: 'approval_void', message: expect.stringContaining(approval.id) })
    // Nothing can use it now, whatever turn asks.
    await db.sql`UPDATE ai_sessions SET turn_id = NULL, lease_until = NULL WHERE id = ${session.id}`
    expect(await m.approvals.bindResume(approval.id, '33333333-3333-4333-8333-333333333333')).toBe(false)
  })

  it('a resume that fails after claiming the session gives the claim back', async () => {
    const { session, approval } = await orphan()
    const failing = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: scriptedRunner(() => ({ reply: 'ok' })).runner,
      settings: { get: <T>(key: string) => Promise.resolve(values.get(key) as T) },
    })
    // Make startTurn throw: the events table refuses this session's appends.
    await db.sql`CREATE FUNCTION refuse() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF NEW.event LIKE '%user.turn%' THEN RAISE EXCEPTION 'append refused'; END IF; RETURN NEW; END $$`
    await db.sql`CREATE TRIGGER refuse BEFORE INSERT ON ai_session_events FOR EACH ROW EXECUTE FUNCTION refuse()`
    await failing.approvals.decide(browser, approval.id, true)
    expect(await failing.get(session.id, agentA)).toMatchObject({ status: 'idle', turnActive: false })
    const after = await failing.approvals.get(approval.id, browser)
    expect(after.revokedAt).not.toBeNull()
    expect(after.reason).toMatch(/append refused/)
  })

  it('a resumed turn that does not use its approval voids it when it ends', async () => {
    const { session, approval } = await orphan()
    await m.approvals.decide(browser, approval.id, true)
    await expect.poll(async () => (await m.get(session.id, agentA)).turnActive, { timeout: 5000 }).toBe(false)
    const after = await m.approvals.get(approval.id, browser)
    expect(after).toMatchObject({ decision: 'approved', consumedAt: null, reason: 'the turn ended' })
    expect(after.resumeTurnId).not.toBeNull()
    expect(after.revokedAt).not.toBeNull()
  })

  it('F1 repro: after interrupt and handoff, the new owner’s turn cannot use an old approval', async () => {
    const { session, approval } = await orphan()
    // Approved while the session is busy elsewhere (so nothing resumes), then the hold goes.
    await db.sql`UPDATE ai_sessions SET turn_id = gen_random_uuid(), lease_until = now() + interval '1 minute' WHERE id = ${session.id}`
    await m.approvals.decide(browser, approval.id, true)
    await db.sql`UPDATE ai_sessions SET turn_id = NULL, lease_until = NULL WHERE id = ${session.id}`
    expect(await m.interrupt(session.id, browser)).toBe(false)
    await m.handoff(session.id, agentA, agentB).then(() => m.acceptHandoff(session.id, agentB))
    const turn = await m.send(session.id, agentB, 'print the box')
    await turn.done
    const [{ turn_id: none } = { turn_id: null }] = await db.sql<{ turn_id: string | null }[]>`SELECT turn_id FROM ai_sessions WHERE id = ${session.id}`
    expect(none).toBeNull()
    // Whatever turn id agent B's turn had, nothing can consume the approval.
    const rows = await db.sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM ai_approvals WHERE id = ${approval.id} AND revoked_at IS NULL AND consumed_at IS NULL`
    expect(rows[0]?.n).toBe(0)
  })

  it('interrupt and handoff void an approved-but-unused approval of the session', async () => {
    for (const end of ['interrupt', 'handoff'] as const) {
      const { session, approval } = await orphan()
      const turnA = '44444444-4444-4444-8444-444444444444'
      await db.sql`UPDATE ai_approvals SET decision = 'approved', decided_at = now(), usable_until = now() + interval '1 hour',
                   resume_turn_id = ${turnA} WHERE id = ${approval.id}`
      if (end === 'interrupt') expect(await m.interrupt(session.id, browser)).toBe(true)
      else await m.handoff(session.id, agentA, agentB).then(() => m.acceptHandoff(session.id, agentB))
      const after = await m.approvals.get(approval.id, browser)
      expect(after.revokedAt).not.toBeNull()
      expect(await m.approvals.consume(session.id, turnA, approval.tool, approval.inputHash)).toBeUndefined()
    }
  })

  it('F3: a decision that lands while its turn is finishing is voided, and the session does not stay waiting', async () => {
    let hold!: () => void
    const held = new Promise<void>((r) => {
      hold = r
    })
    let parkedId: string | undefined
    const runner = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        try {
          await run.approvalGate!({
            toolName: 'mcp__stub__print',
            input: { job: 'box.3mf' },
            toolUseId: 'toolu_f3',
            tier: 'outward',
            signal: run.signal!,
          })
        } catch {
          // The shutdown aborted the wait; the turn is now finishing, slowly.
          await held
          throw new Error('Claude Code process aborted by user')
        }
        yield* []
      })()
    const f3 = manager({ sql: db.sql, paths: await tempPaths(), run: runner, approvalPollMs: 20 })
    const { session } = await f3.start(agentA, { origin: 'mcp', prompt: 'print' })
    await expect.poll(async () => {
      parkedId = (await f3.approvals.list(browser, { sessionId: session.id, pending: true }))[0]?.id
      return parkedId
    }).toBeDefined()
    f3.abortAll() // shutdown: the approval stays pending, the turn still holds the session
    await expect.poll(async () => (await f3.get(session.id, agentA)).turnActive).toBe(true)
    await f3.approvals.decide(browser, parkedId!, true) // looks parked: nobody resumes
    hold()
    await expect.poll(async () => (await f3.get(session.id, agentA)).turnActive, { timeout: 5000 }).toBe(false)
    expect(await f3.get(session.id, agentA)).toMatchObject({ status: 'idle' })
    const after = await f3.approvals.get(parkedId!, browser)
    // Voided by the finishing turn, before or after it released the session.
    expect(after).toMatchObject({ decision: 'approved', consumedAt: null, reason: expect.stringMatching(/turn ended/) })
    expect(after.revokedAt).not.toBeNull()
  })

  describe("a resumed orphan's turn (PR #715 review)", () => {
    // A registry tool only an outward principal is offered (tools/harness.ts `mcpServers`).
    const outward = ALL_TOOLS.find((t) => t.risk === 'outward' && !t.name.startsWith('sessions_'))!
    const outwardName = `mcp__${SERVER_NAME}__${outward.name}`

    /** A manager whose turns park on `outwardName`, recording what each turn is offered. */
    async function recording(currentTiers?: (owner: Owner) => Promise<readonly Tier[] | undefined>) {
      const offered: string[][] = []
      const runner = (run: HarnessRun): AsyncIterable<SDKMessage> =>
        (async function* () {
          await Promise.resolve()
          if (offered.length > 1) {
            yield* [] // the resumed turn: its tools are what is checked
            return
          }
          await run.approvalGate!({
            toolName: outwardName,
            input: { url: 'https://example.com/box.scad' },
            toolUseId: 'toolu_resume',
            tier: 'outward',
            signal: run.signal!,
          }).catch(() => {})
          throw new Error('Claude Code process aborted by user')
        })()
      const r = manager({
        sql: db.sql,
        paths: await tempPaths(),
        run: runner,
        approvalPollMs: 20,
        // What tools/harness.ts offers a turn, by name.
        mcpServers: (session: { owner: Owner }, turn?: TurnPrincipal) => {
          const principal = turnPrincipal(session.owner, turn)
          offered.push(ALL_TOOLS.filter((t) => hasTier(principal, t.risk)).map((t) => `mcp__${SERVER_NAME}__${t.name}`))
          return {}
        },
        ...(currentTiers ? { currentTiers } : {}),
      })
      return { r, offered }
    }

    /** A token-owned session sent to with outward tiers, whose turn parked and then lost its process. */
    async function parkedThenRestarted(r: SessionManager) {
      const { session, turn } = await r.start(agentA, { origin: 'mcp', prompt: 'import it', tiers: ['read', 'write', 'outward'] })
      let parked: string | undefined
      await expect.poll(async () => {
        parked = (await r.approvals.list(browser, { sessionId: session.id, pending: true }))[0]?.id
        return parked
      }).toBeDefined()
      r.abortAll() // shutdown: the approval stays pending
      await turn!.done
      await expect.poll(async () => (await r.get(session.id, agentA)).turnActive).toBe(false)
      return { session, approvalId: parked! }
    }

    it("runs with the asking turn's tiers, so the approved outward tool is offered again", async () => {
      const { r, offered } = await recording()
      const { session, approvalId } = await parkedThenRestarted(r)
      expect(offered[0]).toContain(outwardName)
      expect((await r.approvals.get(approvalId, browser)).requestedTiers).toEqual(['read', 'write', 'outward'])

      await r.approvals.decide(browser, approvalId, true)
      await expect.poll(() => offered.length).toBe(2)
      await expect.poll(async () => (await r.get(session.id, agentA)).turnActive, { timeout: 5000 }).toBe(false)
      // Without the recorded tiers this turn was `read` only and not offered the tool it was told to call again.
      expect(offered[1]).toContain(outwardName)
      expect(offered[1]).toEqual(offered[0])
    })

    it('gets no more than the owner holds now: a downgraded token loses the tool, a revoked one keeps `read`', async () => {
      let now: readonly Tier[] = ['read', 'write']
      const { r, offered } = await recording((owner) => Promise.resolve(owner.id === agentA.id ? now : undefined))
      const first = await parkedThenRestarted(r)
      await r.approvals.decide(browser, first.approvalId, true)
      await expect.poll(() => offered.length).toBe(2)
      await expect.poll(async () => (await r.get(first.session.id, agentA)).turnActive, { timeout: 5000 }).toBe(false)
      expect(offered[1]).not.toContain(outwardName)
      expect(offered[1]).toContain(`mcp__${SERVER_NAME}__sessions_send`) // write, still held

      now = []
      offered.length = 0
      const second = await parkedThenRestarted(r)
      await r.approvals.decide(browser, second.approvalId, true)
      await expect.poll(() => offered.length).toBe(2)
      await expect.poll(async () => (await r.get(second.session.id, agentA)).turnActive, { timeout: 5000 }).toBe(false)
      expect(offered[1]).toEqual(ALL_TOOLS.filter((t) => t.risk === 'read').map((t) => `mcp__${SERVER_NAME}__${t.name}`))
    })

    it('are not carried to another owner: a stored approval of agent A resumed in B’s session runs with B’s default', async () => {
      const { r, offered } = await recording()
      const { session } = await r.start(agentB, { origin: 'mcp', title: 't' })
      await db.sql`UPDATE ai_sessions SET status = 'waiting_approval' WHERE id = ${session.id}`
      const approval = await r.approvals.create({
        sessionId: session.id,
        turnId: null,
        toolUseId: 'toolu_1',
        tool: outwardName,
        input: { url: 'https://example.com/box.scad' },
        tier: 'outward',
        requestedBy: agentA,
        requestedTiers: ['read', 'write', 'outward'],
      })
      offered.push([]) // so the runner treats the next turn as the resumed one
      await r.approvals.decide(browser, approval.id, true)
      await expect.poll(() => offered.length).toBe(2)
      expect(offered[1]).not.toContain(outwardName)
      expect(offered[1]).not.toContain(`mcp__${SERVER_NAME}__sessions_send`)
    })
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
    await m.handoff(session.id, agentA, agentB).then(() => m.acceptHandoff(session.id, agentB))
    expect(await m.approvals.get(approval.id, browser)).toMatchObject({ decision: 'cancelled', reason: 'the session was handed off to another MCP token' })
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
        body: JSON.stringify({ input_hash: m.approvals.hash('mcp__stub__print', { job: 'other.3mf' }) }),
      })
      expect(res.status).toBe(409)
      expect((await m.approvals.get(approval.id, browser)).decision).toBeNull()
    })

    it('lists a session’s approvals to the UI, or 404 for an unknown session', async () => {
      const { session, approval } = await orphan()
      // A same-origin GET carries no Origin: the Host (via the trusted ingress) decides.
      const read = { host: UI.host, 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
      const res = await app().request(`/api/v1/ai/approvals?session=${session.id}`, { headers: read })
      expect(res.status).toBe(200)
      expect(await res.json()).toEqual({
        approvals: [
          expect.objectContaining({ id: approval.id, session_id: session.id, tool: 'mcp__stub__print', decision: null, used: false, voided: false }),
        ],
      })
      expect((await app().request(`/api/v1/ai/approvals?session=${session.id}`, { headers: UI })).status).toBe(200)
      expect((await app().request('/api/v1/ai/approvals?session=00000000-0000-4000-8000-000000000000', { headers: read })).status).toBe(404)
      expect((await app(false).request('/api/v1/ai/approvals', { headers: read })).status).toBe(503)
    })

    it('guards the reads with the same transport and origin rules', async () => {
      const { session } = await orphan()
      const url = `/api/v1/ai/approvals?session=${session.id}`
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
