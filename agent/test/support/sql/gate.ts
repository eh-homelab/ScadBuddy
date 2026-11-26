import type { Sql, TransactionSql } from 'postgres'
import { responseHash } from '../../../src/gate/canonical.js'
import type { DecideAnswer, DecideCall, GateRoutes, Logged } from '../../../src/gate/crudStore.js'
import { parseRequestId } from '../../../src/gate/ids.js'
import { cut } from '../../../src/gate/limits.js'
import {
  type DenyInput,
  flowResponder,
  type GateDb,
  type GateRecord,
  INPUT_CHANNEL,
  type InputRecord,
  type OpenInput,
  type ResolveInput,
  type Responder,
} from '../../../src/gate/store.js'
import { RespondRefusal, validateRespond } from '../../../src/gate/validate.js'
import type { EventLog } from '../../../src/sessions/eventLog.js'
import type { InputEntry, InputOutcome, Owner, ServerEvent } from '../../../src/sessions/protocol.js'

// Tests only: the gate's writes over the test database, the SQL the agent ran before the API
// took area `gate` (#5299 step 12), behind the routes' interface (`GateRoutes`). src holds no
// SQL; the API's is tools/go/core/internal/gate. The run grant's check has no SQL here.

/** A GateDb over the test database: the session events go to `events` as a route's `logged` do. */
export function sqlGateDb(sql: Sql, events: EventLog): GateDb {
  return { events, crud: new SqlGate(sql) }
}

const PROTOCOL_VERSION = 1
const SYSTEM: Responder = { kind: 'system', id: 'scadbuddy', label: 'ScadBuddy' }
// approvals/service.ts APPROVAL_SUMMARY_MAX: the arguments' 500, plus a resolving tool's line (#4442).
const APPROVAL_SUMMARY_MAX = 500 + 200 + 1

type Hop = { hop: string; action: string; actor: string; at: string; reason?: string }

/** How a resolution reads in an entry's `hops` (spec 2026-10-11 §4.6). */
const HOP_ACTIONS: Record<string, string> = {
  approved: 'approve',
  denied: 'deny',
  expired: 'timeout',
  cancelled: 'cancelled',
}

/** What `ai_pending_input` returns for `RETURNING *`, with `created_at::text`. */
type PendingRow = {
  request_id: string
  session_id: string
  kind: 'approval' | 'answer'
  tool: string
  summary: string
  input_hash: string | null
  prompt: string
  requested_by: Owner | null
  responders: ('browser' | 'grant')[]
  attention: { reason: string; on_timeout: string | null } | null
  created_at: Date
  /** `created_at::text`: microseconds, which a Date loses. */
  created_at_text: string
  expires_at: Date
  flow_run_id: string | null
  hop: 'parent' | 'person' | null
  hops: Hop[] | null
  /** The decision's `agent.approval` span (#5041), which the respond route wrote; an AgentSession's only. */
  decision_traceparent?: string | null
}

/** What `audit` reads of an entry: a pending row, or the grant and deny paths' stand-in. */
type Audited = {
  request_id: string
  session_id: string
  requested_by: Owner | null
  tool: string
  input_hash: string | null
  summary: string
  /** Microsecond text (`created_at::text`); `::text::timestamptz` below, since postgres.js sends a timestamptz param as a Date, in milliseconds. */
  created_at: string
}

function actor(responder: Responder): string {
  const kind = responder.kind ?? ''
  if (kind === 'session') return `session:${responder.id ?? ''}`
  return ['browser', 'system', 'flow'].includes(kind) ? kind : `${kind}:${responder.id ?? ''}`
}

function hop(at: string, action: string, by: string, reason?: string | null): Hop {
  return { hop: at, action, actor: by, at: iso(new Date()), ...(reason ? { reason } : {}) }
}

function event(body: object): ServerEvent {
  return { v: PROTOCOL_VERSION, ...body } as ServerEvent
}

function iso(at: Date): string {
  return at.toISOString()
}

function cap(text: string, limit: number): string {
  const points = [...text]
  return points.length <= limit ? text : `${points.slice(0, limit - 1).join('')}…`
}

/** The §6.6 entry shape (`InputEntry`) of a projection row. */
function entryOf(row: PendingRow): InputEntry {
  return {
    id: row.request_id,
    kind: row.kind,
    session_id: String(row.session_id),
    tool: row.tool,
    summary: row.summary,
    input_hash: row.input_hash,
    prompt: row.prompt,
    requested_by: row.requested_by,
    responders: [...row.responders],
    created_at: iso(row.created_at),
    expires_at: iso(row.expires_at),
    ...(row.attention === null ? {} : { attention: row.attention }),
  }
}

