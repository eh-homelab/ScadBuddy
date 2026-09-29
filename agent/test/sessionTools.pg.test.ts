import { ResourceUpdatedNotificationSchema } from '@modelcontextprotocol/sdk/types.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ApprovalActions, ownerOf } from '../src/approvals/mcp.js'
import type { Tier } from '../src/auth/principal.js'
import { approvalGrantCheck, principalFor } from '../src/auth/tokens.js'
import { connectDatabase, type Database } from '../src/db.js'
import { PgEventListener } from '../src/events/pgListener.js'
import { ResourceHub } from '../src/resources/hub.js'
import { BROWSER_USER } from '../src/routes/approvals.js'
import { followSessionEvents, SessionEventPublisher } from '../src/sessions/busEvents.js'
import type { SessionManager, TurnPrincipal } from '../src/sessions/manager.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool } from '../src/tools/registry.js'
import { connect, connectWatching, firstText, services, testApp } from './helpers/mcp.js'
import { InMemoryTokenStore } from './support/memoryTokens.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { collectUntil, type FakeTurn, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import type { HarnessRun } from '../src/harness/run.js'

// The `sessions_*` tools (#300) end to end: real /mcp clients with bearer
// tokens, a real SessionManager on Postgres (with a scripted stand-in for the
// SDK, test/support/sessions.ts), the `session.*` publisher and a real LISTEN
// on `scadbuddy_events`. The issue's tests: start from MCP → watch → hand off
// → continue in the browser; concurrent sends are rejected; an approval from
// an agent without the grant is refused; session events reach MCP
// subscribers; and a watcher on another replica is woken by the bus. And the
// PR #715 review's: a handoff to another agent is an offer only it accepts,
// and no caller is shown another principal's id.

type Result = { isError?: boolean; content: { type: string; text?: string }[] }

function errorText(result: Result): string {
  expect(result.isError, JSON.stringify(result)).toBe(true)
  return result.content.map((c) => c.text ?? '').join('\n')
}

function ok<T = Record<string, unknown>>(result: Result): T {
  expect(result.isError ?? false, JSON.stringify(result)).toBe(false)
  return firstText(result) as T
}

