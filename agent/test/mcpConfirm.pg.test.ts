import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { ApprovalActions, ownerOf } from '../src/approvals/mcp.js'
import { ApprovalService } from '../src/approvals/service.js'
import { type Principal, tiersUpTo } from '../src/auth/principal.js'
import type { Database } from '../src/db.js'
import { EventLog } from '../src/sessions/eventLog.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { defineTool, json, runTool } from '../src/tools/registry.js'
import { z } from 'zod'
import { appFetch, BACKEND, connect, firstText, INGRESS, services, testApp } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser } from './support/sessions.js'

// MCP prepare/confirm on ai_approvals (spec §8.2, #258): an outward tool
// called over /mcp records a pending approval; the UI decides it through the
// approval routes; confirm_action runs the call once, for the same principal
// and the same input, and only once approved.

const OUTPUT = '0123456789abcdef0123456789abcdef'
const SEND = { output_id: OUTPUT }

const principalA: Principal = { id: 'token:a', kind: 'bearer', tiers: tiersUpTo('outward') }
const principalB: Principal = { id: 'token:b', kind: 'bearer', tiers: tiersUpTo('outward') }

describe('ownerOf', () => {
  it("never stores an anonymous caller's MCP session id, only a digest of it", () => {
    const owner = ownerOf({ id: 'anonymous:secret-session-id', kind: 'anonymous', tiers: ['read'], clientIp: '10.1.2.3' })
    expect(owner.id).toMatch(/^anonymous:[0-9a-f]{32}$/)
    expect(JSON.stringify(owner)).not.toContain('secret-session-id')
    expect(owner.label).toContain('10.1.2.3')
    expect(ownerOf({ id: 'anonymous:other', kind: 'anonymous', tiers: ['read'] }).id).not.toBe(owner.id)
    expect(ownerOf(principalA)).toMatchObject({ kind: 'bearer', id: 'token:a' })
  })

  it('records an OIDC caller as itself, by subject and client, never as the browser user', () => {
    const owner = ownerOf({
      id: 'oidc:https://idp.example/#alice',
      kind: 'oidc',
      tiers: tiersUpTo('outward'),
      subject: 'alice',
      clientId: 'claude-code',
    })
    expect(owner).toEqual({ kind: 'oidc', id: 'oidc:https://idp.example/#alice', label: 'MCP OIDC alice via claude-code' })
    expect(ownerOf({ id: 'oidc:https://idp.example/#bob', kind: 'oidc', tiers: ['read'], subject: 'bob' }).label).toBe(
      'MCP OIDC bob',
    )
  })

  it('refuses a principal kind it does not know rather than calling it the browser user', () => {
    expect(() => ownerOf({ id: 'x', kind: 'future' as never, tiers: ['read'] })).toThrow(/unknown principal kind/)
  })
})

