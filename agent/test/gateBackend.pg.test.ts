import { execFileSync } from 'node:child_process'
import type { Sql } from 'postgres'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { durableRequestId } from '../src/gate/ids.js'
import { decideInput, denyInput, openInput, type OpenInput, resolveInput, type Responder } from '../src/gate/store.js'
import { EventLog } from '../src/sessions/eventLog.js'
import type { Owner } from '../src/sessions/protocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// The API's port of the gate's writes (backend scadbuddy/agent_data/gate.py, behind
// api/crud_agent_gate.py, #5299 step 10) against the agent's own SQL (gate/store.ts): the same
// calls write the same rows, events (byte for byte), statuses and audit rows. While the API does
// not yet list area `gate`, the agent writes over SQL, and an older pod and a newer one may write
// one session's entries in turn during a roll, so the two must not differ.

const ME: Owner = { kind: 'browser', id: 'browser', label: 'You' }
const SYSTEM: Responder = { kind: 'system', id: 'scadbuddy', label: 'ScadBuddy' }
const S1 = '11111111-1111-4111-8111-111111111111'
const S2 = '22222222-2222-4222-8222-222222222222'
const S3 = '33333333-3333-4333-8333-333333333333'

async function newSession(sql: Sql, id: string): Promise<void> {
  await sql`
    INSERT INTO ai_sessions
      (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
    VALUES (${id}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'running', 10, 1)`
}

