import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SDKMessage, SDKUserMessage } from '@anthropic-ai/claude-agent-sdk'
import { connectDatabase, type Database } from '../src/db.js'
import { SettingsStore } from '../src/credentials.js'
import { CONTINUE_PROMPT, NoUsableCredentialError } from '../src/harness/fallback.js'
import { DEFAULT_MAX_BUDGET_USD, DEFAULT_MAX_TURNS, type HarnessRun } from '../src/harness/run.js'
import { HTTP_SERVER, HTTP_TOOL_NAME, SETTING_HTTP_REQUEST } from '../src/harness/httpRequest.js'
import { userMessage } from '../src/harness/inbox.js'
import { sessionWorkDir } from '../src/harness/stateDirs.js'
import {
  BROWSER_TOOLS_GUIDE,
  listQuery,
  RESTARTING,
  SessionError,
  SETTING_SESSION_BUDGET_USD,
  SETTING_SESSION_MAX_TURNS,
  TAKEN_OVER,
  TITLE_MAX,
  titleFrom,
  type TurnOutcome,
} from '../src/sessions/manager.js'
import type { PluginsForRun } from '../src/plugins/forwarder.js'
import type { CheckedPlugin } from '../src/plugins/registry.js'
import { AuditLog } from '../src/audit/log.js'
import { MemoryAudit } from './support/memoryAudit.js'
import { drainRetains, SETTING_RECALL_MAX_QUERY_CHARS } from '../src/memory/hindsight.js'
import { startFakeHindsight } from './support/fakeHindsight.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, agentB, browser, collectUntil, type FakeTurn, manager, promptText, scriptedRunner, tempPaths } from './support/sessions.js'
import { warmHarness } from './support/warm.js'

// The manager's own rules against real Postgres, with a scripted stand-in for
// the SDK so claims, ownership and interrupts are deterministic. The real SDK
// end to end is test/sessions.e2e.test.ts.

describe('titleFrom', () => {
  it('keeps a short first line whole', () => {
    expect(titleFrom('  Build a box\nwith a lid  ')).toBe('Build a box')
  })

  it('cuts by code point, so an emoji at the boundary is never split', () => {
    // 78 ASCII characters, then emoji: a UTF-16 slice at 79 units would end
    // inside the first emoji's surrogate pair.
    const title = titleFrom(`${'a'.repeat(78)}🧩🧩🧩`)
    expect(title).toBe(`${'a'.repeat(78)}🧩…`)
    expect([...title]).toHaveLength(TITLE_MAX)
    expect(title.isWellFormed()).toBe(true)
    // Exactly TITLE_MAX code points is kept whole, though it is more UTF-16 units.
    const exact = `${'a'.repeat(TITLE_MAX - 1)}🧩`
    expect(titleFrom(exact)).toBe(exact)
  })
})