describe.skipIf(!TEST_DATABASE_URL)(
  `MCP prepare/confirm in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    let approvals: ApprovalService
    let actions: ApprovalActions
    let grants: Set<string>

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      grants = new Set()
      approvals = new ApprovalService({
        sql: db.sql,
        events: new EventLog(db.sql),
        hashKey: Buffer.alloc(32, 3),
        grants: async (p) => grants.has(p.id),
      })
      actions = new ApprovalActions(approvals, { perPrincipal: 3, total: 5 })
    })
    afterEach(async () => {
      await drop()
    })

    const prepare = (p: Principal, input: Record<string, unknown> = SEND) =>
      actions.prepare(p, { tool: 'send_to_bambuddy', input, summary: 'Send it' })

    it('records a sessionless pending approval: principal, tool, HMAC input hash and scrubbed summary', async () => {
      const action = await prepare(principalA, { output_id: OUTPUT, api_key: 'k-1' })
      expect(action).toMatchObject({ tool: 'send_to_bambuddy', summary: 'Send it' })
      const row = await approvals.get(action.id, browser)
      expect(row).toMatchObject({
        sessionId: null,
        turnId: null,
        tool: 'send_to_bambuddy',
        tier: 'outward',
        decision: null,
        requestedBy: { kind: 'bearer', id: 'token:a' },
        inputHash: approvals.hash('send_to_bambuddy', { output_id: OUTPUT, api_key: 'k-1' }),
      })
      expect(row.inputSummary).not.toContain('k-1')
      // The UI's list of pending approvals includes it.
      expect((await approvals.list(browser, { pending: true })).map((a) => a.id)).toEqual([action.id])
      expect((await actions.list(principalA)).map((a) => a.id)).toEqual([action.id])
      expect(await actions.list(principalB)).toEqual([])
    })

    it('confirm before approval is pending; after approval it is claimed exactly once', async () => {
      const action = await prepare(principalA)
      expect(await actions.claim(action.id, principalA, SEND)).toMatchObject({ status: 'pending' })
      await approvals.decide(browser, action.id, true)
      expect(await actions.claim(action.id, principalA, SEND)).toMatchObject({ status: 'approved' })
      const replay = await actions.claim(action.id, principalA, SEND)
      expect(replay).toMatchObject({ status: 'refused' })
      expect(replay.status === 'refused' && replay.reason).toContain('already confirmed')
      expect((await approvals.get(action.id, browser)).consumedAt).not.toBeNull()
    })

    it('two confirms racing: only one wins', async () => {
      const action = await prepare(principalA)
      await approvals.decide(browser, action.id, true)
      const claims = await Promise.all(Array.from({ length: 5 }, () => actions.claim(action.id, principalA, SEND)))
      expect(claims.filter((c) => c.status === 'approved')).toHaveLength(1)
    })

    it("refuses another principal's confirm, and does not use the approval up", async () => {
      const action = await prepare(principalA)
      await approvals.decide(browser, action.id, true)
      expect(await actions.find(action.id, principalB)).toBeUndefined()
      expect(await actions.claim(action.id, principalB, SEND)).toMatchObject({ status: 'refused' })
      // Even straight at the store: B is not the requester.
      expect(await approvals.consumePrepared(action.id, ownerOf(principalB), approvals.hash('send_to_bambuddy', SEND))).toBeUndefined()
      expect(await actions.claim(action.id, principalA, SEND)).toMatchObject({ status: 'approved' })
    })

    it('refuses a changed input, and leaves the approval for the approved one', async () => {
      const action = await prepare(principalA)
      await approvals.decide(browser, action.id, true)
      const changed = await actions.claim(action.id, principalA, { output_id: 'ffffffffffffffffffffffffffffffff' })
      expect(changed).toMatchObject({ status: 'refused' })
      expect(changed.status === 'refused' && changed.reason).toContain('not the ones')
      expect(await actions.claim(action.id, principalA, SEND)).toMatchObject({ status: 'approved' })
    })

    it('refuses denied, expired and out-of-time approvals', async () => {
      const denied = await prepare(principalA)
      await approvals.decide(browser, denied.id, false)
      expect(await actions.claim(denied.id, principalA, SEND)).toMatchObject({ status: 'refused', reason: expect.stringContaining('denied') })

      const expired = await prepare(principalA)
      await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = ${expired.id}`
      expect(await actions.claim(expired.id, principalA, SEND)).toMatchObject({ status: 'refused', reason: expect.stringContaining('expired') })
      expect((await approvals.get(expired.id, browser)).decision).toBe('expired')

      const stale = await prepare(principalA)
      await approvals.decide(browser, stale.id, true)
      await db.sql`UPDATE ai_approvals SET usable_until = now() - interval '1 second' WHERE id = ${stale.id}`
      expect(await actions.claim(stale.id, principalA, SEND)).toMatchObject({ status: 'refused', reason: expect.stringContaining('no longer usable') })
    })

    it('refuses self-approval, even with an approval grant; another grant holder may decide', async () => {
      const action = await prepare(principalA)
      await expect(approvals.decide(ownerOf(principalA), action.id, true)).rejects.toMatchObject({ code: 'forbidden' })
      grants.add('token:a')
      grants.add('token:b')
      await expect(approvals.decide(ownerOf(principalA), action.id, true)).rejects.toMatchObject({ code: 'forbidden' })
      expect(await actions.claim(action.id, principalA, SEND)).toMatchObject({ status: 'pending' })
      await approvals.decide(ownerOf(principalB), action.id, true)
      expect(await actions.claim(action.id, principalA, SEND)).toMatchObject({ status: 'approved' })
    })

    it("bounds the queue: a full caller loses its own oldest, a full table refuses; nobody else's is touched", async () => {
      const [b1] = [await prepare(principalB)]
      const a = []
      for (let i = 0; i < 4; i++) a.push(await prepare(principalA, { output_id: `${i}`.padStart(32, '0') }))
      expect((await actions.list(principalA)).map((x) => x.id)).toEqual(a.slice(-3).map((x) => x.id))
      expect((await approvals.get(a[0]!.id, browser)).decision).toBe('cancelled')
      expect((await actions.list(principalB)).map((x) => x.id)).toEqual([b1!.id])
      const principalC: Principal = { id: 'token:c', kind: 'bearer', tiers: tiersUpTo('outward') }
      await prepare(principalC)
      await expect(prepare({ ...principalC, id: 'token:d' })).rejects.toThrow('too many actions')
    })

    it('an evicted action leaves no pending approval behind, and an action outlives a restart', async () => {
      const first = await prepare(principalA, { output_id: '0'.repeat(32) })
      const kept = await prepare(principalA, { output_id: '1'.repeat(32) })
      for (let i = 2; i < 4; i++) await prepare(principalA, { output_id: `${i}`.repeat(32) })
      // The UI no longer offers the evicted one, and it cannot be decided or confirmed.
      expect((await approvals.list(browser, { pending: true })).map((a) => a.id)).not.toContain(first.id)
      await expect(approvals.decide(browser, first.id, true)).rejects.toBeDefined()
      const evicted = await actions.claim(first.id, principalA, { output_id: '0'.repeat(32) })
      expect(evicted.status === 'refused' && evicted.reason).toContain('cancelled')
      // Nothing is held in memory: a new store on the same table (a restarted
      // process) confirms what the old one prepared.
      await approvals.decide(browser, kept.id, true)
      const restarted = new ApprovalActions(approvals, { perPrincipal: 3, total: 5 })
      expect(await restarted.claim(kept.id, principalA, { output_id: '1'.repeat(32) })).toMatchObject({
        status: 'approved',
      })
    })

    it('holds both bounds under concurrent prepares (one transaction under an advisory lock)', async () => {
      const pendingOf = async (id?: string) => {
        const [row] = await db.sql<{ n: number }[]>`
          SELECT count(*)::int AS n FROM ai_approvals
          WHERE session_id IS NULL AND decision IS NULL AND (${id ?? null}::text IS NULL OR requested_by_id = ${id ?? null})`
        return row!.n
      }
      // One caller bursting: never more than its 3 pending, the rest cancelled.
      const burst = await Promise.allSettled(
        Array.from({ length: 20 }, (_, i) => prepare(principalA, { output_id: `${i}`.padStart(32, '0') })),
      )
      expect(burst.every((r) => r.status === 'fulfilled')).toBe(true)
      expect(await pendingOf('token:a')).toBe(3)

      // Many callers bursting at the global bound (5): exactly 2 more fit.
      const others = await Promise.allSettled(
        Array.from({ length: 12 }, (_, i) => prepare({ ...principalB, id: `token:x${i}` })),
      )
      expect(others.filter((r) => r.status === 'fulfilled')).toHaveLength(2)
      expect(others.filter((r) => r.status === 'rejected').every((r) => String(r.reason).includes('too many actions'))).toBe(true)
      expect(await pendingOf()).toBe(5)
    })

    it('confirm parses with the tool\'s own schema: a top-level strict() or refine() is not dropped', async () => {
      const ran: unknown[] = []
      const guarded = defineTool({
        name: 'guarded_send',
        description: 'test: an outward tool with a strict, refined top-level input',
        input: z
          .object({ from: z.number(), to: z.number() })
          .strict()
          .refine((v) => v.from < v.to, { message: 'from must be below to' }),
        risk: 'outward',
        routes: [],
        handler: async (args) => {
          ran.push(args)
          return json({ ok: true })
        },
      })
      const confirmTool = ALL_TOOLS.find((t) => t.name === 'confirm_action')!
      const byName = new Map([guarded, confirmTool].map((t) => [t.name, t]))
      const ctx = {
        ...services({ pending: actions }),
        principal: principalA,
        progress: async () => {},
        signal: new AbortController().signal,
        lookup: (name: string) => byName.get(name),
      }
      const prepared = firstText(await runTool(guarded, { from: 1, to: 2 }, ctx)) as { pending_action_id: string }
      await approvals.decide(browser, prepared.pending_action_id, true)
      const confirm = (args: Record<string, unknown>) =>
        runTool(confirmTool, { pending_action_id: prepared.pending_action_id, arguments: args }, ctx)

      // An extra key: z.object(tool.shape) would have stripped it and run the call.
      const extra = await confirm({ from: 1, to: 2, sneaky: true })
      expect(extra.isError).toBe(true)
      expect(firstText(extra)).toContain('not valid for guarded_send')
      // The refinement holds too.
      const refined = await confirm({ from: 3, to: 2 })
      expect(refined.isError).toBe(true)
      expect(firstText(refined)).toContain('from must be below to')
      expect(ran).toEqual([])
      // The approved input still runs, once.
      expect((await confirm({ from: 1, to: 2 })).isError).toBeFalsy()
      expect(ran).toEqual([{ from: 1, to: 2 }])
    })
  },
)