/** Every column the two writers set, an ISO timestamp anywhere masked, so the events compare byte for byte otherwise. */
async function dump(s: Sql): Promise<unknown> {
  const mask = (v: unknown) => JSON.parse(JSON.stringify(v).replace(/\d{4}-\d\d-\d\dT[\d:.]+Z/g, 'T'))
  return mask({
    pending: await s`
      SELECT request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary, input_hash, prompt,
             requested_by, responders, attention, expires_at, flow_run_id, hop, hop_until, parent_session,
             parent_principal, outside_reason, hops, gate
      FROM ai_pending_input ORDER BY request_id`,
    responses: await s`
      SELECT r.request_id, r.session_id, r.kind, r.outcome, r.response, r.response_hash, r.responder, r.reason, r.hops, r.tool,
             r.requested_at IS NOT NULL AS requested, r.decision_traceparent
      FROM ai_input_responses r ORDER BY r.request_id`,
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

describe.skipIf(!TEST_DATABASE_URL)(`the gate's writes on the API${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let ts: Throwaway
  let py: Throwaway
  beforeEach(async () => {
    ts = await throwawayDatabase()
    py = await throwawayDatabase()
  })
  afterEach(async () => {
    await ts.drop()
    await py.drop()
  })

  it.skipIf(!hasUv())('writes what the agent writes for the same calls', { timeout: 300_000 }, async () => {
    const expiresAt = new Date(Date.now() + 3600_000).toISOString()
    const approval = (sid: string, run: string, toolUse: string, over: Partial<OpenInput> = {}): OpenInput => ({
      requestId: durableRequestId(sid, run, toolUse),
      sessionId: sid,
      workflowId: `session-${sid}`,
      workflowRunId: run,
      kind: 'approval',
      tool: 'print_output',
      toolUseId: toolUse,
      summary: '{"output":"box"}',
      inputHash: 'a'.repeat(64),
      prompt: '',
      requestedBy: ME,
      responders: ['browser', 'grant'],
      expiresAt,
      ...over,
    })
    const question = (sid: string, toolUse: string, over: Partial<OpenInput> = {}): OpenInput => ({
      ...approval(sid, 'r1', toolUse),
      kind: 'answer',
      tool: 'ask_user',
      summary: '',
      inputHash: null,
      prompt: 'Which colour?',
      responders: ['browser'],
      questions: [{ question: 'Which colour?', header: 'Colour', multiSelect: false, options: [] }],
      ...over,
    })
    const record = (o: OpenInput) => ({
      id: o.requestId,
      kind: o.kind,
      inputHash: o.inputHash,
      requestedBy: { kind: 'bearer', id: 'agent-1' },
      sessionOwner: { kind: 'browser', id: 'browser' },
      sessionCreator: { kind: 'browser', id: 'browser' },
      ...(o.kind === 'answer' ? { questions: [{ question: 'Which colour?', multiSelect: false }] } : {}),
    })
    const flow = { flowRunId: 'flow-run-1', hop: 'parent' as const, hopUntil: expiresAt, parentSession: S2, parentPrincipal: 'browser:browser', outsideReason: 'not in the grant' }
    const rid = (sid: string, run: string, tu: string) => durableRequestId(sid, run, tu)
    const decided = approval(S1, 'r1', 'toolu_h')
    const answered = question(S1, 'toolu_i')
    const steps: { op: 'open' | 'resolve' | 'deny' | 'decide'; args: object }[] = [
      // an approval, resolved by a person
      { op: 'open', args: approval(S1, 'r1', 'toolu_a') },
      { op: 'resolve', args: { requestId: rid(S1, 'r1', 'toolu_a'), outcome: 'approved', responder: ME } },
      // a long summary (capped in the event and the audit), expired by the system
      { op: 'open', args: approval(S1, 'r1', 'toolu_b', { summary: 'é'.repeat(600) }) },
      { op: 'resolve', args: { requestId: rid(S1, 'r1', 'toolu_b'), outcome: 'expired', responder: SYSTEM, reason: 'timeout' } },
      // an attention request answered, then one whose timer ran out
      { op: 'open', args: question(S1, 'toolu_c', { attention: { reason: 'done', on_timeout: 'proceed' } }) },
      { op: 'resolve', args: { requestId: rid(S1, 'r1', 'toolu_c'), outcome: 'answered', responder: ME, response: { answers: ['Red'] }, rawResponse: { kind: 'answer', choice: 'Red' } } },
      { op: 'open', args: question(S1, 'toolu_j', { attention: { reason: 'stuck', on_timeout: null } }) },
      { op: 'resolve', args: { requestId: rid(S1, 'r1', 'toolu_j'), outcome: 'timed_out', responder: SYSTEM } },
      // a tab wait the tab's return ended
      { op: 'open', args: question(S1, 'toolu_k') },
      { op: 'resolve', args: { requestId: rid(S1, 'r1', 'toolu_k'), outcome: 'cancelled', responder: SYSTEM, response: { reconnected: true }, reason: 'the tab is back' } },
      // a flow turn's escalating entry, resolved by a session; and a Reset's cancel
      { op: 'open', args: approval(S2, 'r1', 'toolu_d', flow) },
      { op: 'resolve', args: { requestId: rid(S2, 'r1', 'toolu_d'), outcome: 'denied', responder: { kind: 'session', id: S1, label: 'Parent' }, reason: 'no' } },
      { op: 'open', args: approval(S3, 'r1', 'toolu_e') },
      { op: 'open', args: approval(S3, 'r2', 'toolu_f', flow) },
      // a flow denial
      {
        op: 'deny',
        args: { requestId: rid(S1, 'r1', 'toolu_g'), sessionId: S1, runId: 'flow-run-1', tool: 'print_output', summary: 'box', inputHash: 'b'.repeat(64), reason: 'outside', requestedBy: ME },
      },
      // recorded entries decided against their record
      { op: 'open', args: { ...decided, gate: record(decided) } },
      { op: 'decide', args: { requestId: decided.requestId, response: { kind: 'approval', decision: 'approve', input_hash: 'a'.repeat(64) }, responder: ME, role: 'browser' } },
      { op: 'open', args: { ...answered, gate: record(answered) } },
      { op: 'decide', args: { requestId: answered.requestId, response: { kind: 'answer', answers: { 'Which colour?': 'Blue' } }, responder: ME, role: 'browser' } },
    ]
    for (const s of [ts.db.sql, py.db.sql]) for (const id of [S1, S2, S3]) await newSession(s, id)
    const db = { sql: ts.db.sql, events: new EventLog(ts.db.sql) }
    for (const step of steps) {
      const a = step.args as never
      if (step.op === 'open') await openInput(db, a)
      else if (step.op === 'resolve') await resolveInput(db, a)
      else if (step.op === 'deny') await denyInput(db, a)
      else await decideInput(db, a)
    }
    const script = new URL('./support/gate_backend_parity.py', import.meta.url).pathname
    const cwd = new URL('../../backend/', import.meta.url).pathname
    execFileSync('uv', ['run', '--frozen', 'python', script, py.url, py.schema], { cwd, input: JSON.stringify(steps), stdio: ['pipe', 'inherit', 'inherit'] })
    const agent = (await dump(ts.db.sql)) as { events: unknown[]; responses: unknown[] }
    expect(agent).toEqual(await dump(py.db.sql))
    expect(agent.events.length).toBeGreaterThan(30)
    expect(agent.responses).toHaveLength(10)
  })
})
