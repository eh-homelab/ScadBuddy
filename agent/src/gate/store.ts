import { randomUUID } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import type { InputEntry, InputOutcome, Owner, ServerEvent } from '../sessions/protocol.js'
import { busKindOf } from '../sessions/busEvents.js'
import { responseHash } from './canonical.js'
import { parseRequestId } from './ids.js'
import { cut } from './limits.js'
import { type GateEntry, RespondRefusal, type Role, validateRespond } from './validate.js'

// Every write about a durable entry (durable-agents spec §6.6, "How a durable entry
// opens and closes"), ported whole from agent-durable's gate/store.py; the SQL is the
// same. Line numbers are that file's at the port:
//   _hop :139, _cap :159, _entry :163, _rows :183  -> hop, cap, entryOf, the tagged queries
//   _append :191 (events, then the bus NOTIFY on commit)  -> append
//   _resolved :236, _audit :324                   -> resolved, audit
//   openInput :368, grantInput :478, denyInput :536, resolveInput :577
//   run_grant_responder :115, flow_responder :119, _actor :132
// decideInput and readInput are new (R5, plan Step 9.7): the respond route's write of a decision
// on a row that is its request's record, and the workflow's read of what was decided.
// Python's Outcome and Kind are InputOutcome and InputEntry['kind']; _WAITING/_SETTLED are
// busKindOf's (sessions/busEvents.ts). Each function returns what Python's returns: false when nothing was
// written (already open or resolved, gone, or the grant does not hold).
//
// Beyond Python: every resolution (resolveInput, and the Reset cancel in openInput) also sends `NOTIFY scadbuddy_input, <request id>`, in the
// transaction that deletes the row, so a waiter learns of a resolution without polling.
// The channel is new; Python does not send it. Nor does Python resolve an entry with the response
// `{reconnected: true}` (an AgentSession's tab wait, #5003), whose `question.resolved` says
// `reconnected` as a classic one does.

const PROTOCOL_VERSION = 1
const PG_CHANNEL = 'scadbuddy_events'
export const INPUT_CHANNEL = 'scadbuddy_input'
// A foreign id on purpose: this agent's own LISTEN consumer then wakes its local followers, since
// this file does not write through EventLog.append. If the gate ever does, use the publisher's id.
const REPLICA = 'agent-durable'
const SYSTEM: Responder = { kind: 'system', id: 'scadbuddy', label: 'ScadBuddy' }
// approvals/service.ts APPROVAL_SUMMARY_MAX: the arguments' 500, plus a resolving tool's line (#4442).
const APPROVAL_SUMMARY_MAX = 500 + 200 + 1

export type Responder = { kind: string; id: string; label: string }
type Hop = { hop: string; action: string; actor: string; at: string; reason?: string }

export type OpenInput = {
  requestId: string
  sessionId: string
  workflowId: string
  workflowRunId: string
  kind: 'approval' | 'answer'
  tool: string
  toolUseId: string
  summary: string
  inputHash: string | null
  prompt: string
  requestedBy: Owner
  responders: readonly ('browser' | 'grant')[]
  /** ISO 8601: when the entry's timer fires (at most 86 400 s after it parks). */
  expiresAt: string
  /** The card the panel shows (question.asked's `questions`); absent for an approval. */
  questions?: unknown[] | null
  /** An attention request's {reason, on_timeout}; absent otherwise. */
  attention?: { reason: string; on_timeout: string | null } | null
  flowRunId?: string | null
  hop?: 'parent' | 'person' | null
  /** ISO 8601. */
  hopUntil?: string | null
  parentSession?: string | null
  parentPrincipal?: string | null
  outsideReason?: string | null
  /**
   * The entry as the validator checks a response against it (R5, plan Step 9.7): with it the row
   * is the request's record, and the respond route decides it here (decideInput). Absent, the
   * workflow holds the entry, and a response goes to it as the `respond` Update.
   */
  gate?: GateRecord | null
}