/** A transaction's writes: its connection, and the batches it appended, which the route answers as `logged`. */
type Tx = { tx: TransactionSql; appended: Logged[] }

/** Runs `fn` in one transaction; answers its value and the batches it appended once it has committed. */
async function transaction<T>(sql: Sql, fn: (w: Tx) => Promise<T>): Promise<{ value: T; logged: Logged[] }> {
  const appended: Logged[] = []
  // The cast drops postgres.js's UnwrapPromiseArray<T>: safe, since no caller's `fn` resolves to an array of promises.
  const value = (await sql.begin((tx) => fn({ tx, appended }))) as T
  return { value, logged: appended }
}

/** Appends `events` to the session's log in the transaction, as the API's routes do. */
export async function appendIn(tx: TransactionSql, sessionId: string, events: readonly ServerEvent[]): Promise<number[]> {
  if (events.length === 0) return []
  const texts = events.map((e) => JSON.stringify(e))
  const rows = await tx<{ seq: string }[]>`
    WITH s AS (
      UPDATE ai_sessions SET event_seq = event_seq + ${texts.length}, updated_at = now()
      WHERE id = ${sessionId}
      RETURNING event_seq - ${texts.length} AS base
    )
    INSERT INTO ai_session_events (session_id, seq, event)
    SELECT ${sessionId}, s.base + e.ord, e.event
    FROM s, unnest(${tx.array(texts)}::text[]) WITH ORDINALITY AS e(event, ord)
    RETURNING seq`
  if (rows.length !== texts.length) throw new Error(`session ${sessionId} does not exist`)
  return rows.map((r) => Number(r.seq)).sort((a, b) => a - b)
}

async function append(w: Tx, sessionId: string, events: ServerEvent[]): Promise<void> {
  if (events.length === 0) return
  w.appended.push({ session_id: sessionId, events, seqs: await appendIn(w.tx, sessionId, events) })
}