async function until(check: () => boolean | Promise<boolean>, what: string, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!(await check())) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`)
    await new Promise((r) => setTimeout(r, 10))
  }
}

describe.skipIf(!TEST_DATABASE_URL)(
  `sessions_* tools over /mcp${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let schema: string
    let drop: () => Promise<void>
    const closers: (() => Promise<void>)[] = []

    beforeEach(async () => {
      ;({ db, schema, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
    })
    afterEach(async () => {
      for (const close of closers.splice(0).reverse()) await close().catch(() => {})
      await drop()
    })

    async function listener(): Promise<PgEventListener> {
      const l = new PgEventListener(TEST_DATABASE_URL!, { searchPath: schema, retryMinMs: 20, log: () => {} })
      l.start()
      closers.push(() => l.close())
      await l.ready()
      return l
    }

    async function setup(turns: (run: HarnessRun) => FakeTurn = () => ({ reply: 'hello from the agent' })) {
      const tokens = new InMemoryTokenStore()
      const publisher = new SessionEventPublisher(db.sql, { throttleMs: 10 })
      const { runner, runs } = scriptedRunner(turns)
      const principals: TurnPrincipal[] = []
      const sessions = manager({
        sql: db.sql,
        paths: await tempPaths(),
        run: runner,
        onAppend: publisher.onAppend,
        approvalGrants: approvalGrantCheck(tokens),
        mcpServers: (_session, turn) => {
          principals.push(turn)
          return {}
        },
      })
      const bus = await listener()
      const hub = new ResourceHub(bus, { minIntervalMs: 0 })
      closers.push(async () => {
        sessions.abortAll()
        hub.close()
        publisher.close()
      })
      const svc = services({ sessions, pending: new ApprovalActions(sessions.approvals) })
      const t = testApp({ tokens, services: svc, mcp: { resources: hub } })
      async function agent(tier: Tier, options: { approvalGrant?: boolean } = {}) {
        const { token, record } = await tokens.mint({ name: `agent ${tier}`, tier, ...options })
        const client = await connect(t.app, { headers: { authorization: `Bearer ${token}` } })
        closers.push(() => client.close())
        const call = (name: string, args: Record<string, unknown> = {}) =>
          client.callTool({ name, arguments: args }) as Promise<Result>
        return { client, call, token, owner: ownerOf(principalFor(record.id, tier)) }
      }
      return { ...t, sessions, runs, principals, publisher, bus, hub, agent }
    }

    it('starts a session from MCP, watches it, hands it to the browser user, who continues it', async () => {
      const { agent, sessions, principals } = await setup()
      const a = await agent('write')
      const started = ok<{ session: { id: string; origin: string; owner: { kind: string }; status: string }; turn: { finished: boolean } }>(
        await a.call('sessions_start', { prompt: 'make a box', title: 'Box', wait_seconds: 10 }),
      )
      expect(started.session).toMatchObject({ origin: 'mcp', owner: { kind: 'bearer' }, status: 'idle' })
      expect(started.turn).toMatchObject({ finished: true, result: 'success' })
      // The turn's in-process tools ran with the sending token's tiers.
      expect(principals).toEqual([{ tiers: ['read', 'write'] }])
      const id = started.session.id

      const got = ok<{ transcript: Record<string, unknown>[]; next_seq: number; more: boolean }>(
        await a.call('sessions_get', { session_id: id }),
      )
      expect(got.transcript).toContainEqual(expect.objectContaining({ type: 'user.turn', text: 'make a box' }))
      expect(got.transcript).toContainEqual(
        expect.objectContaining({ type: 'assistant.text', text: 'hello from the agent', done: true }),
      )
      expect(got.more).toBe(false)
      expect(ok<unknown[]>(await a.call('sessions_list'))).toHaveLength(1)

      // Hand off to the human in the browser.
      const handed = ok<{ owner: { kind: string } }>(await a.call('sessions_handoff', { session_id: id, to: 'browser' }))
      expect(handed.owner).toMatchObject({ kind: 'browser' })
      // It can still watch (it started it), but no longer send.
      expect(errorText(await a.call('sessions_send', { session_id: id, text: 'more' }))).toMatch(/controlled by You/)
      // The browser user continues it (the panel and routes call the manager as the browser user).
      const turn = await sessions.send(id, BROWSER_USER, 'now with a lid')
      await turn.done
      const after = ok<{ transcript: Record<string, unknown>[] }>(
        await a.call('sessions_get', { session_id: id, after_seq: got.next_seq }),
      )
      expect(after.transcript).toContainEqual(
        expect.objectContaining({ type: 'user.turn', text: 'now with a lid', author: expect.objectContaining({ kind: 'browser' }) }),
      )
      // Browser-owned turns use the browser's own tiers (no override).
      expect(principals.at(-1)).toEqual({})
    })

    it("shows a caller only the sessions it may see; another agent's are 'no session'", async () => {
      const { agent } = await setup()
      const a = await agent('write')
      const b = await agent('outward')
      const { session } = ok<{ session: { id: string } }>(await a.call('sessions_start', { title: 'mine' }))
      expect(ok<unknown[]>(await b.call('sessions_list'))).toEqual([])
      for (const [name, args] of [
        ['sessions_get', { session_id: session.id }],
        ['sessions_attach', { session_id: session.id, wait_seconds: 1 }],
        ['sessions_send', { session_id: session.id, text: 'hi' }],
        ['sessions_interrupt', { session_id: session.id }],
        ['sessions_handoff', { session_id: session.id, to: 'browser' }],
        ['sessions_fork', { session_id: session.id }],
      ] as const) {
        expect(errorText(await b.call(name, args)), name).toMatch(new RegExp(`no session ${session.id}`))
      }
      // A read token may watch but not start.
      const r = await agent('read')
      expect(errorText(await r.call('sessions_start', { title: 'x' }))).toMatch(/needs the "write" tier/)
    })

    it('refuses a send while a turn runs; any watcher may interrupt it', async () => {
      let n = 0
      const { agent } = await setup(() => (n++ === 0 ? { hang: true } : { reply: 'second' }))
      const a = await agent('write')
      const { session } = ok<{ session: { id: string; status: string } }>(
        await a.call('sessions_start', { prompt: 'take your time' }),
      )
      expect(session.status).toBe('running')
      expect(errorText(await a.call('sessions_send', { session_id: session.id, text: 'hurry' }))).toMatch(
        /a turn is already running/,
      )
      expect(ok(await a.call('sessions_interrupt', { session_id: session.id }))).toEqual({ interrupted: true })
      await until(async () => ok<{ session: { status: string; running: boolean } }>(
        await a.call('sessions_get', { session_id: session.id }),
      ).session.running === false, 'the interrupted turn to end')
      const sent = ok<{ turn: { finished: boolean } }>(
        await a.call('sessions_send', { session_id: session.id, text: 'again', wait_seconds: 10 }),
      )
      expect(sent.turn).toMatchObject({ finished: true, result: 'success' })
    })

    it('forks a session into one the caller owns, with the conversation so far', async () => {
      const { agent, sessions } = await setup()
      const a = await agent('write')
      const { session } = ok<{ session: { id: string } }>(
        await a.call('sessions_start', { prompt: 'first', wait_seconds: 10 }),
      )
      // The scripted runner writes no SDK transcript; the fork needs one.
      await sessions.store.append({ projectKey: 'p', sessionId: session.id }, [{ type: 'user', uuid: 'u1', message: {} }])
      const child = ok<{ id: string; parent_id: string; owner: { kind: string } }>(
        await a.call('sessions_fork', { session_id: session.id, title: 'alt' }),
      )
      expect(child).toMatchObject({ parent_id: session.id, owner: { kind: 'bearer' } })
      const got = ok<{ transcript: Record<string, unknown>[] }>(await a.call('sessions_get', { session_id: child.id }))
      expect(got.transcript).toContainEqual(expect.objectContaining({ type: 'user.turn', text: 'first' }))
    })

    it('lets only a token with the approval grant decide, and never its own', async () => {
      const { agent, sessions, publisher } = await setup()
      const a = await agent('write')
      const { session } = ok<{ session: { id: string } }>(await a.call('sessions_start', { title: 'needs approval' }))
      const approval = await sessions.approvals.create({
        sessionId: session.id,
        turnId: null,
        toolUseId: 'tu-1',
        tool: 'send_to_bambuddy',
        input: { output_id: 'o-1' },
        tier: 'outward',
        requestedBy: a.owner,
      })

      // Below outward: refused by tier before anything else.
      expect(errorText(await a.call('sessions_approve', { approval_id: approval.id }))).toMatch(/needs the "outward" tier/)
      // Outward without the grant: it may not even see another agent's approval.
      const plain = await agent('outward')
      expect(errorText(await plain.call('sessions_approve', { approval_id: approval.id }))).toMatch(/no approval/)
      expect(errorText(await plain.call('sessions_list_approvals'))).toMatch(/name a session/)

      // With the grant: listed, and decided, with the hash it was shown.
      const reviewer = await agent('outward', { approvalGrant: true })
      const pending = ok<{ id: string; input_hash: string }[]>(await reviewer.call('sessions_list_approvals'))
      expect(pending.map((p) => p.id)).toEqual([approval.id])
      expect(
        errorText(await reviewer.call('sessions_deny', { approval_id: approval.id, input_hash: '0'.repeat(64) })),
      ).toMatch(/different input/)
      const sentBefore = publisher.sent
      const decided = ok<{ decision: string; decided_by: { kind: string } }>(
        await reviewer.call('sessions_deny', { approval_id: approval.id, input_hash: pending[0]!.input_hash }),
      )
      expect(decided).toMatchObject({ decision: 'denied', decided_by: { kind: 'bearer' } })
      // The decision's approval.resolved, appended inside its transaction, is announced once it commits.
      await until(() => publisher.sent > sentBefore, 'the decision on the bus')

      // A grant never covers the holder's own sessions.
      const own = ok<{ session: { id: string } }>(await reviewer.call('sessions_start', { title: 'mine' }))
      const mine = await sessions.approvals.create({
        sessionId: own.session.id,
        turnId: null,
        toolUseId: 'tu-2',
        tool: 'send_to_bambuddy',
        input: { output_id: 'o-2' },
        tier: 'outward',
        requestedBy: reviewer.owner,
      })
      expect(errorText(await reviewer.call('sessions_approve', { approval_id: mine.id }))).toMatch(/its own outward actions/)
      expect((await sessions.approvals.get(mine.id, BROWSER_USER)).decision).toBeNull()
    })

    it('delivers session events to an MCP subscriber, and refuses a subscription to another agent', async () => {
      const { app, tokens, agent } = await setup()
      const a = await agent('write')
      // Another token, which may not see `a`'s session; `a` watches its own
      // over a second client whose GET stream the test can wait on.
      const { token } = await tokens.mint({ name: 'watcher', tier: 'write' })
      const aWatch = await connectWatching(app, { headers: { authorization: `Bearer ${a.token}` } })
      closers.push(() => aWatch.client.close())
      const other = await connect(app, { headers: { authorization: `Bearer ${token}` } })
      closers.push(() => other.close())
      const updated: string[] = []
      aWatch.client.setNotificationHandler(ResourceUpdatedNotificationSchema, (n) => {
        updated.push(n.params.uri)
      })

      const { session } = ok<{ session: { id: string } }>(await a.call('sessions_start', { title: 'watched' }))
      const uri = `scadbuddy://sessions/${session.id}`
      await expect(other.subscribeResource({ uri })).rejects.toMatchObject({ code: -32002 })
      await expect(other.readResource({ uri })).rejects.toThrow(/no session/)
      await aWatch.client.subscribeResource({ uri })
      await aWatch.client.subscribeResource({ uri: 'scadbuddy://sessions' })
      await aWatch.streamOpen()

      await a.call('sessions_send', { session_id: session.id, text: 'go', wait_seconds: 10 })
      await until(() => updated.includes(uri) && updated.includes('scadbuddy://sessions'), 'resources/updated')
      const { contents } = await aWatch.client.readResource({ uri })
      expect(JSON.stringify(contents)).toContain('hello from the agent')
    })

    it('wakes a watcher on another replica through the bus, not the poll', async () => {
      const paths = await tempPaths()
      const slow = 60_000
      const pubA = new SessionEventPublisher(db.sql, { throttleMs: 10 })
      const { runner } = scriptedRunner(() => ({ reply: 'from replica A' }))
      const replicaA = manager({ sql: db.sql, paths, run: runner, pollMs: slow, onAppend: pubA.onAppend })
      const other = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
      const pubB = new SessionEventPublisher(other.sql, { throttleMs: 10 })
      const replicaB: SessionManager = manager({ sql: other.sql, paths, pollMs: slow, onAppend: pubB.onAppend })
      const bus = await listener()
      const stopWake = followSessionEvents(bus, replicaB.events, pubB.replica)
      closers.push(async () => {
        stopWake()
        pubA.close()
        pubB.close()
        replicaA.abortAll()
        await other.close()
      })

      const { session } = await replicaA.start(BROWSER_USER, { origin: 'chat', title: 'across replicas' })
      const stop = new AbortController()
      closers.push(async () => stop.abort())
      const from = await replicaB.events.lastSeq(session.id)
      const follow = await replicaB.attach(session.id, BROWSER_USER, { afterSeq: from, signal: stop.signal })
      const started = Date.now()
      const reading = collectUntil(follow, (e) => e.event.type === 'session.status' && e.event.status === 'idle', 5000)
      // Give the follower its first (empty) read, so it is waiting when the turn runs.
      await new Promise((r) => setTimeout(r, 100))
      await (await replicaA.send(session.id, BROWSER_USER, 'hello')).done
      const events = await reading
      expect(events.map((e) => e.event.type)).toContain('assistant.text.delta')
      // Far inside the 60 s poll: the NOTIFY woke it.
      expect(Date.now() - started).toBeLessThan(5000)
      expect(pubA.sent).toBeGreaterThan(0)
    })

    it('tells followers when LISTEN comes back, so NOTIFY-only session events are re-read', async () => {
      const bus = await listener()
      let reconnects = 0
      const stop = bus.follow({ onEvent: () => {}, onResync: () => {}, onReconnect: () => void reconnects++ })
      closers.push(async () => stop())
      await bus.dropConnectionForTest()
      await until(() => reconnects === 1, 'the reconnect')
    })

    it('offers a session to another agent, which alone may accept it; A cannot force it or see B\'s id after', async () => {
      const { agent, bus } = await setup()
      const a = await agent('write')
      const b = await agent('write')
      const c = await agent('write')
      const ownerEvents: string[] = []
      const stop = bus.follow({
        onEvent: (e) => {
          if (e.kind === 'session.owner' && typeof e.session_id === 'string') ownerEvents.push(e.session_id)
        },
        onResync: () => {},
        onReconnect: () => {},
      })
      closers.push(async () => stop())
      const { session } = ok<{ session: { id: string } }>(await a.call('sessions_start', { prompt: 'hi', wait_seconds: 10 }))
      const id = session.id

      // A offers it to B: nothing moves yet, and the offer is announced.
      const offered = ok<{ owner: { kind: string; id?: string }; offer: { to: { id?: string }; until: string } }>(
        await a.call('sessions_handoff', { session_id: id, to: b.owner.id }),
      )
      expect(offered.owner).toEqual(a.owner)
      expect(Date.parse(offered.offer.until)).toBeGreaterThan(Date.now())
      // A named B, but is shown only B's kind.
      expect(offered.offer.to).toEqual({ kind: 'bearer', label: 'another MCP token' })
      await until(() => ownerEvents.includes(id), 'the offer on the bus')

      // B sees it, flagged, and may read it before deciding; it may not send yet.
      const listed = ok<{ id: string; offered_to_you: boolean; offer: { to: unknown }; owner: { id?: string } }[]>(
        await b.call('sessions_list'),
      )
      expect(listed).toHaveLength(1)
      expect(listed[0]).toMatchObject({ id, offered_to_you: true, offer: { to: b.owner } })
      // B is shown A by kind only.
      expect(listed[0]!.owner).toEqual({ kind: 'bearer', label: 'another MCP token' })
      ok(await b.call('sessions_get', { session_id: id }))
      expect(errorText(await b.call('sessions_send', { session_id: id, text: 'mine?' }))).toMatch(
        /controlled by another MCP token/,
      )
      expect(ok<{ offered_to_you: boolean }[]>(await a.call('sessions_list'))[0]!.offered_to_you).toBe(false)

      // A cannot accept on B's behalf, and a third agent cannot see it at all.
      expect(errorText(await a.call('sessions_accept_handoff', { session_id: id }))).toMatch(/not offered to you/)
      for (const [name, args] of [
        ['sessions_accept_handoff', { session_id: id }],
        ['sessions_handoff', { session_id: id, to: c.owner.id }],
        ['sessions_cancel_handoff', { session_id: id }],
      ] as const) {
        expect(errorText(await c.call(name, args)), name).toMatch(new RegExp(`no session ${id}`))
      }
      expect(ok<unknown[]>(await c.call('sessions_list'))).toEqual([])

      // B accepts: it owns the session now, and alone may send.
      ownerEvents.length = 0
      const accepted = ok<{ owner: unknown; offer: unknown; offered_to_you: boolean }>(
        await b.call('sessions_accept_handoff', { session_id: id }),
      )
      expect(accepted).toMatchObject({ owner: b.owner, offer: null, offered_to_you: false })
      await until(() => ownerEvents.includes(id), 'the new owner on the bus')
      ok(await b.call('sessions_send', { session_id: id, text: 'b here', wait_seconds: 10 }))
      expect(errorText(await a.call('sessions_send', { session_id: id, text: 'x' }))).toMatch(/controlled by another MCP token/)

      // A still sees the session it started, but nowhere B's id.
      const seen = await a.call('sessions_get', { session_id: id })
      const asA = ok<{ session: { owner: unknown }; transcript: Record<string, unknown>[] }>(seen)
      expect(asA.session.owner).toEqual({ kind: 'bearer', label: 'another MCP token' })
      expect(asA.transcript).toContainEqual(
        expect.objectContaining({ type: 'session.owner', owner: { kind: 'bearer', label: 'another MCP token' } }),
      )
      expect(asA.transcript).toContainEqual(
        expect.objectContaining({ type: 'user.turn', text: 'b here', author: { kind: 'bearer', label: 'another MCP token' } }),
      )
      const bId = b.owner.id.slice('token:'.length)
      expect(JSON.stringify(seen)).not.toContain(bId)
      expect(JSON.stringify(await a.call('sessions_list'))).not.toContain(bId)
      const resource = await a.client.readResource({ uri: `scadbuddy://sessions/${id}` })
      expect(JSON.stringify(resource)).not.toContain(bId)
      // And B, the owner now, does not see A's.
      expect(JSON.stringify(await b.call('sessions_get', { session_id: id }))).not.toContain(a.owner.id.slice('token:'.length))
    })

    it('lets the owner withdraw an offer and the target decline one', async () => {
      const { agent } = await setup()
      const a = await agent('write')
      const b = await agent('write')
      for (const who of ['owner', 'target'] as const) {
        const { session } = ok<{ session: { id: string } }>(await a.call('sessions_start', { title: who }))
        ok(await a.call('sessions_handoff', { session_id: session.id, to: b.owner.id }))
        const canceller = who === 'owner' ? a : b
        expect(ok(await canceller.call('sessions_cancel_handoff', { session_id: session.id }))).toEqual({ cancelled: true })
        expect(ok(await a.call('sessions_cancel_handoff', { session_id: session.id }))).toEqual({ cancelled: false })
        // The offer is gone: B no longer sees the session, nor can it accept it.
        expect(errorText(await b.call('sessions_accept_handoff', { session_id: session.id }))).toMatch(/no session/)
        expect(errorText(await b.call('sessions_handoff', { session_id: session.id, to: b.owner.id }))).toMatch(/no session/)
        const kept = ok<{ session: unknown }>(await a.call('sessions_get', { session_id: session.id }))
        expect(kept).toMatchObject({ session: { owner: a.owner, offer: null } })
      }
      expect(ok<unknown[]>(await b.call('sessions_list'))).toEqual([])
    })

    it('ends an offer when ownership changes or it expires; the browser still takes a session back', async () => {
      const { agent, sessions } = await setup()
      const a = await agent('write')
      const b = await agent('write')

      // A hands the offered session to the browser user instead: the offer goes with the change.
      const one = ok<{ session: { id: string } }>(await a.call('sessions_start', { title: 'one' })).session.id
      ok(await a.call('sessions_handoff', { session_id: one, to: b.owner.id }))
      ok(await a.call('sessions_handoff', { session_id: one, to: 'browser' }))
      expect(errorText(await b.call('sessions_accept_handoff', { session_id: one }))).toMatch(/no session/)
      expect((await sessions.get(one, BROWSER_USER)).offer).toBeNull()

      // The browser user takes a session back while it is offered (the chat
      // socket's session.handoff): it owns it, and the offer is gone.
      const two = ok<{ session: { id: string } }>(await a.call('sessions_start', { title: 'two' })).session.id
      ok(await a.call('sessions_handoff', { session_id: two, to: b.owner.id }))
      const taken = await sessions.handoff(two, BROWSER_USER, BROWSER_USER)
      expect(taken).toMatchObject({ owner: BROWSER_USER, offer: null })
      expect(errorText(await b.call('sessions_handoff', { session_id: two, to: b.owner.id }))).toMatch(/no session/)
      await (await sessions.send(two, BROWSER_USER, 'mine again')).done
      // Taking back a session it already owns changes nothing.
      expect((await sessions.handoff(two, BROWSER_USER, BROWSER_USER)).owner).toEqual(BROWSER_USER)

      // An offer that has run out is none: not listed, not acceptable.
      const three = ok<{ session: { id: string } }>(await a.call('sessions_start', { title: 'three' })).session.id
      ok(await a.call('sessions_handoff', { session_id: three, to: b.owner.id }))
      expect(ok<unknown[]>(await b.call('sessions_list'))).toHaveLength(1)
      await db.sql`UPDATE ai_sessions SET pending_owner_until = now() - interval '1 second' WHERE id = ${three}`
      expect(ok<unknown[]>(await b.call('sessions_list'))).toEqual([])
      expect(errorText(await b.call('sessions_accept_handoff', { session_id: three }))).toMatch(/no session/)
      expect(ok<{ session: { offer: unknown } }>(await a.call('sessions_get', { session_id: three })).session.offer).toBeNull()
    })

    it('in the harness, refuses to decide approvals or hand a session off', async () => {
      const { sessions } = await setup()
      const svc = services({ sessions })
      const byName = new Map(ALL_TOOLS.map((t) => [t.name, t]))
      const { session } = await sessions.start(BROWSER_USER, { origin: 'chat', title: 'mine' })
      const ctx = {
        ...svc,
        principal: { id: 'browser', kind: 'browser' as const, tiers: ['read', 'write', 'outward'] as Tier[] },
        progress: async () => {},
        signal: new AbortController().signal,
        gate: 'harness' as const,
      }
      for (const [name, args] of [
        ['sessions_approve', { approval_id: session.id }],
        ['sessions_deny', { approval_id: session.id }],
        ['sessions_handoff', { session_id: session.id, to: 'token:0e5a3c1e-1111-4222-8333-944455556666' }],
        ['sessions_accept_handoff', { session_id: session.id }],
        ['sessions_cancel_handoff', { session_id: session.id }],
      ] as const) {
        const result = (await runTool(byName.get(name)!, args, ctx)) as Result
        expect(errorText(result), name).toMatch(/a session model cannot make it/)
      }
      // Reads still work there, as the session's owner.
      const listed = (await runTool(byName.get('sessions_list')!, {}, ctx)) as Result
      expect(ok<unknown[]>(listed)).toHaveLength(1)
    })
  },
)