describe.skipIf(!TEST_DATABASE_URL)(
  `SessionManager${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let schema: string
    let url: string
    let drop: () => Promise<void>
    let stop: AbortController
    const others: Database[] = []

    beforeEach(async () => {
      ;({ db, schema, url, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      stop = new AbortController()
    })
    afterEach(async () => {
      stop.abort()
      for (const other of others.splice(0)) await other.close()
      await drop()
    })

    /** A second "replica": its own pool on the same schema. */
    function replica(): Database {
      const other = connectDatabase(url, { searchPath: schema })
      others.push(other)
      return other
    }

    it('serves a principal’s list from the owner and creator indexes, not a table scan', async () => {
      // Only this test's schema: test files run in parallel, each in a throwaway
      // schema, and describing another one's index while it is dropped fails with
      // "could not open relation with OID".
      const indexes = await db.sql<{ indexname: string; indexdef: string }[]>`
        SELECT indexname, indexdef FROM pg_indexes
        WHERE schemaname = current_schema() AND tablename = 'ai_sessions'`
      const defs = Object.fromEntries(indexes.map((i) => [i.indexname, i.indexdef]))
      expect(defs.ai_sessions_owner).toMatch(/\(owner_kind, owner_id, updated_at DESC\)/)
      expect(defs.ai_sessions_creator).toMatch(/\(creator_kind, creator_id, updated_at DESC\)/)

      // 20,000 sessions spread over 2,000 principals.
      await db.sql.unsafe(`
        INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,
                                 status, max_turns, budget_usd, updated_at)
        SELECT gen_random_uuid(), 'mcp', 'bearer', 'token:' || (g % 2000), 'agent', 'bearer',
               'token:' || ((g + 7) % 2000), 'idle', 10, 1, now() - g * interval '1 second'
        FROM generate_series(1, 20000) AS g`)
      await db.sql`ANALYZE ai_sessions`
      const { text, params } = listQuery({ kind: 'bearer', id: 'token:42', label: 'x' })
      const [row] = await db.sql.unsafe<{ 'QUERY PLAN': unknown }[]>(`EXPLAIN (FORMAT JSON) ${text}`, params)
      const plan = JSON.stringify(row?.['QUERY PLAN'])
      expect(plan).not.toContain('Seq Scan')
      expect(plan).toContain('ai_sessions_owner')
      expect(plan).toContain('ai_sessions_creator')
      expect(plan).toContain('ai_sessions_pending_owner')
    })

    it('starts a session with limits from ai_settings, and defaults without them', async () => {
      const paths = await tempPaths()
      const { runner } = scriptedRunner(() => ({ reply: 'hi' }))
      const m = manager({ sql: db.sql, paths, run: runner, settings: new SettingsStore(db.sql) })
      const { session: plain } = await m.start(agentA, { origin: 'mcp', title: 'plain' })
      expect(plain).toMatchObject({
        origin: 'mcp',
        owner: agentA,
        status: 'idle',
        title: 'plain',
        parentId: null,
        maxTurns: DEFAULT_MAX_TURNS,
        budgetUsd: DEFAULT_MAX_BUDGET_USD,
        costUsd: 0,
        turns: 0,
      })
      const settings = new SettingsStore(db.sql)
      await settings.set(SETTING_SESSION_MAX_TURNS, 7)
      await settings.set(SETTING_SESSION_BUDGET_USD, 0.5)
      const { session: capped } = await m.start(agentA, { origin: 'chat' })
      expect(capped).toMatchObject({ maxTurns: 7, budgetUsd: 0.5 })
    })

    it('runs a turn: first with the session id, then resuming it, in the session’s own cwd', async () => {
      const paths = await tempPaths()
      const { runner, runs } = scriptedRunner(() => ({ reply: 'ok', costUsd: 0.02 }))
      const m = manager({ sql: db.sql, paths, run: runner })
      const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'Build a box' })
      expect(session.title).toBe('Build a box')
      expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success', costUsd: 0.02, turns: 1 })
      // The scripted runner writes no transcript, so this still looks like a first turn;
      // mark it written to see the resume path. That also moves the transcript's tail,
      // so the warm process does not serve the next turn (warm.ts): it spawns again.
      await m.store.append({ projectKey: 'p', sessionId: session.id }, [{ type: 'user', uuid: 'x' }])
      await (await m.send(session.id, agentA, 'again')).done
      expect(runs.map((r) => [r.sessionId, r.resume])).toEqual([
        [session.id, undefined],
        [undefined, session.id],
      ])
      expect(runs[0]!.cwd).toBe(sessionWorkDir(paths, session.id))
      // The cache's mirror of the manager's store (warm.ts MirroredTranscript).
      expect(runs[0]!.sessionStore).toBeDefined()
      expect(runs[0]!.includePartialMessages).toBe(true)
      expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle', turnActive: false, turns: 2 })
    })

    it('shows a synthetic API-error message the turn went on from, as it came (#1101)', async () => {
      const paths = await tempPaths()
      const say = (id: string, text: string, error?: string) =>
        ({
          type: 'assistant',
          message: { id, content: [{ type: 'text', text }] },
          parent_tool_use_id: null,
          ...(error ? { error } : {}),
        }) as unknown as SDKMessage
      const runner = (): AsyncIterable<SDKMessage> =>
        (async function* () {
          yield say('msg_syn', 'API Error: 529 Overloaded', 'server_error')
          yield say('msg_2', 'Carried on.')
          yield { type: 'result', subtype: 'success', is_error: false, result: 'Carried on.', total_cost_usd: 0.01, num_turns: 1 } as unknown as SDKMessage
        })()
      const m = manager({ sql: db.sql, paths, run: runner })
      const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
      expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success', turns: 1 })
      const said = (await m.events.read(session.id, 0, 1000))
        .map((e) => e.event)
        .flatMap((e) => (e.type === 'assistant.text.delta' ? [e.delta] : []))
      expect(said).toEqual(['API Error: 529 Overloaded', 'Carried on.'])
      expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle' })
    })

    it("passes ScadBuddy's own plugin to each turn, and says so when Claude Code did not load it (#896)", async () => {
      const paths = await tempPaths()
      const scripted = scriptedRunner(() => ({ reply: 'ok' }))
      const runs: HarnessRun[] = []
      const listed = [[{ path: '/plugins/own' }], []]
      const runner = (run: HarnessRun): AsyncIterable<SDKMessage> =>
        (async function* () {
          runs.push(run)
          yield { type: 'system', subtype: 'init', mcp_servers: [], plugins: listed.shift() } as unknown as SDKMessage
          yield* scripted.runner(run)
        })()
      const m = manager({ sql: db.sql, paths, run: runner, ownPlugin: '/plugins/own' })
      const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
      await turn!.done
      // Only a spawn reports an init: the warm process is let go so the next turn spawns again.
      await m.warm.evict(session.id, 'the test')
      await (await m.send(session.id, agentA, 'again')).done
      expect(runs.map((r) => r.ownPlugin)).toEqual(['/plugins/own', '/plugins/own'])
      const logged = (
        await db.sql<{ event: string }[]>`
          SELECT event FROM ai_session_events WHERE session_id = ${session.id} ORDER BY seq`
      ).map((e) => JSON.parse(e.event) as { type: string; code?: string; message?: string })
      // Only the second turn's init left it out.
      expect(logged.filter((e) => e.code === 'plugin_unavailable').map((e) => e.message)).toEqual([
        "ScadBuddy's own plugin was not loaded by Claude Code: its skills and subagents are unavailable",
      ])
    })

    it("says so in the session when one of the turn's in-process tool servers did not connect (#1255)", async () => {
      // A server Claude Code cannot connect is only logged at debug level, and every
      // tool on it is gone for the turn: the session must keep the evidence.
      const paths = await tempPaths()
      const scripted = scriptedRunner(() => ({ reply: 'ok' }))
      const inits = [
        [{ name: HTTP_SERVER, status: 'failed' }],
        [{ name: HTTP_SERVER, status: 'connected' }],
        [],
      ]
      const runner = (run: HarnessRun): AsyncIterable<SDKMessage> =>
        (async function* () {
          yield { type: 'system', subtype: 'init', mcp_servers: inits.shift(), plugins: [] } as unknown as SDKMessage
          yield* scripted.runner(run)
        })()
      const m = manager({ sql: db.sql, paths, run: runner, settings: new SettingsStore(db.sql), httpRequest: {} })
      const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
      await turn!.done
      // Only a spawn reports an init: the warm process is let go before each later turn.
      await m.warm.evict(session.id, 'the test')
      await (await m.send(session.id, agentA, 'again')).done
      await m.warm.evict(session.id, 'the test')
      await (await m.send(session.id, agentA, 'and again')).done
      const logged = (
        await db.sql<{ event: string }[]>`
          SELECT event FROM ai_session_events WHERE session_id = ${session.id} ORDER BY seq`
      ).map((e) => JSON.parse(e.event) as { type: string; code?: string; message?: string })
      expect(logged.filter((e) => e.code === 'tools_unavailable').map((e) => e.message)).toEqual([
        `the ${HTTP_SERVER} tools are not available in this turn: Claude Code reports their in-process MCP server as failed`,
        `the ${HTTP_SERVER} tools are not available in this turn: Claude Code reports their in-process MCP server as missing`,
      ])
    })

    it('rejects a concurrent send on the same replica with a clear error', async () => {
      const paths = await tempPaths()
      const { runner } = scriptedRunner(() => ({ hang: true }))
      const m = manager({ sql: db.sql, paths, run: runner })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      const first = await m.send(session.id, agentA, 'one')
      const err = await m.send(session.id, agentA, 'two').catch((e: unknown) => e)
      expect(err).toBeInstanceOf(SessionError)
      expect(err).toMatchObject({ code: 'busy', status: 409, message: expect.stringMatching(/already running/) })
      await m.interrupt(session.id, agentA)
      await first.done
    })

    it('lets exactly one of two replicas claim a turn when both send at once', async () => {
      const paths = await tempPaths()
      const turns: FakeTurn[] = []
      const { runner } = scriptedRunner(() => turns.shift() ?? { hang: true })
      const a = manager({ sql: db.sql, paths, run: runner })
      const b = manager({ sql: replica().sql, paths, run: runner })
      const { session } = await a.start(agentA, { origin: 'mcp' })
      const results = await Promise.allSettled([
        a.send(session.id, agentA, 'from a'),
        b.send(session.id, agentA, 'from b'),
        a.send(session.id, agentA, 'from a again'),
        b.send(session.id, agentA, 'from b again'),
      ])
      const won = results.filter((r) => r.status === 'fulfilled')
      const lost = results.filter((r): r is PromiseRejectedResult => r.status === 'rejected')
      expect(won).toHaveLength(1)
      expect(lost.map((r) => (r.reason as SessionError).code)).toEqual(['busy', 'busy', 'busy'])
      // Interrupt from the OTHER replica than the one running it, whichever that is.
      await a.interrupt(session.id, agentA)
      await b.interrupt(session.id, agentA)
      expect(await (won[0] as PromiseFulfilledResult<{ done: Promise<TurnOutcome> }>).value.done).toEqual({
        kind: 'interrupted',
      })
    })

    it('interrupts a turn running on another replica through the database flag', async () => {
      const paths = await tempPaths()
      const { runner } = scriptedRunner(() => ({ hang: true }))
      const runnerSide = manager({ sql: db.sql, paths, run: runner })
      const watcherSide = manager({ sql: replica().sql, paths, run: runner })
      const { session } = await runnerSide.start(agentA, { origin: 'mcp' })
      const turn = await runnerSide.send(session.id, agentA, 'long job')
      // A watcher (the browser) on the other replica stops it.
      expect(await watcherSide.interrupt(session.id, browser)).toBe(true)
      expect(await turn.done).toEqual({ kind: 'interrupted' })
      const after = await watcherSide.get(session.id, browser)
      expect(after).toMatchObject({ status: 'idle', turnActive: false })
      expect(await watcherSide.interrupt(session.id, browser)).toBe(false)
      // The session takes a new turn afterwards.
      const { runner: ok } = scriptedRunner(() => ({ reply: 'done' }))
      const next = manager({ sql: db.sql, paths, run: ok })
      expect(await (await next.send(session.id, agentA, 'again')).done).toMatchObject({ kind: 'result' })
    })

    it('answers not_found, not a database error, for ids that are not UUIDs', async () => {
      const paths = await tempPaths()
      const m = manager({ sql: db.sql, paths, run: scriptedRunner(() => ({ reply: 'x' })).runner })
      // Both pass a loose /^[0-9a-f-]{36}$/ check but are not UUIDs to Postgres.
      for (const bad of ['-'.repeat(36), '0f8fad5bd9cb-469f-a165-70867728950e-', 'nope']) {
        await expect(m.get(bad, browser)).rejects.toMatchObject({ code: 'not_found' })
        await expect(m.send(bad, browser, 'hi')).rejects.toMatchObject({ code: 'not_found' })
        await expect(m.interrupt(bad, browser)).rejects.toMatchObject({ code: 'not_found' })
        await expect(m.attach(bad, browser)).rejects.toMatchObject({ code: 'not_found' })
      }
    })

    it('reports an interrupt that comes after the result as not stopping anything', async () => {
      const paths = await tempPaths()
      let release!: () => void
      const hold = new Promise<void>((r) => (release = r))
      const { runner } = scriptedRunner(() => ({ reply: 'done', holdAfterResult: hold }))
      const m = manager({ sql: db.sql, paths, run: runner })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      const turn = await m.send(session.id, agentA, 'x')
      await collectUntil(await m.attach(session.id, agentA, { signal: stop.signal }), (e) => e.event.type === 'assistant.text.done')
      // The result has been yielded; the stream is still open (the SDK's last appends).
      await new Promise((r) => setTimeout(r, 50))
      expect(await m.interrupt(session.id, agentA)).toBe(false)
      release()
      expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    })

    it('redacts the turn’s credential from everything it logs', async () => {
      const paths = await tempPaths()
      const turns: FakeTurn[] = [{ reply: 'your key is sk-ant-test, right?' }, { throws: 'upstream said: bad key sk-ant-test' }]
      const { runner } = scriptedRunner(() => turns.shift()!)
      const m = manager({ sql: db.sql, paths, run: runner })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      await (await m.send(session.id, agentA, 'hi')).done
      // On a fresh process: a warm one that throws before its first message is taken
      // for dead and the turn tried once more (sessions/warmTurn.ts), which the script
      // has no turn for.
      await m.warm.evict(session.id, 'the test')
      expect(await (await m.send(session.id, agentA, 'again')).done).toEqual({
        kind: 'failed',
        message: 'upstream said: bad key [redacted]',
      })
      const logged = JSON.stringify(await m.events.read(session.id))
      expect(logged).not.toContain('sk-ant-test')
      expect(logged).toContain('your key is [redacted], right?')
    })

    it('redacts the credential from a failure in the turn’s set-up (after the credential was fetched)', async () => {
      const paths = await tempPaths()
      const { runner, runs } = scriptedRunner(() => ({ reply: 'unused' }))
      const m = manager({ sql: db.sql, paths, run: runner })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      m.store.exists = () => Promise.reject(new Error('lookup failed for key sk-ant-test'))
      expect(await (await m.send(session.id, agentA, 'hi')).done).toEqual({
        kind: 'failed',
        message: 'lookup failed for key [redacted]',
      })
      expect(runs).toHaveLength(0)
      const logged = JSON.stringify(await m.events.read(session.id))
      expect(logged).not.toContain('sk-ant-test')
      expect(logged).toContain('lookup failed for key [redacted]')
    })

    it('keeps follower bookkeeping only while someone follows, and still wakes late followers', async () => {
      const paths = await tempPaths()
      const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
      // A long poll, so only a local wake can deliver quickly.
      const m = manager({ sql: db.sql, paths, run: runner, pollMs: 60_000 })
      const sessions = await Promise.all([1, 2, 3].map(() => m.start(agentA, { origin: 'mcp' })))
      for (const { session } of sessions) await (await m.send(session.id, agentA, 'hi')).done
      // Many sessions written, nobody following: nothing retained.
      expect(m.events.watchedSessions()).toBe(0)

      const id = sessions[0]!.session.id
      const last = (await m.events.read(id)).at(-1)!.seq
      const a = new AbortController()
      const b = new AbortController()
      const followA = await m.attach(id, agentA, { afterSeq: last, signal: a.signal })
      const followB = await m.attach(id, agentA, { afterSeq: last, signal: b.signal })
      const gotA = collectUntil(followA, (e) => e.event.type === 'session.status' && e.event.status === 'idle', 5_000)
      const gotB = collectUntil(followB, (e) => e.event.type === 'session.status' && e.event.status === 'idle', 5_000)
      await new Promise((r) => setTimeout(r, 50))
      expect(m.events.watchedSessions()).toBe(1)
      const started = performance.now()
      await (await m.send(id, agentA, 'again')).done
      expect((await gotA).at(0)?.event.type).toBe('user.turn')
      expect((await gotB).at(0)?.event.type).toBe('user.turn')
      expect(performance.now() - started).toBeLessThan(5_000)
      // collectUntil stopped iterating: both followers detached, the entry is gone.
      expect(m.events.watchedSessions()).toBe(0)

      // A follower that starts after the entry was dropped still replays and goes live.
      const c = new AbortController()
      const late = collectUntil(
        await m.attach(id, agentA, { afterSeq: last, signal: c.signal }),
        (e) => e.event.type === 'user.turn' && e.event.text === 'third',
        5_000,
      )
      await (await m.send(id, agentA, 'third')).done
      expect((await late).filter((e) => e.event.type === 'user.turn').map((e) => (e.event.type === 'user.turn' ? e.event.text : ''))).toEqual([
        'again',
        'third',
      ])
      expect(m.events.watchedSessions()).toBe(0)
      a.abort()
      b.abort()
      c.abort()
    })

    it('frees a claim whose replica died once its lease runs out', async () => {
      const paths = await tempPaths()
      const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
      const m = manager({ sql: db.sql, paths, run: runner, leaseMs: 200 })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      // A turn claimed by a replica that is gone: no renewal.
      await db.sql`
        UPDATE ai_sessions SET status = 'running', turn_id = gen_random_uuid(),
          lease_until = now() + interval '150 milliseconds'
        WHERE id = ${session.id}`
      await expect(m.send(session.id, agentA, 'hello')).rejects.toMatchObject({ code: 'busy' })
      await new Promise((r) => setTimeout(r, 250))
      expect((await m.get(session.id, agentA)).turnActive).toBe(false)
      expect(await (await m.send(session.id, agentA, 'hello')).done).toMatchObject({ kind: 'result' })
    })

    it('reaps a turn whose lease ran out: it says it stopped, and the session is idle again', async () => {
      const paths = await tempPaths()
      const { runner } = scriptedRunner(() => ({ hang: true }))
      const m = manager({ sql: db.sql, paths, run: runner })
      const dead = (await m.start(agentA, { origin: 'mcp' })).session
      const parked = (await m.start(agentA, { origin: 'mcp' })).session
      const live = (await m.start(agentA, { origin: 'mcp' })).session
      // Two turns whose process died before finish() (a restart closed the pool
      // under them, 2026-09-30); one of them left an approval pending.
      await db.sql`
        UPDATE ai_sessions SET status = 'running', turn_id = gen_random_uuid(), lease_until = now() - interval '1 second'
        WHERE id IN ${db.sql([dead.id, parked.id])}`
      await db.sql`
        INSERT INTO ai_approvals (id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
                                  requested_by_kind, requested_by_id, requested_by_label, requested_tiers, expires_at)
        SELECT gen_random_uuid(), id, turn_id, 't1', 'send_to_bambuddy', 'send', 'h', 'outward',
               'bearer', 'token:a', 'Agent A', '{}', now() + interval '1 hour'
        FROM ai_sessions WHERE id = ${parked.id}`
      // A live one, on this replica.
      const running = await m.send(live.id, agentA, 'long job')

      expect((await m.reapExpired()).sort()).toEqual([dead.id, parked.id].sort())
      expect(await m.get(dead.id, agentA)).toMatchObject({ status: 'idle', turnActive: false })
      expect(await m.get(parked.id, agentA)).toMatchObject({ status: 'waiting_approval', turnActive: false })
      expect(await m.get(live.id, agentA)).toMatchObject({ status: 'running', turnActive: true })
      const events = (await m.events.read(dead.id)).map((e) => e.event)
      expect(events.at(-2)).toMatchObject({ type: 'error', code: 'interrupted' })
      expect(events.at(-1)).toMatchObject({ type: 'session.status', status: 'idle' })
      // Reaped once: a second sweep (or another replica's) finds nothing.
      expect(await m.reapExpired()).toEqual([])
      // And the session takes a turn again.
      const { runner: ok } = scriptedRunner(() => ({ reply: 'back' }))
      const after = manager({ sql: db.sql, paths, run: ok })
      expect(await (await after.send(dead.id, agentA, 'hello')).done).toMatchObject({ kind: 'result' })

      await m.interrupt(live.id, agentA)
      await running.done
    })

    it('says what it can tell of why it reaped a turn, and writes the turn its audit row (#2375)', async () => {
      const paths = await tempPaths()
      const audit = new MemoryAudit()
      // No renewals: this replica's turn stops renewing, as a stalled process's does.
      // It still reports what it spent once stopped: the requests were billed (#2981 review).
      const { runner } = scriptedRunner(() => ({ hang: true, resultOnAbortUsd: 0.02 }))
      const m = manager({ sql: db.sql, paths, run: runner, audit, renewMs: 60_000, leaseMs: 90_000 })
      const elsewhere = (await m.start(agentA, { origin: 'mcp' })).session
      const here = (await m.start(agentA, { origin: 'mcp' })).session
      // A turn another replica was running, and one running on this replica.
      const [dead] = await db.sql<{ turn_id: string }[]>`
        UPDATE ai_sessions SET status = 'running', turn_id = gen_random_uuid(), lease_until = now() - interval '1 second'
        WHERE id = ${elsewhere.id} RETURNING turn_id::text`
      const deadTurn = dead!.turn_id
      const running = await m.send(here.id, agentA, 'long job')
      await db.sql`UPDATE ai_sessions SET lease_until = now() - interval '1 second' WHERE id = ${here.id}`

      expect((await m.reapExpired()).sort()).toEqual([elsewhere.id, here.id].sort())
      const error = async (id: string) =>
        (await m.events.read(id)).map((e) => e.event).find((e) => e.type === 'error' && e.code === 'interrupted')
      expect(await error(elsewhere.id)).toMatchObject({
        message: 'the turn was stopped: its claim on the session lapsed (the replica running it stopped, or could not reach the database)',
      })
      expect(await error(here.id)).toMatchObject({
        message: 'the turn was stopped: it could not renew its claim on the session for 90 seconds, though its replica is running',
      })
      expect(JSON.stringify(await m.events.read(elsewhere.id))).not.toContain('restarted')
      // This replica's turn is stopped too, not left running without its claim.
      expect(await running.done).toEqual({ kind: 'lost_claim' })
      const [spent] = await db.sql<{ cost_usd: number }[]>`SELECT cost_usd FROM ai_sessions WHERE id = ${here.id}`
      expect(spent!.cost_usd).toBeCloseTo(0.02)
      expect(audit.entries.filter((e) => e.kind === 'turn' && e.action === 'lost_claim')).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            sessionId: elsewhere.id,
            turnId: deadTurn,
            outcome: 'error',
            actor: { kind: agentA.kind, id: agentA.id, label: agentA.label },
          }),
          expect.objectContaining({ sessionId: here.id, outcome: 'error' }),
        ]),
      )
    })

    it('gives a lapsed turn its one turn row whether a send takes its session or the reaper does (#3320)', async () => {
      const paths = await tempPaths()
      const audit = new MemoryAudit()
      const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
      const m = manager({ sql: db.sql, paths, run: runner, audit })
      const taken = (await m.start(agentA, { origin: 'mcp' })).session
      const reaped = (await m.start(agentA, { origin: 'mcp' })).session
      // Two turns whose replica stopped renewing their claims.
      const lapsed = await db.sql<{ id: string; turn_id: string }[]>`
        UPDATE ai_sessions SET status = 'running', turn_id = gen_random_uuid(), lease_until = now() - interval '1 second'
        WHERE id IN ${db.sql([taken.id, reaped.id])} RETURNING id::text, turn_id::text`
      const turnOf = (id: string) => lapsed.find((r) => r.id === id)!.turn_id

      // A send takes one before any reap; the reaper takes the other, then a send follows.
      await (await m.send(taken.id, agentA, 'hello')).done
      expect(await m.reapExpired()).toEqual([reaped.id])
      await (await m.send(reaped.id, agentA, 'hello')).done

      const rows = audit.entries.filter((e) => e.kind === 'turn' && e.action === 'lost_claim')
      expect(rows).toHaveLength(2)
      expect(rows).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            sessionId: taken.id,
            turnId: turnOf(taken.id),
            outcome: 'error',
            detail: TAKEN_OVER,
            actor: { kind: agentA.kind, id: agentA.id, label: agentA.label },
          }),
          expect.objectContaining({ sessionId: reaped.id, turnId: turnOf(reaped.id), outcome: 'error' }),
        ]),
      )
    })

    it('a resumed approval that takes a lapsed turn\'s session writes that turn its row (#2981 review)', async () => {
      const paths = await tempPaths()
      const audit = new MemoryAudit()
      const { runner } = scriptedRunner(() => ({ reply: 'done' }))
      const m = manager({ sql: db.sql, paths, run: runner, audit })
      const { session } = await m.start(browser, { origin: 'chat' })
      // A turn that parked an outward call, then whose replica stopped renewing its claim.
      const [lapsed] = await db.sql<{ turn_id: string }[]>`
        UPDATE ai_sessions SET status = 'waiting_approval', turn_id = gen_random_uuid(),
          lease_until = now() - interval '1 second'
        WHERE id = ${session.id} RETURNING turn_id::text`
      const approval = await m.approvals.create({
        sessionId: session.id,
        turnId: lapsed!.turn_id,
        toolUseId: 'toolu_out',
        tool: 'mcp__stub__print',
        input: { job: 'box.3mf' },
        tier: 'outward',
        requestedBy: browser,
      })
      // Approved after its turn was lost: the decision starts the turn that re-runs it.
      await m.approvals.decide(browser, approval.id, true)
      const [bound] = await db.sql<{ resume_turn_id: string | null }[]>`
        SELECT resume_turn_id::text FROM ai_approvals WHERE id = ${approval.id}`
      expect(bound!.resume_turn_id).not.toBeNull()

      await expect
        .poll(() => audit.entries.filter((e) => e.kind === 'turn' && e.action === 'lost_claim'))
        .toEqual([expect.objectContaining({ sessionId: session.id, turnId: lapsed!.turn_id, detail: TAKEN_OVER })])
    })

    it("leaves a newer turn's approvals alone when its claim goes after the lost check (#2981 review)", async () => {
      const paths = await tempPaths()
      const { runner } = scriptedRunner(() => ({ reply: 'nearly done' }))
      // No renewals: nothing tells this turn its claim went.
      const m = manager({ sql: db.sql, paths, run: runner, renewMs: 60_000 })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      // Its result is in and runTurn's lost check passed: finish() has begun (its first
      // step is this cancel). Before its own check, another replica's turn takes the
      // session and parks a call. Here, not on a timer: a turn that finished first
      // would make this race the test's to lose.
      const cancelQuestions = m.questions.cancelPending.bind(m.questions)
      vi.spyOn(m.questions, 'cancelPending').mockImplementationOnce(async (...args) => {
        await db.sql`
          UPDATE ai_sessions SET status = 'waiting_approval', turn_id = gen_random_uuid(), lease_until = now() + interval '1 minute'
          WHERE id = ${session.id}`
        await db.sql`
          INSERT INTO ai_approvals (id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
                                    requested_by_kind, requested_by_id, requested_by_label, requested_tiers, expires_at)
          SELECT gen_random_uuid(), id, turn_id, 't2', 'send_to_bambuddy', 'send', 'h', 'outward',
                 'bearer', 'token:a', 'Agent A', '{}', now() + interval '1 hour'
          FROM ai_sessions WHERE id = ${session.id}`
        return cancelQuestions(...args)
      })
      const running = await m.send(session.id, agentA, 'job')

      expect(await running.done).toEqual({ kind: 'lost_claim' })
      const approvals = await db.sql<{ decision: string | null }[]>`
        SELECT decision FROM ai_approvals WHERE session_id = ${session.id}`
      expect(approvals).toEqual([{ decision: null }])
      // Its result's spend is the session's all the same (#2981 review).
      const [spent] = await db.sql<{ cost_usd: number }[]>`SELECT cost_usd FROM ai_sessions WHERE id = ${session.id}`
      expect(spent!.cost_usd).toBeCloseTo(0.01)
      // Nor is its process kept for a session another turn owns now (#2981 review).
      await expect.poll(() => m.warm.size()).toBe(0)
      await m.warm.close()
    })

    it('attributes a dead turn to the local one only when it is that turn (#2981 review)', async () => {
      const paths = await tempPaths()
      const { runner, runs } = scriptedRunner(() => ({ hang: true }))
      const m = manager({ sql: db.sql, paths, run: runner, renewMs: 60_000 })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      const running = await m.send(session.id, agentA, 'long job')
      // The claim the reaper finds is another turn's: this replica's turn is not the one that died.
      await db.sql`
        UPDATE ai_sessions SET turn_id = gen_random_uuid(), lease_until = now() - interval '1 second'
        WHERE id = ${session.id}`
      expect(await m.reapExpired()).toEqual([session.id])
      const error = (await m.events.read(session.id)).map((e) => e.event).find((e) => e.type === 'error')
      expect(error).toMatchObject({
        message: 'the turn was stopped: its claim on the session lapsed (the replica running it stopped, or could not reach the database)',
      })
      // Not this replica's turn that died: the reaper never stopped it.
      expect(runs[0]!.signal?.aborted).toBe(false)
      m.abortAll()
      await running.done
    })

    it("leaves a turn sent after the reap its approval: the reaped turn's own ending cancels nothing (#2981 review)", async () => {
      const paths = await tempPaths()
      const { runner } = scriptedRunner(() => ({ hang: true }))
      const m = manager({ sql: db.sql, paths, run: runner, renewMs: 60_000 })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      const running = await m.send(session.id, agentA, 'long job')
      await db.sql`UPDATE ai_sessions SET lease_until = now() - interval '1 second' WHERE id = ${session.id}`
      // The reaped turn's ending is held until the newer turn has parked its approval,
      // so a session-wide cancel from it, if any, lands after.
      let parked!: () => void
      const newerParked = new Promise<void>((resolve) => (parked = resolve))
      const cancel = m.approvals.cancelPending.bind(m.approvals)
      vi.spyOn(m.approvals, 'cancelPending').mockImplementation(async (...args) => {
        await newerParked
        return cancel(...args)
      })

      expect(await m.reapExpired()).toEqual([session.id])
      // Sent again, on another replica: it claims the session and parks an outward call.
      await db.sql`
        UPDATE ai_sessions SET status = 'waiting_approval', turn_id = gen_random_uuid(),
          lease_until = now() + interval '1 minute'
        WHERE id = ${session.id}`
      await db.sql`
        INSERT INTO ai_approvals (id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
                                  requested_by_kind, requested_by_id, requested_by_label, requested_tiers, expires_at)
        SELECT gen_random_uuid(), id, turn_id, 't2', 'send_to_bambuddy', 'send', 'h', 'outward',
               'bearer', 'token:a', 'Agent A', '{}', now() + interval '1 hour'
        FROM ai_sessions WHERE id = ${session.id}`
      parked()

      expect(await running.done).toEqual({ kind: 'lost_claim' })
      const approvals = await db.sql<{ decision: string | null }[]>`
        SELECT decision FROM ai_approvals WHERE session_id = ${session.id}`
      expect(approvals).toEqual([{ decision: null }])
    })

    it('on a restart, lets a running turn finish, then aborts the rest and waits for them to record it', async () => {
      const paths = await tempPaths()
      let release!: () => void
      const held = new Promise<void>((resolve) => (release = resolve))
      const turns: FakeTurn[] = [{ reply: 'nearly done', holdAfterResult: held }, { hang: true }]
      const { runner } = scriptedRunner(() => turns.shift()!)
      const m = manager({ sql: db.sql, paths, run: runner })
      const finishing = (await m.start(agentA, { origin: 'mcp' })).session
      const stuck = (await m.start(agentA, { origin: 'mcp' })).session
      const first = await m.send(finishing.id, agentA, 'a')
      const second = await m.send(stuck.id, agentA, 'b')
      // The grace period ends when the test says, not after a wall-clock wait a loaded
      // runner can outlast: its timer is the one set for GRACE_MS, held until called.
      const GRACE_MS = 987_654
      let endGrace: (() => void) | undefined
      const realSetTimeout = globalThis.setTimeout
      const timers = vi.spyOn(globalThis, 'setTimeout').mockImplementation(((
        fn: (...args: unknown[]) => void,
        ms?: number,
        ...args: unknown[]
      ) => {
        if (ms !== GRACE_MS) return realSetTimeout(fn, ms, ...args)
        endGrace = () => fn(...args)
        return realSetTimeout(() => {}, 2 ** 31 - 1)
      }) as typeof setTimeout)

      const stopping = m.stopTurns({ graceMs: GRACE_MS, abortWaitMs: 5_000 })
      // Draining: nothing new starts here.
      await expect(m.send(finishing.id, agentA, 'c')).rejects.toMatchObject({ code: 'busy', message: RESTARTING })
      release()
      // The one that could finish did, inside the grace period.
      expect(await first.done).toMatchObject({ kind: 'result' })
      expect(endGrace).toBeDefined()
      endGrace!()
      await stopping
      timers.mockRestore()
      // The other was aborted and recorded it before stopTurns returned, so the pool can close now.
      expect(await second.done).toEqual({ kind: 'interrupted' })
      expect(await m.get(stuck.id, agentA)).toMatchObject({ status: 'idle', turnActive: false })
      const events = (await m.events.read(stuck.id)).map((e) => e.event)
      expect(events.at(-2)).toMatchObject({ type: 'error', code: 'interrupted' })
      expect(events.at(-1)).toMatchObject({ type: 'session.status', status: 'idle' })
    })

    it('records a failed turn and says why', async () => {
      const paths = await tempPaths()
      const { runner } = scriptedRunner(() => ({ throws: 'spawn failed' }))
      const m = manager({ sql: db.sql, paths, run: runner })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      expect(await (await m.send(session.id, agentA, 'x')).done).toEqual({ kind: 'failed', message: 'spawn failed' })
      expect((await m.get(session.id, agentA)).status).toBe('failed')
      const events = (await m.events.read(session.id)).map((e) => e.event)
      expect(events.at(-2)).toMatchObject({ type: 'error', code: 'turn_failed', message: 'spawn failed' })
      expect(events.at(-1)).toMatchObject({ type: 'session.status', status: 'failed' })
    })

    it('says when a credential is usable again when none is now, and starts no query (#1093)', async () => {
      const paths = await tempPaths()
      const { runner, runs } = scriptedRunner(() => ({ reply: 'never' }))
      const why = 'no Claude credential is usable: credential 1 (API key …aaaa) is rate limited until 2026-10-03T12:05:00.000Z. The first is usable again at 2026-10-03T12:05:00.000Z'
      const m = manager({
        sql: db.sql,
        paths,
        run: runner,
        credentials: {
          candidates: () => Promise.reject(new NoUsableCredentialError(why, new Date('2026-10-03T12:05:00Z'))),
          reporter: () => () => Promise.resolve(),
        },
      })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      expect(await (await m.send(session.id, agentA, 'x')).done).toEqual({ kind: 'failed', message: why })
      expect(runs).toHaveLength(0)
      const events = (await m.events.read(session.id)).map((e) => e.event)
      expect(events.at(-2)).toMatchObject({ type: 'error', code: 'turn_failed', message: why })
    })

    it('gives a turn memory hooks only when it loaded an enabled hindsight plugin, recalling the user’s words', async () => {
      const fake = await startFakeHindsight()
      try {
        const paths = await tempPaths()
        const { runner, runs, prompts } = scriptedRunner(() => ({ reply: 'ok' }))
        const hindsight: CheckedPlugin = {
          plugin: { name: 'hindsight', url: `http://hindsight.invalid:${fake.port}/mcp/b/`, toolTiers: {}, disabledTools: [] },
          address: '127.0.0.1',
        }
        // What forwardForRun gives the manager: the plugin is there only when
        // it is enabled and its endpoint passed the egress check this turn.
        let loaded: CheckedPlugin | undefined = hindsight
        const remotePlugins = (): Promise<PluginsForRun> =>
          Promise.resolve({ plugins: [], problems: [], secrets: [], ...(loaded ? { hindsight: loaded } : {}), terminate: async () => {}, release: () => {} })
        const context = '<page_context>\n{"route":"/"}\n</page_context>'
        const m = manager({ sql: db.sql, paths, run: runner, remotePlugins })
        const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'Make a box', context })
        await turn!.done
        const hooks = runs[0]!.memoryHooks
        expect(Object.keys(hooks ?? {}).sort()).toEqual(['Stop', 'UserPromptSubmit'])
        // The scripted process fired the hooks as Claude Code does (test/support/sessions.ts):
        // the recall was against the user's words, not the prompt with its page context.
        expect(promptText(prompts[0]!)).toBe(`Make a box\n\n${context}`)
        expect(fake.recalls().map((r) => (r.body as { query: string }).query)).toEqual(['Make a box'])

        // A plugin no longer loaded changes what the process was spawned with: the next turn spawns again.
        loaded = undefined
        await (await m.send(session.id, agentA, 'again')).done
        expect(runs[1]!.memoryHooks).toBeUndefined()

        loaded = hindsight
        const off = manager({ sql: db.sql, paths, run: runner, remotePlugins, memory: false })
        await (await off.send(session.id, agentA, 'once more')).done
        expect(runs[2]!.memoryHooks).toBeUndefined()
      } finally {
        await fake.close()
      }
    })

    it('reads the recall query cap from Settings at each turn (memory_recall_max_query_chars)', async () => {
      const fake = await startFakeHindsight()
      try {
        const paths = await tempPaths()
        const { runner, runs } = scriptedRunner(() => ({ reply: 'ok' }))
        const hindsight: CheckedPlugin = {
          plugin: { name: 'hindsight', url: `http://hindsight.invalid:${fake.port}/mcp/b/`, toolTiers: {}, disabledTools: [] },
          address: '127.0.0.1',
        }
        const remotePlugins = (): Promise<PluginsForRun> =>
          Promise.resolve({ plugins: [], problems: [], secrets: [], hindsight, terminate: async () => {}, release: () => {} })
        const settings = new SettingsStore(db.sql)
        const m = manager({ sql: db.sql, paths, run: runner, remotePlugins, settings, memory: { hookConfig: { autoRetain: false } } })
        // Each turn's process fires the recall (test/support/sessions.ts); a changed cap is
        // in the fingerprint, so each change spawns a process with the new one (warm.ts).
        const long = 'a'.repeat(1000)
        const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: long })
        await turn!.done
        await settings.set(SETTING_RECALL_MAX_QUERY_CHARS, 12)
        await (await m.send(session.id, agentA, long)).done
        await settings.set(SETTING_RECALL_MAX_QUERY_CHARS, 0)
        await (await m.send(session.id, agentA, long)).done
        expect(runs).toHaveLength(3)
        expect(fake.recalls().map((r) => (r.body as { query: string }).query.length)).toEqual([800, 12, 1000])
      } finally {
        await fake.close()
      }
    })

    it('audits one recall per user message when the turn falls back to another credential (#1896)', async () => {
      const fake = await startFakeHindsight()
      try {
        fake.memories = ['The user prints in PETG.']
        const paths = await tempPaths()
        const hindsight: CheckedPlugin = {
          plugin: { name: 'hindsight', url: `http://hindsight.invalid:${fake.port}/mcp/bank1/`, toolTiers: {}, disabledTools: [] },
          address: '127.0.0.1',
        }
        const remotePlugins = (): Promise<PluginsForRun> =>
          Promise.resolve({ plugins: [], problems: [], secrets: [], hindsight, terminate: async () => {}, release: () => {} })
        const audit = new AuditLog({ sql: db.sql })
        const pooled = (id: string, secret: string) => ({
          id,
          epoch: 0,
          label: `credential ${id}`,
          credential: { kind: 'anthropic_api_key' as const, secret },
        })
        const runs: HarnessRun[] = []
        const prompts: string[] = []
        // Claude Code fires UserPromptSubmit for every prompt pushed into a process, the
        // continuation's too; the first credential is refused after its init, so the
        // second credential's process resumes.
        const runner = (run: HarnessRun): AsyncIterable<SDKMessage> => {
          runs.push(run)
          const attempt = runs.length
          const session_id = run.sessionId ?? run.resume ?? 'unknown'
          return (async function* () {
            const signal = new AbortController().signal
            for await (const message of run.prompt as AsyncIterable<SDKUserMessage>) {
              const prompt = promptText(message)
              prompts.push(prompt)
              for (const matcher of run.memoryHooks?.UserPromptSubmit ?? []) {
                await matcher.hooks[0]!(
                  { session_id, transcript_path: '/nonexistent', cwd: '/', hook_event_name: 'UserPromptSubmit', prompt },
                  undefined,
                  { signal },
                )
              }
              yield { type: 'system', subtype: 'init', session_id, model: 'claude-sonnet-4-5', mcp_servers: [] } as unknown as SDKMessage
              if (attempt === 1) {
                yield { type: 'system', subtype: 'api_retry', attempt: 1, max_retries: 10, retry_delay_ms: 500, error_status: 401, error: 'authentication_failed' } as unknown as SDKMessage
                return
              }
              yield { type: 'assistant', message: { content: [{ type: 'text', text: 'ok' }] }, parent_tool_use_id: null, session_id } as unknown as SDKMessage
              yield { type: 'result', subtype: 'success', is_error: false, result: 'ok', num_turns: 1, total_cost_usd: 0.01, session_id } as unknown as SDKMessage
            }
          })()
        }
        const m = manager({
          sql: db.sql,
          paths,
          run: runner,
          remotePlugins,
          audit,
          memory: { hookConfig: { autoRetain: false } },
          credentials: {
            candidates: () => Promise.resolve([pooled('a', 'sk-ant-key-aaaa'), pooled('b', 'sk-ant-key-bbbb')]),
            reporter: () => () => Promise.resolve(),
          },
          probe: () => Promise.resolve({ verdict: 'refused', reason: 'HTTP 401 again' }),
        })
        const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'Make a box' })
        expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
        expect(prompts).toEqual(['Make a box', CONTINUE_PROMPT])
        await expect.poll(async () => (await audit.list({ sessionId: session.id, kind: 'memory' })).entries.length).toBe(1)
        const { entries } = await audit.list({ sessionId: session.id, kind: 'memory' })
        expect(entries.map((e) => [e.action, e.turn_id])).toEqual([['recall', turn!.turnId]])
        expect(fake.recalls()).toHaveLength(1)
      } finally {
        await fake.close()
      }
    })

    it('writes an audit row and a panel event per automatic recall and retain, a retain after the turn ended included', async () => {
      const fake = await startFakeHindsight()
      try {
        fake.memories = ['The user prints in PETG.', 'Boxes get 2 mm walls.']
        // The retain is fire-and-forget (memory/hindsight.ts): answered this late, it
        // finishes after the turn ended, the case the panel event and audit row must still cover.
        fake.respond = (r) => (r.path.endsWith('/memories') ? { json: { success: true, operation_id: 'op-late' }, delayMs: 1_500 } : undefined)
        const paths = await tempPaths()
        const { runner } = scriptedRunner(() => ({ reply: 'A box, 40 mm wide.' }))
        const hindsight: CheckedPlugin = {
          plugin: { name: 'hindsight', url: `http://hindsight.invalid:${fake.port}/mcp/bank1/`, toolTiers: {}, disabledTools: [] },
          address: '127.0.0.1',
        }
        const remotePlugins = (): Promise<PluginsForRun> =>
          Promise.resolve({ plugins: [], problems: [], secrets: [], hindsight, terminate: async () => {}, release: () => {} })
        const failures: unknown[] = []
        const audit = new AuditLog({ sql: db.sql, onError: (err) => failures.push(err) })
        const m = manager({ sql: db.sql, paths, run: runner, remotePlugins, audit })
        // The scripted process fires the hooks as Claude Code does (test/support/sessions.ts).
        const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'Make a box' })
        await turn!.done
        await drainRetains()
        await expect.poll(async () => (await audit.list({ sessionId: session.id, kind: 'memory' })).entries.length).toBe(2)
        const { entries } = await audit.list({ sessionId: session.id, kind: 'memory' })
        const recall = entries.find((e) => e.action === 'recall')
        const retain = entries.find((e) => e.action === 'retain')
        expect(recall).toMatchObject({
          surface: 'harness',
          actor: agentA,
          session_id: session.id,
          turn_id: expect.any(String),
          outcome: 'ok',
          input_summary: '{"bank":"bank1","results":2}',
          duration_ms: expect.any(Number),
        })
        expect(retain).toMatchObject({
          outcome: 'ok',
          turn_id: recall!.turn_id,
          input_summary: `{"bank":"bank1","document_id":"conversation:${session.id}"}`,
        })
        expect(JSON.stringify(entries)).not.toMatch(/PETG|Make a box|40 mm/)
        expect(failures).toEqual([])

        const events = (await m.events.read(session.id)).map((e) => e.event)
        const memory = events.filter((e) => e.type === 'memory')
        // The panel event carries what was sent and what came back (shown in Advanced);
        // the audit rows above never do.
        expect(memory).toEqual([
          {
            v: 1,
            type: 'memory',
            sessionId: session.id,
            turnId: recall!.turn_id,
            action: 'recall',
            bank: 'bank1',
            outcome: 'ok',
            count: 2,
            input: 'Make a box',
            memories: [expect.stringMatching(/^1\. /), expect.stringMatching(/^2\. /)],
          },
          {
            v: 1,
            type: 'memory',
            sessionId: session.id,
            turnId: recall!.turn_id,
            action: 'retain',
            bank: 'bank1',
            outcome: 'ok',
            input: expect.stringContaining('40 mm'),
          },
        ])
        // The recall came in the turn; the retain after its last status: the log keeps
        // them in that order for a replay.
        const lastStatus = events.findLastIndex((e) => e.type === 'session.status')
        expect(events.findIndex((e) => e.type === 'memory')).toBeLessThan(lastStatus)
        expect(events.findLastIndex((e) => e.type === 'memory')).toBeGreaterThan(lastStatus)

        // A failed retain is recorded as an error with its reason.
        fake.respond = (r) => (r.path.endsWith('/memories') ? { status: 503, text: 'overloaded' } : undefined)
        await (await m.send(session.id, agentA, 'Again.')).done
        await drainRetains()
        const failed = (await audit.list({ sessionId: session.id, kind: 'memory', outcome: 'error' })).entries
        expect(failed).toMatchObject([{ action: 'retain', detail: expect.stringMatching(/HTTP 503/) }])
      } finally {
        await fake.close()
      }
    })

    it('refuses sends once the session budget is spent, and passes what is left to the SDK', async () => {
      const paths = await tempPaths()
      const settings = new SettingsStore(db.sql)
      await settings.set(SETTING_SESSION_BUDGET_USD, 0.05)
      const { runner, runs } = scriptedRunner(() => ({ reply: 'ok', costUsd: 0.03 }))
      const m = manager({ sql: db.sql, paths, run: runner, settings })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      await (await m.send(session.id, agentA, 'one')).done
      expect(runs[0]!.maxBudgetUsd).toBe(0.05)
      // Second turn, on another replica: a fresh process, resumed from a transcript
      // with no `cost-state` yet (the first replica's process is still warm, and
      // writes its only as it exits), so its total starts from 0 and is the turn's own.
      const { runner: r2, runs: runs2 } = scriptedRunner(() => ({ reply: 'ok', costUsd: 0.03 }))
      const m2 = manager({ sql: db.sql, paths, run: r2, settings })
      await (await m2.send(session.id, agentA, 'two')).done
      expect(runs2[0]!.maxBudgetUsd).toBeCloseTo(0.02)
      expect((await m2.get(session.id, agentA)).costUsd).toBeCloseTo(0.06)
      await expect(m2.send(session.id, agentA, 'three')).rejects.toMatchObject({ code: 'budget_exhausted' })
    })

    it('tells every turn which browser each browser_* set drives', async () => {
      const paths = await tempPaths()
      const { runner, runs } = scriptedRunner(() => ({ reply: 'ok' }))
      const m = manager({ sql: db.sql, paths, run: runner })
      const { turn } = await m.start(agentA, { origin: 'mcp', prompt: 'look' })
      await turn!.done
      // A model called mcp__scadbuddy__browser_find in production, a name only a
      // browser plugin's set (Playwright MCP) has.
      expect(runs[0]!.systemPromptAppend).toContain(BROWSER_TOOLS_GUIDE)
    })

    it('gives a turn the http_request tool unless the setting is off (#827, on by default)', async () => {
      const paths = await tempPaths()
      const settings = new SettingsStore(db.sql)
      const { runner, runs } = scriptedRunner(() => ({ reply: 'ok' }))
      const m = manager({ sql: db.sql, paths, run: runner, settings, httpRequest: {} })
      const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'fetch' })
      await turn!.done
      expect(Object.keys(runs[0]!.mcpServers ?? {})).toContain(HTTP_SERVER)
      // Its tier follows the method: a GET runs, a POST waits for approval.
      expect(runs[0]!.tierOf?.(HTTP_TOOL_NAME, { method: 'GET', url: 'http://x/' })).toBe('read')
      expect(runs[0]!.tierOf?.(HTTP_TOOL_NAME, { method: 'POST', url: 'http://x/' })).toBe('outward')

      await settings.set(SETTING_HTTP_REQUEST, false)
      await (await m.send(session.id, agentA, 'fetch again')).done
      expect(Object.keys(runs[1]!.mcpServers ?? {})).not.toContain(HTTP_SERVER)

      await settings.set(SETTING_HTTP_REQUEST, true)
      await (await m.send(session.id, agentA, 'and again')).done
      expect(Object.keys(runs[2]!.mcpServers ?? {})).toContain(HTTP_SERVER)
    })

    describe('ownership and handoff', () => {
      it('lets only the owner send; others get forbidden or, if they cannot see it, not_found', async () => {
        const paths = await tempPaths()
        const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
        const m = manager({ sql: db.sql, paths, run: runner })
        const { session } = await m.start(agentA, { origin: 'mcp' })
        await expect(m.send(session.id, agentB, 'hi')).rejects.toMatchObject({ code: 'not_found' })
        await expect(m.send(session.id, browser, 'hi')).rejects.toMatchObject({
          code: 'forbidden',
          message: expect.stringMatching(/controlled by Agent A/),
        })
        await expect(m.get(session.id, agentB)).rejects.toMatchObject({ code: 'not_found' })
        expect((await m.get(session.id, browser)).owner).toEqual(agentA)
      })

      it('lists what each principal may see: the browser everything, an agent its own', async () => {
        const paths = await tempPaths()
        const m = manager({ sql: db.sql, paths, run: scriptedRunner(() => ({ reply: 'ok' })).runner })
        const a = (await m.start(agentA, { origin: 'mcp', title: 'a' })).session
        const b = (await m.start(agentB, { origin: 'mcp', title: 'b' })).session
        const c = (await m.start(browser, { origin: 'chat', title: 'c' })).session
        expect((await m.list(agentA)).map((s) => s.id)).toEqual([a.id])
        expect((await m.list(browser)).map((s) => s.id).sort()).toEqual([a.id, b.id, c.id].sort())
        expect((await m.list(browser, { origin: 'chat' })).map((s) => s.id)).toEqual([c.id])
        const snapshot = await m.snapshot(agentB)
        expect(snapshot).toEqual({
          v: 1,
          type: 'sessions.snapshot',
          sessions: [
            {
              sessionId: b.id,
              title: 'b',
              origin: 'mcp',
              owner: agentB,
              status: 'idle',
              // #795: the switcher's nesting, last activity and spend.
              parentId: null,
              updatedAt: expect.any(String),
              costUsd: 0,
              budgetUsd: 1,
              // Plan 5d: the default (durable) ran classic, with no Temporal here.
              mode: 'classic',
              offer: null,
            },
          ],
        })
        // #1284: an offer's target is named only as the viewer may see it.
        await m.handoff(b.id, agentB, agentA)
        const offered = (await m.snapshot(agentB)) as { sessions: { offer: unknown }[] }
        expect(offered.sessions[0]!.offer).toEqual({ to: { kind: 'bearer', label: 'another MCP token' }, until: expect.any(String) })
      })

      it('moves ownership only explicitly: owner to the browser or by accepted offer, browser takes over, nobody else', async () => {
        const paths = await tempPaths()
        const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
        const m = manager({ sql: db.sql, paths, run: runner })
        const { session } = await m.start(agentA, { origin: 'mcp' })

        // The browser takes over (panel `session.handoff`).
        expect((await m.handoff(session.id, browser, browser)).owner).toEqual(browser)
        await expect(m.send(session.id, agentA, 'still mine?')).rejects.toMatchObject({ code: 'forbidden' })
        expect(await (await m.send(session.id, browser, 'mine now')).done).toMatchObject({ kind: 'result' })

        // A non-owner agent cannot grab it, even though it created it.
        await expect(m.handoff(session.id, agentA, agentA)).rejects.toMatchObject({ code: 'forbidden' })
        // The creator still sees it after handing it off.
        expect((await m.list(agentA)).map((s) => s.id)).toEqual([session.id])

        // The owner offers it to another agent, which must accept before it can send.
        expect((await m.handoff(session.id, browser, agentB)).owner).toEqual(browser)
        await expect(m.send(session.id, agentB, 'not yet')).rejects.toMatchObject({ code: 'forbidden' })
        await m.handoff(session.id, agentB, agentB)
        expect(await (await m.send(session.id, agentB, 'agent b here')).done).toMatchObject({ kind: 'result' })
        await expect(m.send(session.id, browser, 'x')).rejects.toMatchObject({ code: 'forbidden' })

        const owners = (await m.events.read(session.id))
          .map((e) => e.event)
          .filter((e) => e.type === 'session.owner')
          .map((e) => (e.type === 'session.owner' ? e.owner.id : ''))
        expect(owners).toEqual(['browser', 'token:b'])
      })

      it('lets a handoff during a running turn apply to the next send', async () => {
        const paths = await tempPaths()
        const { runner } = scriptedRunner(() => ({ hang: true }))
        const m = manager({ sql: db.sql, paths, run: runner })
        const { session } = await m.start(agentA, { origin: 'mcp' })
        const turn = await m.send(session.id, agentA, 'working')
        await m.handoff(session.id, browser, browser)
        await expect(m.send(session.id, browser, 'x')).rejects.toMatchObject({ code: 'busy' })
        await m.interrupt(session.id, browser)
        await turn.done
        await expect(m.send(session.id, agentA, 'x')).rejects.toMatchObject({ code: 'forbidden' })
      })
    })

    describe('attach', () => {
      it('replays exactly what a live watcher saw, then follows new events', async () => {
        const paths = await tempPaths()
        const { runner } = scriptedRunner(() => ({ reply: 'Hello there' }))
        const m = manager({ sql: db.sql, paths, run: runner })
        const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })

        // A live watcher from before the first turn.
        const live = collectUntil(await m.attach(session.id, browser, { signal: stop.signal }), (e) =>
          e.event.type === 'session.status' && e.event.status === 'idle' && e.seq > 2,
        )
        await (await m.send(session.id, agentA, 'hi')).done
        const seen = await live

        // A late watcher, on another replica, replays the same sequence.
        const other = manager({ sql: replica().sql, paths, run: runner })
        const replay = await collectUntil(await other.attach(session.id, browser, { signal: stop.signal }), (e) => e.seq === seen.at(-1)!.seq)
        expect(replay).toEqual(seen)
        expect(seen.map((e) => e.event.type)).toEqual([
          'session.started',
          'session.status',
          'user.turn',
          'session.status',
          'assistant.text.delta',
          'assistant.text.done',
          'session.result',
          'session.status',
        ])
        expect(seen.map((e) => e.seq)).toEqual(seen.map((_, i) => i + 1))

        // Then it goes live: a turn run by the first replica reaches the second's follower.
        const more = collectUntil(
          await other.attach(session.id, browser, { afterSeq: seen.at(-1)!.seq, signal: stop.signal }),
          (e) => e.event.type === 'session.status' && e.event.status === 'idle',
        )
        await (await m.send(session.id, agentA, 'more')).done
        expect((await more).map((e) => e.event.type)).toEqual([
          'user.turn',
          'session.status',
          'assistant.text.delta',
          'assistant.text.done',
          'session.result',
          'session.status',
        ])
      })

      it('is refused to a principal that cannot see the session', async () => {
        const paths = await tempPaths()
        const m = manager({ sql: db.sql, paths, run: scriptedRunner(() => ({ reply: 'x' })).runner })
        const { session } = await m.start(agentA, { origin: 'mcp' })
        await expect(m.attach(session.id, agentB)).rejects.toMatchObject({ code: 'not_found' })
      })
    })

    // The session's warm Claude Code (spec 2026-10-13 §5.3, sessions/warm.ts,
    // sessions/warmTurn.ts) over the scripted process. The real one is
    // test/sessions.e2e.test.ts ("two sends in a row use one Claude Code process").
    describe('the warm process', () => {
      it('serves turn after turn from one process: each prompt into it, each turn its own share of the total', async () => {
        const paths = await tempPaths()
        const totals = [0.02, 0.05]
        const { runner, runs, prompts } = scriptedRunner(() => ({ reply: 'ok', costUsd: totals.shift() }))
        const m = manager({ sql: db.sql, paths, run: runner })
        const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'one' })
        expect(await turn!.done).toMatchObject({ kind: 'result', costUsd: 0.02 })
        const second = await (await m.send(session.id, agentA, 'two')).done
        expect(second).toMatchObject({ kind: 'result', turns: 2 })
        if (second.kind !== 'result') throw new Error('unreachable')
        expect(second.costUsd).toBeCloseTo(0.05, 10)
        expect(runs).toHaveLength(1)
        expect(prompts.map(promptText)).toEqual(['one', 'two'])
        expect(m.warm.size()).toBe(1)
        await m.warm.close()
        expect(m.warm.size()).toBe(0)
      })

      it('an interrupt stops the turn and keeps the process for the next', async () => {
        const paths = await tempPaths()
        const turns: FakeTurn[] = [{ hang: true }, { reply: 'ok' }]
        const { runner, runs } = scriptedRunner(() => turns.shift()!)
        const m = manager({ sql: db.sql, paths, run: runner })
        const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'go' })
        await new Promise((r) => setTimeout(r, 50))
        expect(await m.interrupt(session.id, agentA)).toBe(true)
        expect(await turn!.done).toEqual({ kind: 'interrupted' })
        expect(m.warm.size()).toBe(1)
        expect(await (await m.send(session.id, agentA, 'again')).done).toMatchObject({ kind: 'result', subtype: 'success' })
        expect(runs).toHaveLength(1)
        await m.warm.close()
      })

      it('a process that dies mid-turn is let go: the turn fails, and the next one spawns anew', async () => {
        const paths = await tempPaths()
        const turns: FakeTurn[] = [{ reply: 'ok' }, { stall: { model: 'claude-sonnet-4-5', usage: {} }, dies: 'Claude Code process exited with code 1' }, { reply: 'ok' }]
        const { runner, runs } = scriptedRunner(() => turns.shift()!)
        const m = manager({ sql: db.sql, paths, run: runner })
        const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'one' })
        await turn!.done
        expect(await (await m.send(session.id, agentA, 'two')).done).toMatchObject({ kind: 'failed', message: expect.stringMatching(/exited with code 1/) })
        expect(m.warm.size()).toBe(0)
        expect(await (await m.send(session.id, agentA, 'three')).done).toMatchObject({ kind: 'result', subtype: 'success' })
        expect(runs).toHaveLength(2)
        await m.warm.close()
      })

      it('a warm process gone before its first message is tried once more on a fresh one', async () => {
        const paths = await tempPaths()
        // The second process's first prompt finds it dead (it throws at once); the third serves the turn.
        const turns: FakeTurn[] = [{ reply: 'ok' }, { throws: 'Claude Code process exited with code 1' }, { reply: 'ok' }]
        const { runner, runs } = scriptedRunner(() => turns.shift()!)
        const m = manager({ sql: db.sql, paths, run: runner })
        const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'one' })
        await turn!.done
        expect(await (await m.send(session.id, agentA, 'two')).done).toMatchObject({ kind: 'result', subtype: 'success' })
        expect(runs).toHaveLength(2)
        await m.warm.close()
      })

      // Ruling 9: the manager's claim keeps two turns off one session, so an
      // entry held by anything else when a turn acquires is a bug, surfaced as
      // the turn's failure, never swallowed.
      it('a turn whose process is held fails, saying so, and leaves the held process alone', async () => {
        const paths = await tempPaths()
        const { runner, runs } = scriptedRunner(() => ({ reply: 'ok' }))
        const m = manager({ sql: db.sql, paths, run: runner })
        const h = await warmHarness({ db, url, schema, drop: () => Promise.resolve() })
        try {
          const { session } = await m.start(agentA, { origin: 'mcp' })
          const held = await m.warm.acquire(session.id, h.assembled(), h.credential(), 1)
          expect(held.outcome).toBe('miss')
          expect(await (await m.send(session.id, agentA, 'hi')).done).toEqual({
            kind: 'failed',
            message: `session ${session.id} already has a turn running on its warm process`,
          })
          // The held process is as it was: still the cache's one entry, its inbox open, nothing pushed into it.
          expect(m.warm.size()).toBe(1)
          expect(runs).toHaveLength(1)
          expect(() => held.entry.inbox.push(userMessage('still open'))).not.toThrow()
          expect(await m.get(session.id, agentA)).toMatchObject({ status: 'failed' })
          await m.warm.abandon(held.entry, 'the test')
          expect(m.warm.size()).toBe(0)
        } finally {
          await h.close()
        }
      })

      it('a changed setting spawns a new process for the next turn', async () => {
        const paths = await tempPaths()
        const settings = new SettingsStore(db.sql)
        const { runner, runs } = scriptedRunner(() => ({ reply: 'ok' }))
        const m = manager({ sql: db.sql, paths, run: runner, settings })
        const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'one' })
        await turn!.done
        await settings.set('model', 'claude-opus-5')
        await (await m.send(session.id, agentA, 'two')).done
        expect(runs.map((r) => r.model)).toEqual([undefined, 'claude-opus-5'])
        expect(m.warm.size()).toBe(1)
        await m.warm.close()
      })
    })
  },
)