/** Records the end of the entry `row` (already deleted, in this transaction). */
async function resolved(
  w: Tx,
  row: PendingRow,
  outcome: InputOutcome,
  responder: Responder,
  response: unknown,
  reason: string | null,
  hash: string | null = null,
): Promise<void> {
  const { tx } = w
  const sid = String(row.session_id)
  const rid = row.request_id
  // An attention request's timer gives no reason (the workflow's `#expire`); it gets classic's
  // (classic's QuestionService `timeOut`, before plan Step 9.7, #815), so the panel and the audit row say why it ended.
  if (outcome === 'timed_out' && reason === null && row.attention) {
    reason = `nobody replied in time (on_timeout: ${row.attention.on_timeout ?? 'proceed'})`
  }
  const hops: Hop[] = [...(row.hops ?? [])]
  if (row.flow_run_id !== null && row.hop !== null) {
    // A run's entry keeps its way up, the resolution as its last step (§4.6).
    hops.push(hop(row.hop, HOP_ACTIONS[outcome] ?? outcome, actor(responder), reason))
  }
  // The tool and when it was asked, which the attention rate limit counts once the pending row is gone (#5003).
  await tx`
    INSERT INTO ai_input_responses
      (request_id, session_id, kind, outcome, response, responder, reason, hops, response_hash, tool, requested_at,
       decision_traceparent)
    VALUES (${rid}, ${sid}, ${row.kind}, ${outcome},
            ${response === undefined || response === null ? null : tx.json(response as never)}::jsonb,
            ${tx.json(responder)}, ${reason}, ${tx.json(hops)}, ${hash}, ${row.tool}, ${row.created_at_text}::timestamptz,
            ${row.decision_traceparent ?? null})`
  const person = responder.kind !== 'system'
  const events: ServerEvent[] = []
  if (row.kind === 'approval') {
    events.push(
      event({
        type: 'approval.resolved',
        sessionId: sid,
        id: rid,
        approved: outcome === 'approved',
        decision: ['approved', 'denied', 'expired'].includes(outcome) ? outcome : 'cancelled',
        ...(person && ['approved', 'denied'].includes(outcome) ? { by: responder } : {}),
        ...(reason && !['approved', 'denied'].includes(outcome) ? { reason } : {}),
      }),
    )
  } else {
    const answers = (response as { answers?: unknown } | null | undefined)?.answers
    const answered =
      outcome === 'answered' && typeof response === 'object' && response !== null && !Array.isArray(response) && answers !== undefined
    events.push(
      event({
        type: 'question.resolved',
        sessionId: sid,
        id: rid,
        answered: outcome === 'answered',
        ...(answered ? { answers: [...(answers as unknown[])], by: responder } : {}),
        ...(reason && outcome !== 'answered' ? { reason } : {}),
        // #5003: a tab wait the tab's return ended, as a classic one's event says it (classic's QuestionService `reconnected`, before plan Step 9.7).
        ...(outcome === 'cancelled' && (response as { reconnected?: unknown } | null | undefined)?.reconnected === true ? { reconnected: true } : {}),
      }),
    )
  }
  events.push(
    event({ type: 'input.resolved', sessionId: sid, id: rid, kind: row.kind, outcome, ...(reason ? { reason } : {}) }),
  )
  // The status follows what of the session is still parked: running once nothing is, else the
  // kind still waiting (an approval first), as classic's stores hand back (approvals/service.ts,
  // classic's QuestionService `withStatus`, before plan Step 9.7). Read under the session row's lock, so two resolves that
  // overlap cannot each see the other's entry still there and both leave it waiting (#3685).
  // agent-durable's gate/store.py `_resolved` does the same, and audits as below (#5052).
  await tx`SELECT 1 FROM ai_sessions WHERE id = ${sid} FOR UPDATE`
  const moved = await tx<{ status: 'running' | 'waiting_approval' | 'waiting_input' }[]>`
    WITH next AS (
      SELECT CASE
        WHEN EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = ${sid} AND kind = 'approval') THEN 'waiting_approval'
        WHEN EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = ${sid}) THEN 'waiting_input'
        ELSE 'running' END AS status)
    UPDATE ai_sessions SET status = next.status, updated_at = now() FROM next
    WHERE id = ${sid} AND ai_sessions.status IN ('waiting_approval', 'waiting_input') AND ai_sessions.status <> next.status
    RETURNING ai_sessions.status`
  if (moved[0]) events.push(event({ type: 'session.status', sessionId: sid, status: moved[0].status }))
  await append(w, sid, events)
  await tx`SELECT pg_notify(${INPUT_CHANNEL}, ${rid})`
  if (row.kind === 'approval') await audit(tx, { ...row, created_at: row.created_at_text }, outcome, responder, reason)
  else if (outcome !== 'answered') await auditUnanswered(tx, row, outcome, reason)
}

/**
 * The `question` row classic's QuestionService writes for a question nobody answered (#1075):
 * ScadBuddy's own, `system`. An answer's row is the respond route's, which knows the client.
 */
async function auditUnanswered(tx: TransactionSql, row: PendingRow, outcome: InputOutcome, reason: string | null): Promise<void> {
  const parsed = parseRequestId(row.request_id)
  const toolUseId = parsed?.store === 'durable' ? parsed.toolUseId : null
  const action = outcome === 'expired' || outcome === 'timed_out' ? outcome : 'cancelled'
  const detail = `${row.tool} question ${row.request_id}${reason ? `: ${reason}` : ''}`
  await tx`
    INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id, principal_label,
                          session_id, tool_use_id, tier, request_id, outcome, detail, started_at, finished_at, duration_ms)
    VALUES ('question', ${action}, 'system', ${SYSTEM.kind}, ${SYSTEM.id}, ${SYSTEM.label}, ${String(row.session_id)},
            ${toolUseId}, 'read', ${row.request_id}, 'refused', ${cut(detail, 500)}, ${row.created_at_text}::timestamptz, now(),
            GREATEST(0, (extract(epoch FROM now() - ${row.created_at_text}::timestamptz) * 1000)::int))`
}