/** What `ai_pending_input.gate` keeps: the validator's view of the entry, without its state (the row's). */
export type GateRecord = Omit<GateEntry, 'state'>

export type DenyInput = {
  requestId: string
  sessionId: string
  runId: string
  tool: string
  summary: string
  inputHash: string
  reason: string | null
  requestedBy: Owner
}

export type ResolveInput = {
  requestId: string
  outcome: InputOutcome
  responder: Responder
  response?: unknown
  /** The response as the person sent it, whose hash is kept for a retry's comparison (#3724). */
  rawResponse?: unknown
  reason?: string | null
}

export type GrantInput = {
  requestId: string
  sessionId: string
  tool: string
  summary: string
  /** describe_call's hash of the call: what the grant's recorded use must be for. */
  inputHash: string
  grantId: string
  requestedBy: Owner
}

export function runGrantResponder(grantId: string): Responder {
  return { kind: 'run_grant', id: grantId, label: 'Run grant' }
}

export function flowResponder(runId: string): Responder {
  return { kind: 'flow', id: runId, label: 'Flow' }
}

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

/** agent/src/sessions/eventLog.ts `append`, then the batch's bus NOTIFY (on commit). */
async function append(tx: TransactionSql, sessionId: string, events: ServerEvent[]): Promise<void> {
  if (events.length === 0) return
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
  const seqs = rows.map((r) => Number(r.seq))
  const { kind, status } = busKindOf(events)
  const payload = {
    id: randomUUID().replaceAll('-', ''),
    at: iso(new Date()),
    kind,
    session_id: sessionId,
    seq: Math.max(...seqs),
    replica: REPLICA,
    ...(status === undefined ? {} : { status }),
  }
  await tx`SELECT pg_notify(${PG_CHANNEL}, ${JSON.stringify(payload)})`
}

/** Records the end of the entry `row` (already deleted, in this transaction). */
async function resolved(
  tx: TransactionSql,
  row: PendingRow,
  outcome: InputOutcome,
  responder: Responder,
  response: unknown,
  reason: string | null,
  hash: string | null = null,
): Promise<void> {
  const sid = String(row.session_id)
  const rid = row.request_id
  // An attention request's timer gives no reason (the workflow's `#expire`); it gets classic's
  // (questions/service.ts `timeOut`, #815), so the panel and the audit row say why it ended.
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
        // #5003: a tab wait the tab's return ended, as a classic one's event says it (questions/service.ts `reconnected`).
        ...(outcome === 'cancelled' && (response as { reconnected?: unknown } | null | undefined)?.reconnected === true ? { reconnected: true } : {}),
      }),
    )
  }
  events.push(
    event({ type: 'input.resolved', sessionId: sid, id: rid, kind: row.kind, outcome, ...(reason ? { reason } : {}) }),
  )
  // The status follows what of the session is still parked: running once nothing is, else the
  // kind still waiting (an approval first), as classic's stores hand back (approvals/service.ts,
  // questions/service.ts `withStatus`). Read under the session row's lock, so two resolves that
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
  await append(tx, sid, events)
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

/**
 * Opens the entry; false when it was open already (a retried activity adds nothing).
 *
 * First, in its own transaction, the session's entries of another workflow run (a
 * Reset started a new one) are resolved `cancelled`, reason `reset`, so after a Reset
 * the read shows exactly one entry for the call.
 */
export async function openInput(sql: Sql, args: OpenInput): Promise<boolean> {
  await sql.begin(async (tx) => {
    // The session first, as every resolution takes it (lockSessionOf).
    await tx`SELECT 1 FROM ai_sessions WHERE id = ${args.sessionId} FOR UPDATE`
    const stale = await tx<PendingRow[]>`
      DELETE FROM ai_pending_input WHERE session_id = ${args.sessionId} AND workflow_run_id <> ${args.workflowRunId}
      RETURNING *, created_at::text AS created_at_text`
    for (const old of stale) await resolved(tx, old, 'cancelled', SYSTEM, null, 'reset')
  })
  return await sql.begin(async (tx) => {
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
    await append(tx, sid, events)
    return true
  })
}

