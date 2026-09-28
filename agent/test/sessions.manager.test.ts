import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { connectDatabase, type Database } from '../src/db.js'
import { SettingsStore } from '../src/credentials.js'
import { DEFAULT_MAX_BUDGET_USD, DEFAULT_MAX_TURNS } from '../src/harness/run.js'
import { sessionWorkDir } from '../src/harness/stateDirs.js'
import {
  listQuery,
  SessionError,
  SETTING_SESSION_BUDGET_USD,
  SETTING_SESSION_MAX_TURNS,
  TITLE_MAX,
  titleFrom,
  type TurnOutcome,
} from '../src/sessions/manager.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, agentB, browser, collectUntil, type FakeTurn, manager, scriptedRunner, tempPaths } from './support/sessions.js'

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
    let drop: () => Promise<void>
    let stop: AbortController
    const others: Database[] = []

    beforeEach(async () => {
      ;({ db, schema, drop } = await throwawayDatabase())
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
      const other = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
      others.push(other)
      return other
    }

    it('serves a principal’s list from the owner and creator indexes, not a table scan', async () => {
      const indexes = await db.sql<{ indexname: string; indexdef: string }[]>`
        SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'ai_sessions'`
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
      // mark it written to see the resume path.
      await m.store.append({ projectKey: 'p', sessionId: session.id }, [{ type: 'user', uuid: 'x' }])
      await (await m.send(session.id, agentA, 'again')).done
      expect(runs.map((r) => [r.sessionId, r.resume])).toEqual([
        [session.id, undefined],
        [undefined, session.id],
      ])
      expect(runs[0]!.cwd).toBe(sessionWorkDir(paths, session.id))
      expect(runs[0]!.sessionStore).toBe(m.store)
      expect(runs[0]!.includePartialMessages).toBe(true)
      expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle', turnActive: false, turns: 2 })
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
      const started = Date.now()
      await (await m.send(id, agentA, 'again')).done
      expect((await gotA).at(0)?.event.type).toBe('user.turn')
      expect((await gotB).at(0)?.event.type).toBe('user.turn')
      expect(Date.now() - started).toBeLessThan(5_000)
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

    it('refuses sends once the session budget is spent, and passes what is left to the SDK', async () => {
      const paths = await tempPaths()
      const settings = new SettingsStore(db.sql)
      await settings.set(SETTING_SESSION_BUDGET_USD, 0.05)
      const { runner, runs } = scriptedRunner(() => ({ reply: 'ok', costUsd: 0.03 }))
      const m = manager({ sql: db.sql, paths, run: runner, settings })
      const { session } = await m.start(agentA, { origin: 'mcp' })
      await (await m.send(session.id, agentA, 'one')).done
      expect(runs[0]!.maxBudgetUsd).toBe(0.05)
      // Second turn: the SDK reports the cumulative total (see manager.ts).
      const { runner: r2, runs: runs2 } = scriptedRunner(() => ({ reply: 'ok', costUsd: 0.06 }))
      const m2 = manager({ sql: db.sql, paths, run: r2, settings })
      await (await m2.send(session.id, agentA, 'two')).done
      expect(runs2[0]!.maxBudgetUsd).toBeCloseTo(0.02)
      expect((await m2.get(session.id, agentA)).costUsd).toBeCloseTo(0.06)
      await expect(m2.send(session.id, agentA, 'three')).rejects.toMatchObject({ code: 'budget_exhausted' })
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
          sessions: [{ sessionId: b.id, title: 'b', origin: 'mcp', owner: agentB, status: 'idle' }],
        })
      })

      it('moves ownership only explicitly: owner to anyone, browser takes over, nobody else', async () => {
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

        // The owner hands it to another agent, which can then send.
        await m.handoff(session.id, browser, agentB)
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
  },
)
