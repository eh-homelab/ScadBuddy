import { execFileSync, spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import type { Sql } from 'postgres'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { CrudEditRows, type EditRows, PgEditRows } from '../src/sessions/edits.js'
import { SessionError } from '../src/sessions/manager.js'
import type { ServerEvent } from '../src/sessions/protocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// The API's port of an edit of a session (backend scadbuddy/agent_data/edits.py behind
// api/crud_agent_edits.py, #6449) against the agent's own (sessions/edits.ts `PgEditRows`): one
// scenario of renames, dones, archives and refusals, run on two databases, one each way, answers
// the same (the same SessionError for each refusal), tells the same workflows they are done,
// leaves the same rows and logs the same events. A refusal writes nothing either way.

const id = (n: number) => `${n}${n}${n}${n}${n}${n}${n}${n}-1111-4111-8111-111111111111`
const [S1, S2, S3, S4, S5, S6] = [1, 2, 3, 4, 5, 6].map(id) as [string, string, string, string, string, string]
const MISSING = id(9)
const Q = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const BROWSER = { kind: 'browser' as const, id: 'browser' }

const hasUv = (): boolean => {
  try {
    execFileSync('uv', ['--version'], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

/** The scenario's answers in order, a refusal as its code and message, and the sessions told they are done. */
async function scenario(rows: EditRows): Promise<{ out: unknown[]; told: string[] }> {
  const told: string[] = []
  const tell = async (sid: string) => {
    if (sid === S6) throw new SessionError('unavailable', 'no Temporal')
    told.push(sid)
  }
  const out: unknown[] = []
  const run = async (sid: string, write: Parameters<EditRows['edit']>[1]) => {
    try {
      const edited = await rows.edit(sid, write, tell)
      out.push({ logged: edited.logged ?? null, dismissed: edited.dismissed.map((d) => ({ ...d, created_at: d.created_at instanceof Date })) })
    } catch (err) {
      out.push(err instanceof SessionError ? { code: err.code, message: err.message } : String(err))
    }
  }
  await run(S1, { principal: BROWSER, title: 'renamed "é"', done: false })
  await run(S1, { principal: BROWSER, done: true })
  await run(S1, { principal: BROWSER, done: true })
  await run(S2, { principal: BROWSER, done: true })
  await run(S2, { principal: BROWSER, done: false, archived: true })
  await run(S3, { principal: BROWSER, done: false, archived: true })
  await run(S3, { principal: BROWSER, done: false, archived: false })
  await run(S4, { principal: BROWSER, done: false, archived: true })
  // Done and archived at once on an idle session with a call parked: refused `parked`, and no
  // workflow is told either way.
  await run(S4, { principal: BROWSER, done: true, archived: true })
  await run(S5, { principal: { kind: 'bearer', id: 'not-the-owner' }, title: 'x', done: false })
  await run(MISSING, { principal: BROWSER, title: 'x', done: false })
  await run(S6, { principal: BROWSER, done: true })
  return { out, told }
}

async function seed(sql: Sql): Promise<void> {
  for (const [sid, kind, owner, status] of [
    [S1, 'browser', 'browser', 'idle'],
    [S2, 'browser', 'browser', 'running'],
    [S3, 'browser', 'browser', 'idle'],
    [S4, 'browser', 'browser', 'idle'],
    [S5, 'bearer', 'a', 'idle'],
    [S6, 'browser', 'browser', 'idle'],
  ] as const) {
    await sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
      VALUES (${sid}, 'chat', ${kind}, ${owner}, 'You', ${kind}, ${owner}, ${status}, 10, 1)`
  }
  await sql`
    INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, summary)
    VALUES (${Q}, ${S3}, ${Q}, 'attn', 'toolu_done', '[]', 'attention', 'done', 's')`
  await sql`
    INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, expires_at)
    VALUES (gen_random_uuid(), ${S4}, gen_random_uuid(), 'AskUserQuestion', 'toolu_q', '[]', now() + interval '10 minutes')`
}

async function left(sql: Sql): Promise<unknown> {
  const rows = await sql`SELECT id::text, title, status, archived_at IS NOT NULL AS archived, event_seq FROM ai_sessions ORDER BY id`
  const questions = await sql`SELECT session_id::text, outcome, reason FROM ai_questions ORDER BY session_id`
  const events = await sql<{ session_id: string; seq: string; event: string }[]>`
    SELECT session_id::text, seq, event FROM ai_session_events ORDER BY session_id, seq`
  return { rows, questions, events: events.map((e) => ({ ...e, event: JSON.parse(e.event) as ServerEvent })) }
}

describe.skipIf(!TEST_DATABASE_URL || !hasUv())(
  `an edit of a session on the API${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
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

    it('answers, refuses and writes as the agent does', async () => {
      await seed(viaSql.db.sql)
      await seed(viaApi.db.sql)
      const script = new URL('./support/turn_rows_parity.py', import.meta.url).pathname
      const cwd = new URL('../../backend/', import.meta.url).pathname
      const py = spawn('uv', ['run', '--frozen', 'python', script, viaApi.url, viaApi.schema], { cwd, stdio: ['pipe', 'pipe', 'inherit'] })
      const lines = createInterface({ input: py.stdout })[Symbol.asyncIterator]()
      const crud = new CrudEditRows('http://api:8080', {
        fetch: async (input, init) => {
          const request = new Request(input, init)
          const text = await request.text()
          py.stdin.write(`${JSON.stringify({ method: request.method, url: request.url, body: text ? JSON.parse(text) : null })}\n`)
          const { value } = await lines.next()
          const answer = JSON.parse(value as string) as { status: number; body: unknown }
          return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { 'x-scadbuddy-crud': '1' } })
        },
      })
      let api: Awaited<ReturnType<typeof scenario>>
      try {
        api = await scenario(crud)
      } finally {
        py.stdin.end()
      }
      const sql = await scenario(new PgEditRows(viaSql.db.sql))
      expect(api).toEqual(sql)
      expect(sql.told).toEqual([S1])
      expect(await left(viaApi.db.sql)).toEqual(await left(viaSql.db.sql))
    })
  },
)
