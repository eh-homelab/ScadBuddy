import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Principal } from '../src/auth/principal.js'
import type { Database } from '../src/db.js'
import { DurableUnavailable } from '../src/gate/durable.js'
import { CLAIM_LEASE_S, EscalationError, Escalations, type EscalationDurable } from '../src/flows/escalations.js'
import { durableRequestId } from '../src/gate/ids.js'
import { PendingProjection } from '../src/gate/projection.js'
import type { RespondArgs } from '../src/gate/durable.js'
import { pendingInput, respond, RespondError } from '../src/routes/pendingInput.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { manager, tempPaths } from './support/sessions.js'

// The starter session's answer to a child's call outside the run's grant (#2482, spec
// 2026-10-11 §4.2, §4.4, §4.6). Who the parent is, and which call it answers, come from
// the entry's own record (written from the backend's verdict), never from the call: the
// caller is the session its turn runs in, which must be the entry's parent session,
// still owned by the principal that started the run, at hop `parent`.

const RUN = '7e2a1c3b-4d5e-4f60-8a9b-0c1d2e3f4a5b'
const TOKEN = 'token:0f0e0d0c-0b0a-4908-8706-050403020100'
const OWNER: Principal = { id: TOKEN, kind: 'bearer', tiers: ['read', 'write'] }