/** The `approval` row ApprovalService writes for a classic decision (audit/log.ts). */
async function audit(
  tx: TransactionSql,
  row: Audited,
  outcome: InputOutcome,
  responder: Responder,
  reason: string | null,
): Promise<void> {
  const person = responder.kind !== 'system'
  const by = person ? responder : SYSTEM
  const parsed = parseRequestId(row.request_id)
  const toolUseId = parsed?.store === 'durable' ? parsed.toolUseId : null
  const requested = row.requested_by?.label ?? '?'
  const detail = `${row.tool}${reason ? `: ${reason}` : ''} (requested by ${requested})`
  const auditOutcome = outcome === 'approved' ? 'ok' : outcome === 'denied' ? 'denied' : 'refused'
  const started = row.created_at
  await tx`
    INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id, principal_label,
                          session_id, tool_use_id, tier, input_hash, input_summary, request_id,
                          outcome, detail, started_at, finished_at, duration_ms)
    VALUES ('approval', ${outcome}, ${person ? 'http' : 'system'}, ${cut(by.kind, 50)}, ${cut(by.id, 200)},
            ${cut(by.label, 200)}, ${String(row.session_id)}, ${toolUseId}, 'outward', ${row.input_hash},
            ${cut(row.summary, 1000)}, ${row.request_id}, ${auditOutcome}, ${cut(detail, 500)},
            ${started}::text::timestamptz, now(), GREATEST(0, (extract(epoch FROM now() - ${started}::text::timestamptz) * 1000)::int))`
}

function stand(args: DenyInput): Omit<Audited, 'created_at'> {
  return {
    request_id: args.requestId,
    session_id: args.sessionId,
    requested_by: args.requestedBy,
    tool: args.tool,
    input_hash: args.inputHash,
    summary: args.summary,
  }
}

async function lockSessionOf(tx: TransactionSql, requestId: string): Promise<void> {
  await tx`SELECT 1 FROM ai_sessions WHERE id = (SELECT session_id FROM ai_pending_input WHERE request_id = ${requestId}) FOR UPDATE`
}

