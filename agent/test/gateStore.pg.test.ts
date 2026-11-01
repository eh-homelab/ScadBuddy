import { responseHash } from '../src/gate/canonical.js'
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import postgres, { type Sql } from 'postgres'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { durableRequestId } from '../src/gate/ids.js'
import {
  decideInput,
  denyInput,
  grantInput,
  INPUT_CHANNEL,
  openInput,
  readInput,
  resolveInput,
  type OpenInput,
  type Responder,
} from '../src/gate/store.js'
import type { Owner, ServerEvent } from '../src/sessions/protocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// The gate's projection writes (durable-agents spec §6.6) against the agent's own
// tables: a port, case for case, of agent-durable/tests/test_gate_store.py, plus the
// `scadbuddy_input` NOTIFY, the grant and deny paths, and a parity check against
// store.py itself.

const ME: Owner = { kind: 'browser', id: 'browser', label: 'You' }
const SYSTEM: Responder = { kind: 'system', id: 'scadbuddy', label: 'ScadBuddy' }
const ME_RESPONDER: Responder = ME

async function newSession(sql: Sql, id: string = randomUUID()): Promise<string> {
  await sql`
    INSERT INTO ai_sessions
      (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,
       status, max_turns, budget_usd, mode)
    VALUES (${id}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser',
            'running', 10, 1, 'durable')`
  return id
}

function open(sid: string, run = 'run-1', kind: 'approval' | 'answer' = 'approval', toolUse = 'toolu_1'): OpenInput {
  const expiresAt = new Date(Date.now() + 10 * 60_000).toISOString()
  const common = {
    requestId: durableRequestId(sid, run, toolUse),
    sessionId: sid,
    workflowId: `session-${sid}`,
    workflowRunId: run,
    toolUseId: toolUse,
    requestedBy: ME,
    expiresAt,
  }
  if (kind === 'approval') {
    return {
      ...common,
      kind: 'approval',
      tool: 'print_output',
      summary: '{"output":"box"}',
      inputHash: 'a'.repeat(64),
      prompt: '',
      responders: ['browser', 'grant'],
    }
  }
  return {
    ...common,
    kind: 'answer',
    tool: 'ask_user',
    summary: '',
    inputHash: null,
    prompt: 'Which colour?',
    responders: ['browser'],
    questions: [{ question: 'Which colour?', header: 'Colour', multiSelect: false, options: [] }],
  }
}

async function events(sql: Sql, sid: string): Promise<ServerEvent[]> {
  const rows = await sql<{ event: string }[]>`SELECT event FROM ai_session_events WHERE session_id = ${sid} ORDER BY seq`
  return rows.map((r) => JSON.parse(r.event) as ServerEvent)
}

async function count(sql: Sql, table: 'ai_pending_input' | 'ai_input_responses' | 'ai_audit'): Promise<number> {
  const [row] = await sql<{ n: string }[]>`SELECT count(*) AS n FROM ${sql(table)}`
  return Number(row?.n)
}

