import { execFileSync, spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { CrudEventRows } from '../src/sessions/crudEvents.js'
import { type EventRows, PgEventRows } from '../src/sessions/eventLog.js'
import type { ServerEvent } from '../src/sessions/protocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// The API's port of the event log's reads (backend scadbuddy/agent_data/events.py, served by
// api/crud_agent_events.py, #5299 step 9 part b) against the agent's own (sessions/eventLog.ts
// `PgEventRows`): on one database, a parent and a fork read both ways give the same events, field
// by field, once `CrudEventRows` has parsed the API's answers. During a roll a follower reads one
// session one way and then the other, so the two must not differ.

const PARENT = '11111111-1111-4111-8111-111111111111'
const FORK = '22222222-2222-4222-8222-222222222222'
const MISSING = '33333333-3333-4333-8333-333333333333'

const hasUv = (): boolean => {
  try {
    execFileSync('uv', ['--version'], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

const ev = (e: Record<string, unknown>): ServerEvent => e as unknown as ServerEvent

/** Every read the event log makes, against `rows`. */
async function reads(rows: EventRows): Promise<unknown> {
  return {
    parent: await rows.read(PARENT, 0, 500),
    fork: await rows.read(FORK, 0, 500),
    forkPage: await rows.read(FORK, 2, 2),
    missing: await rows.read(MISSING, 0, 500),
    lastSeq: [await rows.lastSeq(PARENT), await rows.lastSeq(FORK), await rows.lastSeq(MISSING)],
    userTurn: [await rows.userTurnText(PARENT, 't1'), await rows.userTurnText(PARENT, 'none'), await rows.userTurnText(FORK, 't3')],
    turn: [
      await rows.turnEvents(PARENT, 't2', undefined),
      await rows.turnEvents(PARENT, 't1', '2'),
      await rows.turnEvents(PARENT, 'none', undefined),
      await rows.turnEvents(FORK, 't3', undefined),
      await rows.turnEvents(MISSING, 't1', undefined),
    ],
  }
}

describe.skipIf(!TEST_DATABASE_URL || !hasUv())(
  `the event log's reads on the API${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Throwaway
    beforeEach(async () => {
      db = await throwawayDatabase()
    })
    afterEach(async () => {
      await db.drop()
    })

    it('reads a parent and its fork as the agent does', async () => {
      const sql = db.db.sql
      for (const [id, parent] of [
        [PARENT, null],
        [FORK, PARENT],
      ] as const) {
        await sql`
          INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, fork_parent_id, fork_cut)
          VALUES (${id}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'idle', 10, 1, ${parent}, ${parent === null ? null : 0})`
      }
      const pg = new PgEventRows(sql)
      await pg.append(PARENT, [
        ev({ type: 'session.started', sessionId: PARENT }),
        ev({ type: 'user.turn', sessionId: PARENT, turnId: 't1', text: 'first "é"  ' }),
        ev({ type: 'assistant.text.done', sessionId: PARENT, text: 'one' }),
        ev({ type: 'user.turn', sessionId: PARENT, turnId: 't2', text: 'second' }),
        ev({ type: 'assistant.text.done', sessionId: PARENT, text: 'two' }),
        ev({ type: 'session.status', sessionId: PARENT, status: 'idle' }),
      ])
      await pg.forkLog(FORK, PARENT, 3, [ev({ type: 'session.started', sessionId: FORK })], [ev({ type: 'session.status', sessionId: FORK, status: 'idle' })])
      await pg.append(FORK, [ev({ type: 'user.turn', sessionId: FORK, turnId: 't3', text: 'mine' }), ev({ type: 'assistant.text.done', sessionId: FORK, text: 'three' })])

      // Each URL answered by the API's code on the same database, one process for them all.
      const script = new URL('./support/event_rows_parity.py', import.meta.url).pathname
      const cwd = new URL('../../backend/', import.meta.url).pathname
      const py = spawn('uv', ['run', '--frozen', 'python', script, db.url, db.schema], { cwd, stdio: ['pipe', 'pipe', 'inherit'] })
      const lines = createInterface({ input: py.stdout })[Symbol.asyncIterator]()
      const crud = new CrudEventRows('http://api:8080', {
        fetch: async (input) => {
          py.stdin.write(`${new Request(input).url}\n`)
          const { value } = await lines.next()
          return new Response(value as string, { status: 200, headers: { 'x-scadbuddy-crud': '1' } })
        },
      })
      let viaApi: unknown
      try {
        viaApi = await reads(crud)
      } finally {
        py.stdin.end()
      }
      const viaSql = await reads(pg)
      expect(viaApi).toEqual(viaSql)
      expect((viaSql as { fork: { event: { sessionId: string } }[] }).fork.every((r) => r.event.sessionId === FORK)).toBe(true)
    })
  },
)