export class SqlGate implements GateRoutes {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async open(args: OpenInput): Promise<{ opened: boolean; logged: Logged[] }> {
    const first = await transaction(this.#sql, async (w) => {
      const { tx } = w
      // The session first, as every resolution takes it (lockSessionOf).
      await tx`SELECT 1 FROM ai_sessions WHERE id = ${args.sessionId} FOR UPDATE`
      const stale = await tx<PendingRow[]>`
        DELETE FROM ai_pending_input WHERE session_id = ${args.sessionId} AND workflow_run_id <> ${args.workflowRunId}
        RETURNING *, created_at::text AS created_at_text`
      for (const old of stale) await resolved(w, old, 'cancelled', SYSTEM, null, 'reset')
    })
    const second = await transaction(this.#sql, async (w) => {
      const { tx } = w
      const hops = args.hop == null ? [] : [hop(args.hop, 'parked', 'flow', args.outsideReason)]
      const inserted = await tx<PendingRow[]>`
        INSERT INTO ai_pending_input
          (request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary,
           input_hash, prompt, requested_by, responders, attention, expires_at,
           flow_run_id, hop, hop_until, parent_session, parent_principal,
           outside_reason, hops, gate)
        SELECT ${args.requestId}, ${args.sessionId}::uuid, ${args.workflowId}, ${args.workflowRunId}, ${args.kind},
               ${args.tool}, ${args.summary}, ${args.inputHash}, ${args.prompt}, ${tx.json(args.requestedBy)},
               ${tx.array([...args.responders])}::text[],
               ${args.attention == null ? null : tx.json(args.attention)}::jsonb, ${args.expiresAt}::timestamptz,
               ${args.flowRunId ?? null}, ${args.hop ?? null}, ${args.hopUntil ?? null}::timestamptz,
               ${args.parentSession ?? null}::uuid, ${args.parentPrincipal ?? null}, ${args.outsideReason ?? null},
               ${tx.json(hops)}, ${args.gate == null ? null : tx.json(args.gate as never)}::jsonb
        -- A retried open that lands after its entry was resolved opens nothing.
        WHERE NOT EXISTS (SELECT 1 FROM ai_input_responses WHERE request_id = ${args.requestId})
        ON CONFLICT (request_id) DO NOTHING
        RETURNING *, created_at::text AS created_at_text`
      const row = inserted[0]
      if (row === undefined) return false
      const sid = args.sessionId
      const events: ServerEvent[] = []
      if (args.kind === 'approval') {
        events.push(
          event({
            type: 'approval.required',
            sessionId: sid,
            id: args.requestId,
            tool: args.toolUseId,
            summary: cap(`${args.tool} ${args.summary}`, APPROVAL_SUMMARY_MAX),
            risk: 'outward',
          }),
        )
      } else {
        events.push(
          event({
            type: 'question.asked',
            sessionId: sid,
            id: args.requestId,
            tool: args.toolUseId,
            questions: args.questions ?? [],
            ...(args.attention == null
              ? {}
              : {
                  attention: {
                    reason: args.attention.reason,
                    onTimeout: args.attention.on_timeout,
                    expiresAt: iso(row.expires_at),
                  },
                }),
          }),
        )
      }
      events.push(event({ type: 'input.requested', sessionId: sid, entry: entryOf(row) }))
      const status = args.kind === 'approval' ? 'waiting_approval' : 'waiting_input'
      const changed = await tx`
        UPDATE ai_sessions SET status = ${status}, updated_at = now()
        WHERE id = ${sid} AND status <> ${status} RETURNING status`
      if (changed.length > 0) events.push(event({ type: 'session.status', sessionId: sid, status }))
      await append(w, sid, events)
      return true
    })
    return { opened: second.value, logged: [...first.logged, ...second.logged] }
  }

  async grant(): Promise<boolean> {
    throw new Error("the run grant's check runs on the API; the test database has no route to it")
  }

  async deny(args: DenyInput): Promise<boolean> {
    const responder = flowResponder(args.runId)
    const { value } = await transaction(this.#sql, async ({ tx }) => {
      const inserted = await tx`
        INSERT INTO ai_input_responses
          (request_id, session_id, kind, outcome, responder, reason, hops)
        VALUES (${args.requestId}, ${args.sessionId}, 'approval', 'denied', ${tx.json(responder)},
                ${args.reason}, ${tx.json([hop('person', 'deny', 'flow', args.reason)])})
        ON CONFLICT (request_id) DO NOTHING RETURNING request_id`
      if (inserted.length === 0) return false
      await audit(tx, { ...stand(args), created_at: new Date().toISOString() }, 'denied', responder, args.reason)
      return true
    })
    return value
  }

  async resolve(args: ResolveInput): Promise<{ resolved: boolean; logged: Logged[] }> {
    const { value, logged } = await transaction(this.#sql, async (w) => {
      const { tx } = w
      await lockSessionOf(tx, args.requestId)
      const deleted = await tx<PendingRow[]>`DELETE FROM ai_pending_input WHERE request_id = ${args.requestId} RETURNING *, created_at::text AS created_at_text`
      const row = deleted[0]
      if (row === undefined) return false
      await resolved(w, row, args.outcome, args.responder, args.response, args.reason ?? null, args.rawResponse === undefined ? null : responseHash(args.rawResponse))
      return true
    })
    return { resolved: value, logged }
  }

  /** The API's decide, over the test database: the validator first, then the live check's run, then the write. */
  async decide(call: DecideCall): Promise<DecideAnswer> {
    const { value, logged } = await transaction(this.#sql, async (w): Promise<DecideAnswer> => {
      const { tx } = w
      await lockSessionOf(tx, call.requestId)
      const [row] = await tx<(PendingRow & { gate: GateRecord | null; expired: boolean; workflow_id: string; workflow_run_id: string })[]>`
        SELECT *, created_at::text AS created_at_text, expires_at <= now() AS expired
        FROM ai_pending_input WHERE request_id = ${call.requestId} FOR UPDATE`
      if (row === undefined) return { refused: { code: 'stale', message: `no pending input ${call.requestId}: it is stale or was never asked` } }
      if (row.gate === null) return { workflow: true }
      let valid
      try {
        valid = validateRespond(
          { ...row.gate, id: row.request_id, kind: row.kind, state: row.expired ? 'resolving' : 'pending' },
          { requestId: call.requestId, response: call.response, responder: { kind: call.responder.kind as Owner['kind'], id: call.responder.id }, role: call.role },
        )
      } catch (err) {
        if (err instanceof RespondRefusal) return { refused: { code: err.code, message: err.message } }
        throw err
      }
      if (call.checkLive && call.liveRun !== row.workflow_run_id) return { liveCheck: { workflowId: row.workflow_id, runId: row.workflow_run_id } }
      const outcome: InputOutcome = 'decision' in valid ? (valid.decision === 'approve' ? 'approved' : 'denied') : 'answered'
      const response = 'decision' in valid ? { decision: valid.decision } : { answers: [...valid.answers] }
      await tx`DELETE FROM ai_pending_input WHERE request_id = ${call.requestId}`
      await resolved(w, row, outcome, call.responder, response, null, responseHash(call.response))
      return { decided: { kind: row.kind, outcome }, logged: [] }
    })
    return 'decided' in value ? { ...value, logged } : value
  }

  async read(requestId: string): Promise<InputRecord | null> {
    const [row] = await this.#sql<InputRecord[]>`
      SELECT outcome, response, responder, reason FROM ai_input_responses WHERE request_id = ${requestId}`
    return row ?? null
  }
}
