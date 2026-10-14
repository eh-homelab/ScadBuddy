import { randomUUID } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import type { InputEntry, InputOutcome, Owner, ServerEvent } from '../sessions/protocol.js'
import { parseRequestId } from './ids.js'

// Every write about a durable entry (durable-agents spec §6.6, "How a durable entry
// opens and closes"), ported whole from agent-durable's gate/store.py; the SQL is the
// same. Line numbers are that file's at the port:
//   _hop :139, _cap :159, _entry :163, _rows :183  -> hop, cap, entryOf, the tagged queries
//   _append :191 (events, then the bus NOTIFY on commit)  -> append
//   _resolved :236, _audit :324                   -> resolved, audit
//   openInput :368, grantInput :478, denyInput :536, resolveInput :577
//   run_grant_responder :115, flow_responder :119, _actor :132
// Python's Outcome/Kind/_WAITING/_SETTLED are InputOutcome, InputEntry['kind'] and the
// two lists below. Each function returns what Python's returns: false when nothing was
// written (already open or resolved, gone, or the grant does not hold).
//
// Beyond Python: resolveInput also sends `NOTIFY scadbuddy_input, <request id>`, in the
// transaction that deletes the row, so a waiter learns of a resolution without polling.
// The channel is new; Python does not send it.

const PROTOCOL_VERSION = 1
const PG_CHANNEL = 'scadbuddy_events'
export const INPUT_CHANNEL = 'scadbuddy_input'
const REPLICA = 'agent-durable'
const SYSTEM: Responder = { kind: 'system', id: 'scadbuddy', label: 'ScadBuddy' }
const APPROVAL_SUMMARY_MAX = 500
const WAITING = ['waiting_approval', 'waiting_input']
const SETTLED = ['idle', 'done', 'failed']

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
}

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

/** What `ai_pending_input` returns for `RETURNING *`. */
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
  expires_at: Date
  flow_run_id: string | null
  hop: 'parent' | 'person' | null
  hops: Hop[] | null
}

/** What `audit` reads of an entry: a pending row, or the grant and deny paths' stand-in. */
type Audited = {
  request_id: string
  session_id: string
  requested_by: Owner | null
  tool: string
  input_hash: string | null
  summary: string
  created_at: Date
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

/** Python slices by code point, not UTF-16 unit. */
function cut(text: string, limit: number): string {
  const points = [...text]
  return points.length <= limit ? text : points.slice(0, limit).join('')
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
  const statuses = events.flatMap((e) => (e.type === 'session.status' ? [e.status as string] : []))
  const status = statuses.at(-1)
  const kind =
    status !== undefined && WAITING.includes(status)
      ? 'session.waiting'
      : status !== undefined && SETTLED.includes(status)
        ? 'session.done'
        : 'session.message'
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
): Promise<void> {
  const sid = String(row.session_id)
  const rid = row.request_id
  const hops: Hop[] = [...(row.hops ?? [])]
  if (row.flow_run_id !== null && row.hop !== null) {
    // A run's entry keeps its way up, the resolution as its last step (§4.6).
    hops.push(hop(row.hop, HOP_ACTIONS[outcome] ?? outcome, actor(responder), reason))
  }
  await tx`
    INSERT INTO ai_input_responses
      (request_id, session_id, kind, outcome, response, responder, reason, hops)
    VALUES (${rid}, ${sid}, ${row.kind}, ${outcome},
            ${response === undefined || response === null ? null : tx.json(response as never)}::jsonb,
            ${tx.json(responder)}, ${reason}, ${tx.json(hops)})`
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
      }),
    )
  }
  events.push(
    event({ type: 'input.resolved', sessionId: sid, id: rid, kind: row.kind, outcome, ...(reason ? { reason } : {}) }),
  )
  // Back to running once nothing of the session is parked any more.
  const running = await tx`
    UPDATE ai_sessions SET status = 'running', updated_at = now()
    WHERE id = ${sid} AND status IN ('waiting_approval', 'waiting_input')
      AND NOT EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = ${sid})
    RETURNING status`
  if (running.length > 0) events.push(event({ type: 'session.status', sessionId: sid, status: 'running' }))
  await append(tx, sid, events)
  if (row.kind === 'approval') await audit(tx, row, outcome, responder, reason)
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
            ${started}, now(), GREATEST(0, (extract(epoch FROM now() - ${started}::timestamptz) * 1000)::int))`
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
    const stale = await tx<PendingRow[]>`
      DELETE FROM ai_pending_input WHERE session_id = ${args.sessionId} AND workflow_run_id <> ${args.workflowRunId}
      RETURNING *`
    for (const old of stale) await resolved(tx, old, 'cancelled', SYSTEM, null, 'reset')
  })
  return await sql.begin(async (tx) => {
    const hops = args.hop == null ? [] : [hop(args.hop, 'parked', 'flow', args.outsideReason)]
    const inserted = await tx<PendingRow[]>`
      INSERT INTO ai_pending_input
        (request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary,
         input_hash, prompt, requested_by, responders, attention, expires_at,
         flow_run_id, hop, hop_until, parent_session, parent_principal,
         outside_reason, hops)
      SELECT ${args.requestId}, ${args.sessionId}::uuid, ${args.workflowId}, ${args.workflowRunId}, ${args.kind},
             ${args.tool}, ${args.summary}, ${args.inputHash}, ${args.prompt}, ${tx.json(args.requestedBy)},
             ${tx.array([...args.responders])}::text[],
             ${args.attention == null ? null : tx.json(args.attention)}::jsonb, ${args.expiresAt}::timestamptz,
             ${args.flowRunId ?? null}, ${args.hop ?? null}, ${args.hopUntil ?? null}::timestamptz,
             ${args.parentSession ?? null}::uuid, ${args.parentPrincipal ?? null}, ${args.outsideReason ?? null},
             ${tx.json(hops)}
      -- A retried open that lands after its entry was resolved opens nothing.
      WHERE NOT EXISTS (SELECT 1 FROM ai_input_responses WHERE request_id = ${args.requestId})
      ON CONFLICT (request_id) DO NOTHING
      RETURNING *`
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
    await audit(tx, { ...stand(args), created_at: new Date() }, 'approved', responder, null)
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
    await audit(tx, { ...stand(args), created_at: new Date() }, 'denied', responder, args.reason)
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

/** Resolves the entry; false when it was gone (resolved by another, or never opened). */
export async function resolveInput(sql: Sql, args: ResolveInput): Promise<boolean> {
  return await sql.begin(async (tx) => {
    const deleted = await tx<PendingRow[]>`DELETE FROM ai_pending_input WHERE request_id = ${args.requestId} RETURNING *`
    const row = deleted[0]
    if (row === undefined) return false
    await resolved(tx, row, args.outcome, args.responder, args.response, args.reason ?? null)
    await tx`SELECT pg_notify(${INPUT_CHANNEL}, ${args.requestId})`
    return true
  })
}