async function statusOf(sql: Sql, sid: string): Promise<string | undefined> {
  const [row] = await sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${sid}`
  return row?.status
}

describe.skipIf(!TEST_DATABASE_URL)(
  `gate store${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let t: Throwaway
    let sql: Sql

    beforeEach(async () => {
      t = await throwawayDatabase()
      sql = t.db.sql
    })
    afterEach(async () => {
      await t.drop()
    })

    it("keeps the arguments' full room in approval.required beside a resolving tool's line (#4442 N1)", async () => {
      const sid = await newSession(sql)
      const line = 'L'.repeat(200)
      await openInput(sql, { ...open(sid), summary: `${line}\n${'a'.repeat(1000)}` } as OpenInput)
      const [required] = await events(sql, sid)
      const summary = (required as { summary: string }).summary
      expect(summary.startsWith(`print_output ${line}\n`)).toBe(true)
      expect(summary.split('\n')[1]!.length).toBe(500 - 'print_output '.length)
      expect(summary.endsWith('…')).toBe(true)
    })

    it('open writes the row and its events once', async () => {
      const sid = await newSession(sql)
      const args = open(sid)
      expect(await openInput(sql, args)).toBe(true)
      expect(await openInput(sql, args)).toBe(false) // a retried activity adds nothing
      expect(await count(sql, 'ai_pending_input')).toBe(1)
      const logged = await events(sql, sid)
      expect(logged.map((e) => e.type)).toEqual(['approval.required', 'input.requested', 'session.status'])
      expect(logged[0]).toMatchObject({ id: args.requestId })
      const entry = (logged[1] as Extract<ServerEvent, { type: 'input.requested' }>).entry
      expect(entry.id).toBe(args.requestId)
      expect(entry.input_hash).toBe('a'.repeat(64))
      expect(entry).not.toHaveProperty('input')
      expect(await statusOf(sql, sid)).toBe('waiting_approval')
    })

    it('resolve records the outcome, events and audit', async () => {
      const sid = await newSession(sql)
      const args = open(sid)
      await openInput(sql, args)
      expect(await resolveInput(sql, { requestId: args.requestId, outcome: 'approved', responder: ME_RESPONDER })).toBe(true)
      expect(await count(sql, 'ai_pending_input')).toBe(0)
      const responses = await sql`SELECT outcome, responder FROM ai_input_responses`
      expect(responses.map((r) => ({ ...r }))).toEqual([{ outcome: 'approved', responder: ME }])
      const types = (await events(sql, sid)).map((e) => e.type)
      expect(types.slice(-3)).toEqual(['approval.resolved', 'input.resolved', 'session.status'])
      const audit = await sql`SELECT kind, action, principal_kind, request_id, approval_id, outcome FROM ai_audit`
      expect(audit.map((r) => ({ ...r }))).toEqual([
        { kind: 'approval', action: 'approved', principal_kind: 'browser', request_id: args.requestId, approval_id: null, outcome: 'ok' },
      ])
      expect(await statusOf(sql, sid)).toBe('running')
    })

    it("audits started_at at the entry's own microsecond", async () => {
      const sid = await newSession(sql)
      const args = open(sid)
      await openInput(sql, args)
      const [pending] = await sql<{ created: string }[]>`SELECT created_at::text AS created FROM ai_pending_input`
      await resolveInput(sql, { requestId: args.requestId, outcome: 'approved', responder: ME_RESPONDER })
      const [audited] = await sql<{ started: string }[]>`SELECT started_at::text AS started FROM ai_audit`
      expect(audited?.started).toBe(pending?.created)
    })

    it('an open retried after its resolution opens nothing', async () => {
      const sid = await newSession(sql)
      const args = open(sid)
      await openInput(sql, args)
      await resolveInput(sql, { requestId: args.requestId, outcome: 'denied', responder: ME_RESPONDER })
      expect(await openInput(sql, args)).toBe(false)
      expect(await count(sql, 'ai_pending_input')).toBe(0)
    })

    it('an expiry is recorded as expired by the system', async () => {
      const sid = await newSession(sql)
      const args = open(sid)
      await openInput(sql, args)
      await resolveInput(sql, { requestId: args.requestId, outcome: 'expired', responder: SYSTEM, reason: 'timeout' })
      const audit = await sql`SELECT surface, principal_kind, action, outcome FROM ai_audit`
      expect(audit.map((r) => ({ ...r }))).toEqual([
        { surface: 'system', principal_kind: 'system', action: 'expired', outcome: 'refused' },
      ])
      const resolved = (await events(sql, sid)).filter((e) => e.type === 'approval.resolved')
      expect(resolved[0]).toMatchObject({ decision: 'expired' })
      expect(resolved[0]).not.toHaveProperty('by')
    })

    it('an answer keeps its response', async () => {
      const sid = await newSession(sql)
      const args = open(sid, 'run-1', 'answer')
      await openInput(sql, args)
      expect((await events(sql, sid))[0]?.type).toBe('question.asked')
      const response = { answers: ['Red'] }
      await resolveInput(sql, { requestId: args.requestId, outcome: 'answered', responder: ME_RESPONDER, response })
      const rows = await sql`SELECT response, response_hash FROM ai_input_responses`
      expect(rows.map((r) => ({ ...r }))).toEqual([{ response, response_hash: null }])
      expect(await count(sql, 'ai_audit')).toBe(0)
    })

    it('keeps the hash of the raw response, key order aside (#3724)', async () => {
      const sid = await newSession(sql)
      const args = open(sid, 'run-1', 'answer')
      await openInput(sql, args)
      await resolveInput(sql, {
        requestId: args.requestId,
        outcome: 'answered',
        responder: ME_RESPONDER,
        response: { answers: ['Red'] },
        rawResponse: { kind: 'answer', choice: 'Red' },
      })
      const [row] = await sql`SELECT response_hash FROM ai_input_responses`
      expect(row?.response_hash).toBe(responseHash({ choice: 'Red', kind: 'answer' }))
    })

    it('two resolutions race and one wins', async () => {
      const sid = await newSession(sql)
      const args = open(sid)
      await openInput(sql, args)
      const results = await Promise.all([
        resolveInput(sql, { requestId: args.requestId, outcome: 'approved', responder: ME_RESPONDER }),
        resolveInput(sql, { requestId: args.requestId, outcome: 'denied', responder: ME_RESPONDER }),
      ])
      expect([...results].sort()).toEqual([false, true])
      expect((await events(sql, sid)).filter((e) => e.type === 'input.resolved')).toHaveLength(1)
      expect(await count(sql, 'ai_input_responses')).toBe(1)
    })

    it("after a reset the old run's entry is cancelled", async () => {
      const sid = await newSession(sql)
      const before = open(sid, 'run-1')
      const after = open(sid, 'run-2')
      await openInput(sql, before)
      await openInput(sql, after)
      const pending = await sql`SELECT request_id FROM ai_pending_input`
      expect(pending.map((r) => ({ ...r }))).toEqual([{ request_id: after.requestId }])
      const responses = await sql`SELECT request_id, outcome, reason FROM ai_input_responses`
      expect(responses.map((r) => ({ ...r }))).toEqual([{ request_id: before.requestId, outcome: 'cancelled', reason: 'reset' }])
      // A respond carrying the pre-Reset id finds nothing.
      expect(await resolveInput(sql, { requestId: before.requestId, outcome: 'approved', responder: ME_RESPONDER })).toBe(false)
    })

    it('runs over a connection of its own (the activities case)', async () => {
      // Python's case wraps the two calls in GateActivities; here that is a second
      // connection to the same database, which is all the wrapper adds.
      const other = postgres(t.url, { max: 1, onnotice: () => {} })
      try {
        const sid = await newSession(sql)
        const args = open(sid)
        expect(await openInput(other, args)).toBe(true)
        expect(await resolveInput(other, { requestId: args.requestId, outcome: 'denied', responder: ME_RESPONDER })).toBe(true)
      } finally {
        await other.end({ timeout: 5 })
      }
    })

    it('a resolve sends NOTIFY scadbuddy_input with the request id, on commit', async () => {
      const listener = postgres(t.url, { max: 1, onnotice: () => {} })
      try {
        const heard: string[] = []
        await listener.listen(INPUT_CHANNEL, (payload) => heard.push(payload))
        const sid = await newSession(sql)
        const args = open(sid)
        await openInput(sql, args)
        await new Promise((r) => setTimeout(r, 100))
        expect(heard).toEqual([]) // an open is not a resolution
        await resolveInput(sql, { requestId: args.requestId, outcome: 'approved', responder: ME_RESPONDER })
        await expect.poll(() => heard, { timeout: 5000 }).toEqual([args.requestId])
        // A second, losing resolve writes nothing and notifies nobody.
        await resolveInput(sql, { requestId: args.requestId, outcome: 'denied', responder: ME_RESPONDER })
        await new Promise((r) => setTimeout(r, 100))
        expect(heard).toEqual([args.requestId])
      } finally {
        await listener.end({ timeout: 5 })
      }
    })

    it("a Reset's cancel of the old run's entry notifies too", async () => {
      const listener = postgres(t.url, { max: 1, onnotice: () => {} })
      try {
        const heard: string[] = []
        await listener.listen(INPUT_CHANNEL, (payload) => heard.push(payload))
        const sid = await newSession(sql)
        const before = open(sid, 'run-1')
        await openInput(sql, before)
        await openInput(sql, open(sid, 'run-2'))
        await expect.poll(() => heard, { timeout: 5000 }).toEqual([before.requestId])
      } finally {
        await listener.end({ timeout: 5 })
      }
    })

    it('deny records the flow refusal once, audited', async () => {
      const sid = await newSession(sql)
      const args = {
        requestId: durableRequestId(sid, 'run-1', 'toolu_9'),
        sessionId: sid,
        runId: 'flow-run-1',
        tool: 'print_output',
        summary: 'box',
        inputHash: 'b'.repeat(64),
        reason: 'outside the grant',
        requestedBy: ME,
      }
      expect(await denyInput(sql, args)).toBe(true)
      expect(await denyInput(sql, args)).toBe(false)
      const [row] = await sql`SELECT outcome, responder, reason, hops FROM ai_input_responses`
      expect(row).toMatchObject({
        outcome: 'denied',
        responder: { kind: 'flow', id: 'flow-run-1', label: 'Flow' },
        reason: 'outside the grant',
      })
      expect(row?.hops).toMatchObject([{ hop: 'person', action: 'deny', actor: 'flow', reason: 'outside the grant' }])
      const audit = await sql`SELECT action, outcome, principal_kind, tool_use_id, detail FROM ai_audit`
      expect(audit.map((r) => ({ ...r }))).toEqual([
        {
          action: 'denied',
          outcome: 'denied',
          principal_kind: 'flow',
          tool_use_id: 'toolu_9',
          detail: 'print_output: outside the grant (requested by You)',
        },
      ])
    })

    describe('grant', () => {
      // The backend's tables (backend/scadbuddy/migrations/20261012T2029Z_flow_grants.sql),
      // only the columns grantInput reads.
      async function grantable(sid: string, requestId: string, hash: string): Promise<void> {
        await sql`CREATE TABLE flow_grants (id text PRIMARY KEY, status text NOT NULL, revoked_at timestamptz, expires_at timestamptz)`
        await sql`CREATE TABLE flow_grant_uses (request_id text PRIMARY KEY, grant_id text NOT NULL, session_id text, input_hash text NOT NULL)`
        await sql`INSERT INTO flow_grants VALUES ('g1', 'approved', NULL, now() + interval '1 hour')`
        await sql`INSERT INTO flow_grant_uses VALUES (${requestId}, 'g1', ${sid}, ${hash})`
      }
      const grant = (sid: string, requestId: string, hash: string) => ({
        requestId,
        sessionId: sid,
        tool: 'print_output',
        summary: 'box',
        inputHash: hash,
        grantId: 'g1',
        requestedBy: ME,
      })

      it('records an approval only for the recorded use, once, and a retry is true', async () => {
        const sid = await newSession(sql)
        const requestId = durableRequestId(sid, 'run-1', 'toolu_2')
        await grantable(sid, requestId, 'c'.repeat(64))
        expect(await grantInput(sql, grant(sid, requestId, 'd'.repeat(64)))).toBe(false)
        expect(await count(sql, 'ai_input_responses')).toBe(0)
        expect(await grantInput(sql, grant(sid, requestId, 'c'.repeat(64)))).toBe(true)
        expect(await grantInput(sql, grant(sid, requestId, 'c'.repeat(64)))).toBe(true)
        expect(await count(sql, 'ai_input_responses')).toBe(1)
        expect(await count(sql, 'ai_audit')).toBe(1)
        const [row] = await sql`SELECT outcome, responder FROM ai_input_responses`
        expect(row).toMatchObject({ outcome: 'approved', responder: { kind: 'run_grant', id: 'g1', label: 'Run grant' } })
        await sql`UPDATE flow_grants SET revoked_at = now()`
        const other = durableRequestId(sid, 'run-1', 'toolu_3')
        await sql`INSERT INTO flow_grant_uses VALUES (${other}, 'g1', ${sid}, ${'c'.repeat(64)})`
        expect(await grantInput(sql, grant(sid, other, 'c'.repeat(64)))).toBe(false)
      })
    })

    describe('parity with agent-durable store.py', () => {
      const script = new URL('./support/gate_store_parity.py', import.meta.url).pathname
      const cwd = new URL('../../agent-durable/', import.meta.url).pathname

      // Every column the two writers set, without ids and timestamps (an ISO timestamp
      // anywhere, an event's included, is masked, so the events compare byte for byte
      // otherwise).
      async function dump(s: Sql): Promise<unknown> {
        const mask = (v: unknown) => JSON.parse(JSON.stringify(v).replace(/\d{4}-\d\d-\d\dT[\d:.]+Z/g, 'T'))
        return mask({
          pending: await s`
            SELECT request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary, input_hash, prompt,
                   requested_by, responders, attention, expires_at, flow_run_id, hop, hop_until, parent_session,
                   parent_principal, outside_reason, hops
            FROM ai_pending_input ORDER BY request_id`,
          responses: await s`
            SELECT request_id, session_id, kind, outcome, response, response_hash, responder, reason, hops
            FROM ai_input_responses ORDER BY request_id`,
          events: (await s<{ session_id: string; seq: string; event: string }[]>`
            SELECT session_id, seq, event FROM ai_session_events ORDER BY session_id, seq`).map((r) => ({ ...r })),
          audit: await s`
            SELECT kind, action, surface, principal_kind, principal_id, principal_label, session_id, tool_use_id,
                   tier, input_hash, input_summary, request_id, outcome, detail,
                   duration_ms = GREATEST(0, (extract(epoch FROM finished_at - started_at) * 1000)::int) AS duration_from_started
            FROM ai_audit ORDER BY request_id, action`,
          sessions: await s`SELECT id, status, event_seq FROM ai_sessions ORDER BY id`,
        })
      }

      const hasUv = (): boolean => {
        try {
          execFileSync('uv', ['--version'], { stdio: 'ignore' })
          return true
        } catch {
          return false
        }
      }

      it.skipIf(!hasUv())('writes the same rows and events for the same calls', { timeout: 180_000 }, async () => {
        const s1 = '11111111-1111-4111-8111-111111111111'
        const s2 = '22222222-2222-4222-8222-222222222222'
        const s3 = '33333333-3333-4333-8333-333333333333'
        const expiresAt = new Date(Date.now() + 3600_000).toISOString()
        const flow = {
          flowRunId: 'flow-run-1',
          hop: 'parent' as const,
          hopUntil: expiresAt,
          parentSession: s2,
          parentPrincipal: 'browser:browser',
          outsideReason: 'not in the grant',
        }
        const long = 'é'.repeat(600)
        const o = (sid: string, runId: string, tu: string, over: Partial<OpenInput> = {}): OpenInput => ({
          ...open(sid, runId, 'approval', tu),
          expiresAt,
          ...over,
        })
        const steps: { op: 'open' | 'resolve' | 'deny'; args: object }[] = [
          // an approval, resolved by a person
          { op: 'open', args: o(s1, 'r1', 'toolu_a') },
          { op: 'resolve', args: { requestId: durableRequestId(s1, 'r1', 'toolu_a'), outcome: 'approved', responder: ME } },
          // a long summary (capped in the event and the audit), expired by the system
          { op: 'open', args: o(s1, 'r1', 'toolu_b', { summary: long }) },
          { op: 'resolve', args: { requestId: durableRequestId(s1, 'r1', 'toolu_b'), outcome: 'expired', responder: SYSTEM, reason: 'timeout' } },
          // an attention question answered
          {
            op: 'open',
            args: {
              ...open(s1, 'r1', 'answer', 'toolu_c'),
              expiresAt,
              attention: { reason: 'done', on_timeout: 'proceed' },
            },
          },
          { op: 'resolve', args: { requestId: durableRequestId(s1, 'r1', 'toolu_c'), outcome: 'answered', responder: ME, response: { answers: ['Red'] }, rawResponse: { kind: 'answer', choice: 'Red' } } },
          // a flow turn's escalating entry, resolved by a session; and a Reset's cancel
          { op: 'open', args: o(s2, 'r1', 'toolu_d', flow) },
          { op: 'resolve', args: { requestId: durableRequestId(s2, 'r1', 'toolu_d'), outcome: 'denied', responder: { kind: 'session', id: s1, label: 'Parent' }, reason: 'no' } },
          { op: 'open', args: o(s3, 'r1', 'toolu_e') },
          { op: 'open', args: o(s3, 'r2', 'toolu_f', flow) },
          // a flow denial
          {
            op: 'deny',
            args: {
              requestId: durableRequestId(s1, 'r1', 'toolu_g'),
              sessionId: s1,
              runId: 'flow-run-1',
              tool: 'print_output',
              summary: 'box',
              inputHash: 'b'.repeat(64),
              reason: 'outside',
              requestedBy: ME,
            },
          },
        ]
        const py = await throwawayDatabase()
        try {
          for (const db of [t.db.sql, py.db.sql]) for (const id of [s1, s2, s3]) await newSession(db, id)
          for (const step of steps) {
            const a = step.args as never
            if (step.op === 'open') await openInput(sql, a)
            else if (step.op === 'resolve') await resolveInput(sql, a)
            else await denyInput(sql, a)
          }
          execFileSync('uv', ['run', '--frozen', 'python', script, py.url, py.schema], { cwd, input: JSON.stringify(steps), stdio: ['pipe', 'inherit', 'inherit'] })
          const ts = await dump(sql)
          const reference = await dump(py.db.sql)
          expect(ts).toEqual(reference)
          expect((ts as { events: unknown[] }).events.length).toBeGreaterThan(20)
        } finally {
          await py.drop()
        }
      })
    })
  },
)