// ── end to end: the real MCP SDK client against /mcp, the UI's approval routes ──

const backend = setupServer()
beforeAll(() => backend.listen({ onUnhandledRequest: 'error' }))
afterEach(() => backend.resetHandlers())
afterAll(() => backend.close())

/** The UI through the TLS ingress, as routes/guard.ts wants it (helpers/mcp.ts ORIGINS). */
const UI_VIA = {
  address: INGRESS,
  headers: { origin: 'https://scadbuddy.test', 'x-forwarded-proto': 'https', 'content-type': 'application/json' },
}

describe.skipIf(!TEST_DATABASE_URL)(
  `MCP confirm_action over /mcp${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    let sent: unknown[]
    const clients: Client[] = []

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      sent = []
      backend.use(
        http.post(`${BACKEND}/api/v1/outputs/:id/send`, async ({ request, params }) => {
          sent.push({ id: params.id, body: await request.json() })
          return HttpResponse.json({ status: 'sent', filename: 'x.3mf' })
        }),
      )
    })
    afterEach(async () => {
      await Promise.all(clients.splice(0).map((c) => c.close()))
      await drop()
    })

    async function setup() {
      const approvals = new ApprovalService({ sql: db.sql, events: new EventLog(db.sql), hashKey: Buffer.alloc(32, 4) })
      const t = testApp({
        services: services({ pending: new ApprovalActions(approvals) }),
        deps: { approvals, database: { ping: async () => true, ready: () => db.ready() } },
      })
      const mint = async (name: string) => {
        const { token } = await t.tokens.mint({ name, tier: 'outward' })
        const client = await connect(t.app, { headers: { authorization: `Bearer ${token}` } })
        clients.push(client)
        return client
      }
      const ui = appFetch(t.app, UI_VIA)
      const decide = (id: string, verb: 'approve' | 'deny') =>
        ui(`https://scadbuddy.test/api/v1/ai/approvals/${id}/${verb}`, { method: 'POST', body: '{}' })
      type Listed = { id: string; tool: string; session_id: string | null; requested_by: { kind: string; id: string } }
      const pendingInUi = async () => {
        const res = await ui('https://scadbuddy.test/api/v1/ai/approvals', { headers: { origin: 'https://scadbuddy.test' } })
        return ((await res.json()) as { approvals: Listed[] }).approvals
      }
      return { t, mint, decide, pendingInUi }
    }

    const confirm = (client: Client, id: string, args: Record<string, unknown> = SEND) =>
      client.callTool({ name: 'confirm_action', arguments: { pending_action_id: id, arguments: args } })

    it('prepare → pending; the UI approves; confirm runs it once; replay, another caller and a changed input are refused', async () => {
      const { mint, decide, pendingInUi } = await setup()
      const a = await mint('a')
      const b = await mint('b')

      const prepared = firstText(await a.callTool({ name: 'send_to_bambuddy', arguments: SEND })) as {
        status: string
        pending_action_id: string
        summary: string
      }
      expect(prepared).toMatchObject({ status: 'pending_approval', summary: `Send output ${OUTPUT} to Bambuddy's library` })
      const id = prepared.pending_action_id
      expect(sent).toEqual([])

      // The UI sees it, as a sessionless approval requested by A's token.
      const listed = await pendingInUi()
      expect(listed).toEqual([expect.objectContaining({ id, tool: 'send_to_bambuddy', session_id: null })])
      expect(listed[0]!.requested_by.kind).toBe('bearer')

      // Confirm before approval: pending, nothing sent.
      const early = await confirm(a, id)
      expect(early.isError).toBeFalsy()
      expect(firstText(early)).toMatchObject({ status: 'pending_approval', pending_action_id: id })
      expect(sent).toEqual([])

      // The human approves in the UI.
      expect((await decide(id, 'approve')).status).toBe(200)

      // Another caller cannot confirm it.
      const cross = await confirm(b, id)
      expect(cross.isError).toBe(true)
      expect(firstText(cross)).toContain('no pending action')

      // A changed input is refused and does not use the approval up.
      const changed = await confirm(a, id, { output_id: 'ffffffffffffffffffffffffffffffff' })
      expect(changed.isError).toBe(true)
      expect(firstText(changed)).toContain('not the ones')
      expect(sent).toEqual([])

      // Missing arguments are refused too.
      const bare = await a.callTool({ name: 'confirm_action', arguments: { pending_action_id: id } })
      expect(bare.isError).toBe(true)
      expect(sent).toEqual([])

      // The approved call runs, once.
      const ran = await confirm(a, id)
      expect(ran.isError).toBeFalsy()
      expect(firstText(ran)).toMatchObject({ status: 'sent' })
      expect(sent).toEqual([{ id: OUTPUT, body: { mode: 'library', copies: null, options: {} } }])

      // Replay: refused, nothing more sent.
      const replay = await confirm(a, id)
      expect(replay.isError).toBe(true)
      expect(firstText(replay)).toContain('already confirmed')
      expect(sent).toHaveLength(1)
      expect(await pendingInUi()).toEqual([])
    })

    it('a denied action is refused, and nothing is sent', async () => {
      const { mint, decide } = await setup()
      const a = await mint('a')
      const { pending_action_id: id } = firstText(await a.callTool({ name: 'send_to_bambuddy', arguments: SEND })) as {
        pending_action_id: string
      }
      expect((await decide(id, 'deny')).status).toBe(200)
      const res = await confirm(a, id)
      expect(res.isError).toBe(true)
      expect(firstText(res)).toContain('denied')
      expect(sent).toEqual([])
    })
  },
)