/**
 * Records the run grant's approval of a call, only if the backend recorded the grant's
 * use of this request id, by this session, for this very hash, and the grant is in
 * force: one transaction under the grant row's share lock, so a revocation lands wholly
 * before or after it (and the agent checks the grant again as the call runs). Nothing
 * of the verdict is taken on trust. False when it does not hold; the call then parks.
 * No entry, no events: nothing is listed and nobody is notified.
 */
export async function grantInput(sql: Sql, args: GrantInput): Promise<boolean> {
  const responder = runGrantResponder(args.grantId)
  return await sql.begin(async (tx) => {
    const holds = await tx`
      SELECT 1 FROM flow_grants g JOIN flow_grant_uses u ON u.grant_id = g.id
      WHERE g.id = ${args.grantId} AND u.request_id = ${args.requestId} AND u.session_id = ${args.sessionId}
        AND u.input_hash = ${args.inputHash} AND g.status = 'approved' AND g.revoked_at IS NULL
        AND now() < g.expires_at
      FOR SHARE OF g`
    if (holds.length === 0) return false
    const inserted = await tx`
      INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder)
      VALUES (${args.requestId}, ${args.sessionId}, 'approval', 'approved', ${tx.json(responder)})
      ON CONFLICT (request_id) DO NOTHING RETURNING request_id`
    if (inserted.length === 0) {
      // A retried activity: the same grant's approval is there already.
      const [held] = await tx<{ outcome: string; responder: Responder }[]>`
        SELECT outcome, responder FROM ai_input_responses WHERE request_id = ${args.requestId}`
      if (held === undefined) return false
      return held.outcome === 'approved' && sameResponder(held.responder, responder)
    }
    await audit(tx, { ...stand(args), created_at: new Date().toISOString() }, 'approved', responder, null)
    return true
  })
}

/**
 * Records the flow's refusal of a call (`on_outside="deny"`): denied, by the flow, with
 * the grant's reason; audited as a decision is. False when the request id was resolved
 * already (a retried activity writes nothing twice).
 */