// R5 (unified runtime plan Step 9.7): a row opened with its `gate` is the request's record, and
// decideInput decides it there, as the workflow's `respond` handler does an entry it holds.
describe.skipIf(!TEST_DATABASE_URL)(`a recorded entry${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let t: Throwaway
  let sql: Sql
  beforeEach(async () => {
    t = await throwawayDatabase()
    sql = t.db.sql
  })
  afterEach(async () => {
    await t.drop()
  })

  const gate = (o: OpenInput) => ({
    id: o.requestId,
    kind: o.kind,
    inputHash: o.inputHash,
    requestedBy: { kind: ME.kind, id: ME.id },
    sessionOwner: { kind: ME.kind, id: ME.id },
    sessionCreator: { kind: ME.kind, id: ME.id },
    inRun: false,
    ...(o.kind === 'answer' ? { questions: [{ question: 'Which colour?', multiSelect: false }] } : {}),
  })
  const approve = (requestId: string, response: unknown = { kind: 'approval', decision: 'approve' }) =>
    decideInput(sql, { requestId, response, responder: ME_RESPONDER, role: 'browser' })

  it('decides it in one transaction: the row gone, its resolution written as resolve_input writes one', async () => {
    const sid = await newSession(sql)
    const o = open(sid)
    await openInput(sql, { ...o, gate: gate(o) })
    expect(await approve(o.requestId)).toEqual({ kind: 'approval', outcome: 'approved' })
    expect(await count(sql, 'ai_pending_input')).toBe(0)
    expect(await readInput(sql, { requestId: o.requestId })).toEqual({ outcome: 'approved', response: { decision: 'approve' }, responder: ME, reason: null })
    expect((await events(sql, sid)).map((e) => e.type)).toEqual(['approval.required', 'input.requested', 'session.status', 'approval.resolved', 'input.resolved', 'session.status'])
    expect(await statusOf(sql, sid)).toBe('running')
    // Decided once: the next response finds no row.
    await expect(approve(o.requestId)).rejects.toMatchObject({ code: 'stale' })
  })

  it("answers a question with the validator's answers, and keeps the hash of what was sent", async () => {
    const sid = await newSession(sql)
    const o = open(sid, 'run-1', 'answer')
    await openInput(sql, { ...o, gate: gate(o) })
    const body = { kind: 'answer', answers: { 'Which colour?': 'Blue' } }
    expect(await approve(o.requestId, body)).toEqual({ kind: 'answer', outcome: 'answered' })
    const [row] = await sql<{ response: unknown; response_hash: string }[]>`SELECT response, response_hash FROM ai_input_responses WHERE request_id = ${o.requestId}`
    expect(row).toEqual({ response: { answers: ['Blue'] }, response_hash: responseHash(body) })
  })

  it('refuses what the validator refuses, and writes nothing', async () => {
    const sid = await newSession(sql)
    const o = open(sid)
    await openInput(sql, { ...o, gate: gate(o) })
    await expect(approve(o.requestId, { kind: 'answer', text: 'yes' })).rejects.toMatchObject({ code: 'malformed' })
    await expect(approve(o.requestId, { kind: 'approval', decision: 'approve', input_hash: 'b'.repeat(64) })).rejects.toMatchObject({ code: 'input_mismatch' })
    await expect(decideInput(sql, { requestId: o.requestId, response: { kind: 'approval', decision: 'approve' }, responder: ME_RESPONDER, role: 'grant' })).rejects.toMatchObject({
      code: 'self',
    })
    expect(await count(sql, 'ai_pending_input')).toBe(1)
    expect(await count(sql, 'ai_input_responses')).toBe(0)
  })

  it('refuses a row past its timer as resolving: its workflow is about to end it', async () => {
    const sid = await newSession(sql)
    const o = { ...open(sid), expiresAt: new Date(Date.now() - 1_000).toISOString() }
    await openInput(sql, { ...o, gate: gate(o) })
    await expect(approve(o.requestId)).rejects.toMatchObject({ code: 'resolving' })
    expect(await count(sql, 'ai_pending_input')).toBe(1)
  })

  it('leaves a row with no gate to its workflow, and says so', async () => {
    const sid = await newSession(sql)
    const o = open(sid)
    await openInput(sql, o)
    expect(await approve(o.requestId)).toBe('workflow')
    expect(await count(sql, 'ai_pending_input')).toBe(1)
    expect(await readInput(sql, { requestId: o.requestId })).toBeNull()
  })
})