describe.skipIf(!TEST_DATABASE_URL)(`escalations${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let sent: { sessionId: string; args: RespondArgs }[]
  let escalations: Escalations
  let parent: string
  let child: string

  const durable: EscalationDurable = {
    respond: async (sessionId, args) => {
      sent.push({ sessionId, args })
      return { kind: 'approval', outcome: 'denied' }
    },
  }

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    sent = []
    escalations = new Escalations({ sql: db.sql, durable: () => durable })
    parent = await session('bearer', TOKEN)
    child = await session('flow', `flow:${RUN}`)
  })
  afterEach(async () => {
    await drop()
  })

  async function session(kind: string, id: string): Promise<string> {
    const sid = randomUUID()
    await db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
      VALUES (${sid}, 'chat', ${kind}, ${id}, 'Owner', ${kind}, ${id}, 'waiting_approval', 10, 1)`
    return sid
  }

  async function park(options: { hop?: 'parent' | 'person'; dueInS?: number; parentSession?: string; principal?: string } = {}): Promise<string> {
    const id = durableRequestId(child, 'run-1', `toolu_${randomUUID()}`)
    const hop = options.hop ?? 'parent'
    await db.sql`
      INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary, input_hash, prompt,
                                    requested_by, responders, expires_at, flow_run_id, hop, hop_until, parent_session,
                                    parent_principal, outside_reason, hops)
      VALUES (${id}, ${child}, ${`session-${child}`}, 'run-1', 'approval', 'print_output', '{}', ${'a'.repeat(64)}, '',
              ${db.sql.json({ kind: 'flow', id: `flow:${RUN}`, label: 'Flow' })}, ${['browser']}, now() + interval '1 hour',
              ${RUN}, ${hop}, ${hop === 'parent' ? db.sql`now() + make_interval(secs => ${options.dueInS ?? 600})` : null},
              ${hop === 'parent' ? (options.parentSession ?? parent) : null}, ${hop === 'parent' ? (options.principal ?? TOKEN) : null},
              'print limit reached', ${db.sql.json([{ hop, action: 'parked', actor: 'flow', at: new Date().toISOString() }])})`
    return id
  }

  async function row(id: string) {
    const [r] = await db.sql<{ hop: string; hop_until: Date | null; hops: { hop: string; action: string; actor: string; reason?: string }[] }[]>`
      SELECT hop, hop_until, hops FROM ai_pending_input WHERE request_id = ${id}`
    return r
  }

  async function refused(call: Promise<unknown>): Promise<EscalationError> {
    const err = await call.then(
      () => undefined,
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(EscalationError)
    return err as EscalationError
  }

  it('the parent session denies a call: a parent Update, as the session, never the request', async () => {
    const id = await park()
    expect(await escalations.respond({ principal: OWNER, session: parent }, id, 'deny', { reason: 'not today' })).toEqual({
      request_id: id,
      outcome: 'denied',
    })
    expect(sent).toEqual([
      {
        sessionId: child,
        args: {
          request_id: id,
          response: { kind: 'approval', decision: 'deny' },
          responder: { kind: 'session', id: parent, label: 'Starting session' },
          role: 'parent',
        },
      },
    ])
  })

  it('a deny racing the end of the hop keeps the entry at the parent: the sweep never moves it mid-deny (#2861)', async () => {
    const id = await park({ dueInS: 600 })
    let swept = -1
    const racing: EscalationDurable = {
      respond: async (sessionId, args) => {
        // The hop ends while the Update is in flight: time moves an hour on, and the sweep runs.
        await db.sql`UPDATE ai_pending_input SET hop_until = hop_until - interval '1 hour' WHERE request_id = ${id}`
        swept = await escalations.moveDue()
        sent.push({ sessionId, args })
        return { kind: 'approval', outcome: 'denied' }
      },
    }
    escalations = new Escalations({ sql: db.sql, durable: () => racing })
    expect((await escalations.respond({ principal: OWNER, session: parent }, id, 'deny', {})).outcome).toBe('denied')
    expect(swept).toBe(0)
    expect((await row(id))?.hop).toBe('parent')
    // A pass up while the deny is in flight is refused, not a second answer.
    expect((await refused(escalations.respond({ principal: OWNER, session: parent }, id, 'pass_up', {}))).status).toBe(409)
  })

  it('a deny that does not land puts the hop back as it was (#2861)', async () => {
    const id = await park({ dueInS: 600 })
    const before = (await row(id))?.hop_until
    const down: EscalationDurable = {
      respond: async () => {
        throw new Error('temporal is down')
      },
    }
    escalations = new Escalations({ sql: db.sql, durable: () => down })
    await expect(escalations.respond({ principal: OWNER, session: parent }, id, 'deny', {})).rejects.toThrow('temporal is down')
    const after = await row(id)
    expect(after?.hop).toBe('parent')
    expect(after?.hop_until?.getTime()).toBe(before?.getTime())
    // And the parent may still answer it.
    expect(await escalations.respond({ principal: OWNER, session: parent }, id, 'pass_up', {})).toEqual({
      request_id: id,
      outcome: 'passed_up',
    })
  })

  it('matches the parent session owner by kind as well as id (#2861)', async () => {
    const id = await park()
    // The same id under another kind is not the owner.
    await db.sql`UPDATE ai_sessions SET owner_kind = 'oidc' WHERE id = ${parent}`
    expect((await refused(escalations.respond({ principal: OWNER, session: parent }, id, 'deny', {}))).status).toBe(403)
    expect(sent).toEqual([])
  })

  it("finds which entries are a run's in one query (#2863)", async () => {
    const a = await park()
    const b = await park({ hop: 'person' })
    const projection = new PendingProjection(db.sql)
    expect(await projection.inRunOf([a, b, 'durable:nope'])).toEqual(new Set([a, b]))
    expect(await projection.inRunOf([])).toEqual(new Set())
  })

  it('passing up moves the entry to the person, recorded as the session', async () => {
    const id = await park()
    expect(await escalations.respond({ principal: OWNER, session: parent }, id, 'pass_up', { reason: 'ask them' })).toEqual({
      request_id: id,
      outcome: 'passed_up',
    })
    const now = await row(id)
    expect(now?.hop).toBe('person')
    expect(now?.hop_until).toBeNull()
    expect(now?.hops.map((h) => [h.hop, h.action, h.actor, h.reason])).toEqual([
      ['parent', 'parked', 'flow', undefined],
      ['parent', 'pass_up', `session:${parent}`, 'ask them'],
    ])
    expect(sent).toEqual([])
  })

  it('refuses a caller in no session, or in any session but the parent', async () => {
    const id = await park()
    expect((await refused(escalations.respond({ principal: OWNER, session: undefined }, id, 'deny', {}))).status).toBe(403)
    const other = await session('bearer', TOKEN)
    expect((await refused(escalations.respond({ principal: OWNER, session: other }, id, 'deny', {}))).status).toBe(403)
    expect((await refused(escalations.respond({ principal: OWNER, session: other }, id, 'pass_up', {}))).status).toBe(403)
    expect((await row(id))?.hop).toBe('parent')
    expect(sent).toEqual([])
  })

  it('refuses a parent session handed to another owner since the run started', async () => {
    const id = await park()
    await db.sql`UPDATE ai_sessions SET owner_kind = 'browser', owner_id = 'browser' WHERE id = ${parent}`
    const browser: Principal = { id: 'browser', kind: 'browser', tiers: ['read', 'write', 'outward'] }
    expect((await refused(escalations.respond({ principal: browser, session: parent }, id, 'deny', {}))).status).toBe(403)
    expect(sent).toEqual([])
  })

  it('refuses a principal that is not the parent session owner', async () => {
    const id = await park()
    const stranger: Principal = { id: 'token:11111111-1111-4111-8111-111111111111', kind: 'bearer', tiers: ['read', 'write'] }
    expect((await refused(escalations.respond({ principal: stranger, session: parent }, id, 'pass_up', {}))).status).toBe(403)
  })

  it("refuses at hop person, past the hop's end, and for an entry not in a run", async () => {
    const atPerson = await park({ hop: 'person' })
    expect((await refused(escalations.respond({ principal: OWNER, session: parent }, atPerson, 'deny', {}))).status).toBe(409)
    const late = await park({ dueInS: -1 })
    expect((await refused(escalations.respond({ principal: OWNER, session: parent }, late, 'pass_up', {}))).status).toBe(409)
    expect((await refused(escalations.respond({ principal: OWNER, session: parent }, 'durable:nope', 'deny', {}))).status).toBe(404)
    expect(sent).toEqual([])
  })

  it('never narrows a child call: an agent tool call is denied or passed up', async () => {
    const id = await park()
    expect((await refused(escalations.respond({ principal: OWNER, session: parent }, id, 'narrow', {}))).status).toBe(422)
    expect((await row(id))?.hop).toBe('parent')
  })

  it("never lets an approval grant decide, or even see, a run's call: the run's queue is the person's", async () => {
    const id = await park({ hop: 'person' })
    const holder = { kind: 'bearer' as const, id: 'token:22222222-2222-4222-8222-222222222222', label: 'Another agent' }
    const m = manager({ sql: db.sql, paths: await tempPaths(), approvalGrants: async (p) => p.id === holder.id })
    const err = await respond(m, holder, id, { kind: 'approval', decision: 'approve' }).then(
      () => undefined,
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(RespondError)
    expect((err as RespondError).status).toBe(403)
    expect((await pendingInput(m, holder)).entries.map((e) => e.id)).not.toContain(id)
    // The person sees it.
    expect((await pendingInput(m)).entries.map((e) => e.id)).toContain(id)
  })

  it('moves an entry whose parent hop is over to the person, as the system, and nothing else', async () => {
    const due = await park({ dueInS: -1 })
    const waiting = await park({ dueInS: 600 })
    expect(await escalations.moveDue()).toBe(1)
    expect((await row(due))?.hops.at(-1)).toMatchObject({ hop: 'parent', action: 'timeout', actor: 'system' })
    expect((await row(due))?.hop).toBe('person')
    expect((await row(waiting))?.hop).toBe('parent')
    expect(await escalations.moveDue()).toBe(0)
  })

  it('hands the person a deny claim nothing released once its lease is over, and no sooner (review #3018)', async () => {
    const stale = await park()
    const fresh = await park()
    const claim = (id: string, agoS: number) => db.sql`
      UPDATE ai_pending_input SET hop_until = 'infinity', hop_claimed_at = now() - make_interval(secs => ${agoS})
      WHERE request_id = ${id}`
    await claim(stale, CLAIM_LEASE_S + 1)
    await claim(fresh, 1)
    expect(await escalations.moveDue()).toBe(1)
    expect((await row(stale))?.hop).toBe('person')
    expect((await row(stale))?.hops.at(-1)).toMatchObject({ action: 'timeout', actor: 'system' })
    expect((await row(fresh))?.hop).toBe('parent')
  })

  it('a pass up or a second deny on an entry a deny holds says the deny is in flight (#3518)', async () => {
    const id = await park()
    await db.sql`UPDATE ai_pending_input SET hop_until = 'infinity', hop_claimed_at = now() WHERE request_id = ${id}`
    for (const action of ['pass_up', 'deny'] as const) {
      const err = await refused(escalations.respond({ principal: OWNER, session: parent }, id, action, {}))
      expect(err.status).toBe(409)
      expect(err.message).toContain('a deny for that call is already in flight')
    }
    expect(sent).toEqual([])
  })

  it('a deny whose Update timed out keeps its claim for the lease to end, never the passed deadline (#3518)', async () => {
    const id = await park()
    const timedOut = new Escalations({
      sql: db.sql,
      durable: () => ({ respond: async () => Promise.reject(new DurableUnavailable("the session's worker is not answering")) }),
    })
    await expect(timedOut.respond({ principal: OWNER, session: parent }, id, 'deny', {})).rejects.toBeInstanceOf(DurableUnavailable)
    // It may have landed: still held, so the sweep moves it only once the lease is over.
    const [after] = await db.sql<{ claimed: boolean; hop_claimed_at: Date | null }[]>`
      SELECT hop_until = 'infinity' AS claimed, hop_claimed_at FROM ai_pending_input WHERE request_id = ${id}`
    expect(after).toMatchObject({ claimed: true })
    expect(after?.hop_claimed_at).not.toBeNull()
    expect(await escalations.moveDue()).toBe(0)
    // The parent's retry while the claim is held is told the deny is in flight (#3910).
    const retry = await refused(escalations.respond({ principal: OWNER, session: parent }, id, 'deny', {}))
    expect([retry.status, retry.message]).toEqual([409, expect.stringContaining('a deny for that call is already in flight')])
    // Once the lease is over, the sweep hands the entry to the person.
    await db.sql`
      UPDATE ai_pending_input SET hop_claimed_at = now() - make_interval(secs => ${CLAIM_LEASE_S + 1})
      WHERE request_id = ${id}`
    expect(await escalations.moveDue()).toBe(1)
    expect((await row(id))?.hop).toBe('person')
  })

  it('a deny that does not land puts the deadline back and drops its claim (review #3018)', async () => {
    const id = await park()
    const before = (await row(id))?.hop_until
    sent = []
    const failing = new Escalations({
      sql: db.sql,
      durable: () => ({ respond: async () => Promise.reject(new Error('temporal is down')) }),
    })
    await expect(failing.respond({ principal: OWNER, session: parent }, id, 'deny', {})).rejects.toThrow('temporal is down')
    const [after] = await db.sql<{ hop_until: Date; hop_claimed_at: Date | null }[]>`
      SELECT hop_until, hop_claimed_at FROM ai_pending_input WHERE request_id = ${id}`
    expect(after?.hop_until).toEqual(before)
    expect(after?.hop_claimed_at).toBeNull()
  })
})