export async function denyInput(sql: Sql, args: DenyInput): Promise<boolean> {
  const responder = flowResponder(args.runId)
  return await sql.begin(async (tx) => {
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
}

function stand(args: GrantInput | DenyInput): Omit<Audited, 'created_at'> {
  return {
    request_id: args.requestId,
    session_id: args.sessionId,
    requested_by: args.requestedBy,
    tool: args.tool,
    input_hash: args.inputHash,
    summary: args.summary,
  }
}

function sameResponder(a: Responder, b: Responder): boolean {
  return a.kind === b.kind && a.id === b.id && a.label === b.label
}

/**
 * Takes the lock of the session `requestId`'s entry is in, before anything of the entry. Two
 * resolutions in one session then queue on it: each writes ai_input_responses (whose foreign key
 * holds the session row KEY SHARE) and then locks the session row FOR UPDATE (`resolved`), so
 * taken in that order two of them deadlock (the respond route decides concurrently, R5).
 */
async function lockSessionOf(tx: TransactionSql, requestId: string): Promise<void> {
  await tx`SELECT 1 FROM ai_sessions WHERE id = (SELECT session_id FROM ai_pending_input WHERE request_id = ${requestId}) FOR UPDATE`
}

/** Resolves the entry; false when it was gone (resolved by another, or never opened). */
export async function resolveInput(sql: Sql, args: ResolveInput): Promise<boolean> {
  return await sql.begin(async (tx) => {
    await lockSessionOf(tx, args.requestId)
    const deleted = await tx<PendingRow[]>`DELETE FROM ai_pending_input WHERE request_id = ${args.requestId} RETURNING *, created_at::text AS created_at_text`
    const row = deleted[0]
    if (row === undefined) return false
    await resolved(tx, row, args.outcome, args.responder, args.response, args.reason ?? null, args.rawResponse === undefined ? null : responseHash(args.rawResponse))
    return true
  })
}

/**
 * A response to a request whose row is its record (decideInput), as the respond route passes it.
 * `live` says whether the row's workflow run still runs: one that ended without resolving it (a
 * terminated run, until gate/sweep.ts removes its row) is never decided, since no call would run.
 */
export type DecideInput = { requestId: string; response: unknown; responder: Responder; role: Role; live?: (workflowId: string, runId: string) => Promise<boolean> }

/** How decideInput ended: the request's outcome, or `workflow` when its workflow holds the entry (no `gate`). */
export type Decided = { kind: 'approval' | 'answer'; outcome: InputOutcome } | 'workflow'

/**
 * Decides a request whose row is its record (R5, plan Step 9.7, decision Q7), as the workflow's
 * `respond` handler does for an entry it holds: under the row's lock, the gate's validator against
 * the row's `gate` (state `resolving` once the row is past its timer, which the workflow is about
 * to write), then the row removed and its resolution written (`resolved`) in the same
 * transaction. The workflow is woken only after (gate/durable.ts), and a lost wake is made up by
 * its timer's read. A missing row is RespondRefusal `stale` (ended, or never asked): the route
 * then says how it ended; so is a row whose run has ended (`live`).
 *
 * A decision that commits while the workflow is cancelling the entry (a Stop, the turn's end:
 * the workflow's `resolving`, its row not yet gone) stands: the row decides, the cancel's
 * resolve_input then finds it gone and the workflow ends the entry as recorded. The turn that
 * was stopped does not run the call, as with an approval that lands just before a Stop; the
 * respond Update refused it `resolving` instead, a window the record no longer has.
 */
export async function decideInput(sql: Sql, args: DecideInput): Promise<Decided> {
  return await sql.begin(async (tx) => {
    await lockSessionOf(tx, args.requestId)
    const [row] = await tx<(PendingRow & { gate: GateRecord | null; expired: boolean; workflow_id: string; workflow_run_id: string })[]>`
      SELECT *, created_at::text AS created_at_text, expires_at <= now() AS expired
      FROM ai_pending_input WHERE request_id = ${args.requestId} FOR UPDATE`
    if (row === undefined) throw new RespondRefusal('stale', `no pending input ${args.requestId}: it is stale or was never asked`)
    if (row.gate === null) return 'workflow' as const
    const valid = validateRespond(
      { ...row.gate, id: row.request_id, kind: row.kind, state: row.expired ? 'resolving' : 'pending' },
      { requestId: args.requestId, response: args.response, responder: { kind: args.responder.kind as Owner['kind'], id: args.responder.id }, role: args.role },
    )
    // After the validator, so a response it refuses costs no call to Temporal.
    if (args.live && !(await args.live(row.workflow_id, row.workflow_run_id))) {
      throw new RespondRefusal('stale', `no pending input ${args.requestId}: its session's workflow run has ended`)
    }
    const outcome: InputOutcome = 'decision' in valid ? (valid.decision === 'approve' ? 'approved' : 'denied') : 'answered'
    const response = 'decision' in valid ? { decision: valid.decision } : { answers: [...valid.answers] }
    await tx`DELETE FROM ai_pending_input WHERE request_id = ${args.requestId}`
    await resolved(tx, row, outcome, args.responder, response, null, responseHash(args.response))
    return { kind: row.kind, outcome }
  })
}

/** A request's resolution as its response row records it: what the workflow's `gate.read_input` returns. */
export type InputRecord = { outcome: InputOutcome; response: unknown; responder: Responder; reason: string | null }

/** The response row of `requestId`, or null while the request is open (or was never asked). */
export async function readInput(sql: Sql, args: { requestId: string }): Promise<InputRecord | null> {
  const [row] = await sql<InputRecord[]>`
    SELECT outcome, response, responder, reason FROM ai_input_responses WHERE request_id = ${args.requestId}`
  return row ?? null
}
