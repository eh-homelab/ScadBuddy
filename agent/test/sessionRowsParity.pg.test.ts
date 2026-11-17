import { execFileSync } from 'node:child_process'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { CrudSessionRows, PgSessionRows } from '../src/sessions/rows.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// The API's copy of the session manager's row (backend scadbuddy/agent_data/sessions.py `COLUMNS`
// and `TURN_COLUMNS`, served by api/crud_agent_sessions.py, #5299 step 9 part a) against the
// agent's own (sessions/rows.ts `PgSessionRows.get`): on one database, each row read both ways is
// the same, field by field, once `CrudSessionRows` has parsed the API's answer. During a roll an
// agent reads a session one way and then the other, so the two column lists must not drift.

const PLAIN = '11111111-1111-4111-8111-111111111111'
const FORK = '22222222-2222-4222-8222-222222222222'
const RUNNING = '33333333-3333-4333-8333-333333333333'
const ARCHIVED = '44444444-4444-4444-8444-444444444444'
const EXPIRED = '55555555-5555-4555-8555-555555555555'
const MISSING = '66666666-6666-4666-8666-666666666666'
const IDS = [PLAIN, FORK, RUNNING, ARCHIVED, EXPIRED, MISSING]

const hasUv = (): boolean => {
  try {
    execFileSync('uv', ['--version'], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

describe.skipIf(!TEST_DATABASE_URL || !hasUv())(
  `the session row on the API${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Throwaway
    beforeEach(async () => {
      db = await throwawayDatabase()
    })
    afterEach(async () => {
      await db.drop()
    })

    it('reads every field of the row as the agent does', async () => {
      const sql = db.db.sql
      for (const id of [PLAIN, FORK, RUNNING, ARCHIVED, EXPIRED]) {
        await sql`
          INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
          VALUES (${id}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'idle', 10, 1.25)`
      }
      await sql`UPDATE ai_sessions SET title = 'a "title" é', tags = ${sql.array(['a', 'b'])}, scope = ${sql.json({ model: 'm' })},
        cost_usd = 0.123456, unpriced_cost_usd = 0.5, turns = 3 WHERE id = ${PLAIN}`
      await sql`UPDATE ai_sessions SET parent_id = ${PLAIN}, fork_parent_id = ${PLAIN}, fork_cut = 0,
        pending_owner_kind = 'token', pending_owner_id = '7', pending_owner_label = 'Bot', pending_owner_until = now() + interval '1 hour'
        WHERE id = ${FORK}`
      await sql`UPDATE ai_sessions SET runtime = 'unified', status = 'running', durable_turn_id = gen_random_uuid(),
        turn_started_at = now(), turn_tiers = ${sql.json({ read: 'auto' })}, agent_model = 'opus', agent_effort = 'high',
        agent_type = 'helper', background_tools = false, turn_flow = true WHERE id = ${RUNNING}`
      await sql`UPDATE ai_sessions SET archived_at = now() WHERE id = ${ARCHIVED}`
      await sql`UPDATE ai_sessions SET pending_owner_kind = 'token', pending_owner_id = '8', pending_owner_label = 'Gone',
        pending_owner_until = now() - interval '1 minute' WHERE id = ${EXPIRED}`

      const script = new URL('./support/session_rows_parity.py', import.meta.url).pathname
      const cwd = new URL('../../backend/', import.meta.url).pathname
      const out = execFileSync('uv', ['run', '--frozen', 'python', script, db.url, db.schema], { cwd, input: JSON.stringify(IDS) })
      const wire = JSON.parse(out.toString()) as Record<string, unknown>
      const fetch: typeof globalThis.fetch = async (input) => {
        const id = new URL(new Request(input).url).pathname.split('/').at(-1) ?? ''
        return new Response(JSON.stringify(wire[id]), { status: 200, headers: { 'x-scadbuddy-crud': '1' } })
      }
      const crud = new CrudSessionRows('http://api:8080', { fetch })
      const pg = new PgSessionRows(sql)
      for (const id of IDS) {
        expect({ id, row: await crud.get(id) }).toEqual({ id, row: await pg.get(id) })
      }
      expect((await pg.get(EXPIRED))?.offer_kind).toBeNull()
    })
  },
)
