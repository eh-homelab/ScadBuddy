import { execFileSync, spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import type { Sql } from 'postgres'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { event, type ServerEvent } from '../src/sessions/protocol.js'
import { CrudTurnRows, type NextClaim, PgTurnRows, type TurnRows } from '../src/sessions/turnRows.js'
import { turnEvents } from '../src/temporal/session/finishActivity.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// The API's port of a durable turn's row writes (backend scadbuddy/agent_data/turns.py behind
// api/crud_agent_turns.py, #6449) against the agent's own (sessions/turnRows.ts `PgTurnRows`): one
// scenario of claims, placements, spend, ends and the sweep's end, run on two databases, one each
// way, answers the same, leaves the same rows and logs the same events. Each fence refuses the
// same stale caller both ways: a placement of a turn the row no longer runs (replaced), a turn's
// end written already (ended) or of a turn another replaced, a lost turn's end once the row moved
// on. During a roll one turn is written one way and then the other, so the two must not differ.

const S1 = '11111111-1111-4111-8111-111111111111'
const S2 = '22222222-2222-4222-8222-222222222222'
const MISSING = '33333333-3333-4333-8333-333333333333'
const T1 = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1'
const T2 = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2'
const H1 = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3'
const H2 = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4'
const T3 = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa5'
const BROWSER = { kind: 'browser' as const, id: 'browser' }

const hasUv = (): boolean => {
  try {
    execFileSync('uv', ['--version'], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

const head = (sessionId: string, turnId: string, author: { kind: 'browser' | 'bearer'; id: string } = BROWSER): NextClaim => ({
  turnId,
  author,
  tiers: ['read'],
  bare: false,
  started: [
    event({ type: 'user.turn', sessionId, turnId, text: 'next "é"', author: { ...author, label: 'You' } }),
    event({ type: 'session.status', sessionId, status: 'running' }),
  ],
})

/** The scenario, its answers in order. */
async function scenario(rows: TurnRows): Promise<unknown[]> {
  const out: unknown[] = []
  const say = async (p: Promise<unknown>) => out.push((await p) ?? null)
  await say(rows.sendClaim(S1, { author: BROWSER, turnId: T1, tiers: ['read'], bare: false }))
  await say(rows.sendClaim(S1, { author: BROWSER, turnId: T2, tiers: null, bare: true }))
  await say(rows.queueable(S1, BROWSER))
  await say(rows.queueable(S1, { kind: 'bearer', id: 'x' }))
  await say(rows.placement(S1, T1, 'run:1', T1))
  // Replaced: the row runs T1, so a placement of T2 takes nothing.
  await say(rows.placement(S1, T2, 'run:2', T2))
  await say(rows.placement(S1, T1, 'run:3', `${T1}/r1`))
  await say(rows.state(S1, 3_600_000))
  await say(rows.spend(S1, { key: 'k:0', costUsd: 0.125, unpricedUsd: 0.125, turns: 0 }))
  await say(rows.spend(S1, { key: 'k:1', costUsd: 0.1, unpricedUsd: -0.025, turns: 2 }))
  await say(rows.spend(MISSING, { key: 'k:0', costUsd: 1, unpricedUsd: 1, turns: 1 }))
  await say(rows.interrupt(S1))
  const tail = turnEvents(S1, { sessionId: S1, turnId: T1, outcome: 'budget_exhausted' }, 0, 0, 0)
  await say(rows.finish(S1, T1, tail, head(S1, H1)))
  // Ended: a retry writes no end, and finds its head started.
  await say(rows.finish(S1, T1, tail, head(S1, H1)))
  await say(rows.begin(S1, head(S1, H2)))
  // Replaced: T1's end never ends H1's turn.
  await say(rows.finish(S1, T1, tail))
  await say(rows.state(S1, 3_600_000))
  await say(rows.queueChanged(S1, event({ type: 'session.queue', sessionId: S1, version: 3, queue: [] })))
  await say(rows.queueChanged(MISSING, event({ type: 'session.queue', sessionId: MISSING, version: 1, queue: [] })))
  await say(rows.lastQueue(S1))
  await say(rows.lastQueue(MISSING))
  const [stale] = await rows.stale('', 0, 10)
  out.push(stale ?? null)
  const lost = [event({ type: 'error', sessionId: S1, code: 'turn_failed', message: 'gone' }), event({ type: 'session.status', sessionId: S1, status: 'idle' })]
  // Moved on since the sweep read it: never ended.
  await say(rows.endLost(S1, String(Number(stale!.event_seq) - 1), lost))
  await say(rows.endLost(S1, stale!.event_seq, lost))
  await say(rows.giveBack(S1, 'failed'))
  await say(rows.sendClaim(S1, { author: BROWSER, turnId: T3, tiers: null, bare: true }))
  await say(rows.giveBack(S1, 'failed'))
  await say(rows.begin(S2, head(S2, H2, { kind: 'bearer', id: 'not-the-owner' })))
  await say(rows.begin(MISSING, head(MISSING, H2)))
  return out
}

async function seed(sql: Sql): Promise<void> {
  for (const [id, kind, owner] of [
    [S1, 'browser', 'browser'],
    [S2, 'bearer', 'b'],
  ] as const) {
    await sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
      VALUES (${id}, 'chat', ${kind}, ${owner}, 'You', ${kind}, ${owner}, 'idle', 10, 2)`
  }
  await sql`INSERT INTO ai_session_entries (project_key, session_id, subpath, entry) VALUES ('', ${S1}, '', '{}')`
}

/** What the scenario left: the rows' turn columns and every event, parsed. */
async function left(sql: Sql): Promise<unknown> {
  const rows = await sql`
    SELECT id::text, status, durable_turn_id::text, turn_attempt, turn_mark, turn_mark_turn, turn_tiers, turn_flow, interrupt_requested,
      runtime, cost_usd, unpriced_cost_usd, turns, event_seq
    FROM ai_sessions ORDER BY id`
  const events = await sql<{ session_id: string; seq: string; event: string }[]>`
    SELECT session_id::text, seq, event FROM ai_session_events ORDER BY session_id, seq`
  return { rows, events: events.map((e) => ({ ...e, event: JSON.parse(e.event) as ServerEvent })) }
}

describe.skipIf(!TEST_DATABASE_URL || !hasUv())(
  `a durable turn's row writes on the API${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let viaSql: Throwaway
    let viaApi: Throwaway
    beforeEach(async () => {
      viaSql = await throwawayDatabase()
      viaApi = await throwawayDatabase()
    })
    afterEach(async () => {
      await viaSql.drop()
      await viaApi.drop()
    })

    it('answers, writes and fences as the agent does', async () => {
      await seed(viaSql.db.sql)
      await seed(viaApi.db.sql)
      const script = new URL('./support/turn_rows_parity.py', import.meta.url).pathname
      const cwd = new URL('../../backend/', import.meta.url).pathname
      const py = spawn('uv', ['run', '--frozen', 'python', script, viaApi.url, viaApi.schema], { cwd, stdio: ['pipe', 'pipe', 'inherit'] })
      const lines = createInterface({ input: py.stdout })[Symbol.asyncIterator]()
      const crud = new CrudTurnRows('http://api:8080', {
        fetch: async (input, init) => {
          const request = new Request(input, init)
          const text = request.method === 'POST' ? await request.text() : ''
          py.stdin.write(`${JSON.stringify({ method: request.method, url: request.url, body: text ? JSON.parse(text) : null })}\n`)
          const { value } = await lines.next()
          const answer = JSON.parse(value as string) as { status: number; body: unknown }
          return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { 'x-scadbuddy-crud': '1' } })
        },
      })
      let api: unknown[]
      try {
        api = await scenario(crud)
      } finally {
        py.stdin.end()
      }
      const sql = await scenario(new PgTurnRows(viaSql.db.sql))
      expect(api).toEqual(sql)
      expect(await left(viaApi.db.sql)).toEqual(await left(viaSql.db.sql))
      // The fences held: the replaced placement and the stale ends wrote nothing.
      expect(sql[5]).toBeNull()
      expect((sql[15] as { ended?: unknown }).ended).toBeUndefined()
      expect(sql[22]).toBeNull()
    })
  },
)
