import { event, type InputEntry, type InputOutcome, type Owner, type QuestionView, type ServerEvent } from '../sessions/protocol.js'

// The durable entries' record, `ai_pending_input` (durable-agents spec §6.6, "Two reads,
// one source"): what GET /api/v1/ai/pending-input, the badge's read, and one session's
// read (#7311) union with the classic stores, never a Query of a workflow. The gate's
// open_input / resolve_input and a decision write it; this only reads.
//
// An entry past its `expires_at` is still listed while its run's worker is down (it
// resolves it when it returns), marked `expiring`: the badge does not count it.

// Rows outside ai_pending_input in the gate's one entry shape (folded here from gate/classic.ts,
// plan Step 9.7): a session-less MCP prepare, an `ai_approvals` row (`approval:<id>`), and a
// `done` summary, an `ai_questions` row (`question:<id>`). The pending-input reads list them,
// and their writers log `input.requested` / `input.resolved` with them.

/** The columns of an approval its entry needs. */
export type ApprovalShape = {
  id: string
  sessionId: string | null
  tool: string
  inputSummary: string
  inputHash: string
  requestedBy: Owner
  createdAt: string
  expiresAt: string
}

/** The columns of a question (a done summary) its entry needs. */
export type QuestionShape = {
  id: string
  sessionId: string
  tool: string
  questions: readonly Pick<QuestionView, 'question'>[]
  attentionReason: string | null
  onTimeout: string | null
  summary: string | null
  createdAt: string
  expiresAt: string | null
}

export function approvalEntry(a: ApprovalShape): InputEntry {
  return {
    id: `approval:${a.id}`,
    kind: 'approval',
    session_id: a.sessionId,
    tool: a.tool,
    summary: a.inputSummary,
    input_hash: a.inputHash,
    prompt: '',
    requested_by: a.requestedBy,
    responders: ['browser', 'grant'],
    created_at: a.createdAt,
    expires_at: a.expiresAt,
  }
}

export function questionEntry(q: QuestionShape): InputEntry {
  return {
    id: `question:${q.id}`,
    kind: 'answer',
    session_id: q.sessionId,
    tool: q.tool,
    summary: '',
    input_hash: null,
    prompt: q.questions.map((v) => v.question).join('\n'),
    requested_by: null,
    responders: ['browser'],
    created_at: q.createdAt,
    expires_at: q.expiresAt,
    ...(q.attentionReason
      ? { attention: { reason: q.attentionReason, on_timeout: q.onTimeout, ...(q.summary === null ? {} : { summary: q.summary }) } }
      : {}),
  }
}

/** `input.requested` for an entry of `sessionId`. */
export function inputRequested(sessionId: string, entry: InputEntry): ServerEvent {
  return event({ type: 'input.requested', sessionId, entry })
}

/** `input.resolved`; a question's `reconnected` (#815 §2) is a cancellation by the system, with its reason. */
export function inputResolved(
  sessionId: string,
  id: string,
  kind: InputEntry['kind'],
  outcome: InputOutcome | 'reconnected',
  reason?: string | null,
): ServerEvent {
  return event({
    type: 'input.resolved',
    sessionId,
    id,
    kind,
    outcome: outcome === 'reconnected' ? 'cancelled' : outcome,
    ...(reason ? { reason } : {}),
  })
}

export type PendingInputEntry = InputEntry & {
  /** A durable entry past its timer whose worker has not yet resolved it; not counted as waiting. */
  expiring?: true
  /**
   * A flow run's entry (#2482, spec 2026-10-11 §4.5): the run whose queue it is in, where
   * it waits, and why the grant did not admit it. At hop `parent` it waits for the
   * session that started the run, and the badge does not count it.
   */
  run?: { id: string; name?: string; hop: 'parent' | 'person' | null; reason: string | null; starter_session?: string | null }
}

/** A session as the gate needs it: who owns and started it. */
export type GateSession = {
  owner: Owner
  creator: Pick<Owner, 'kind' | 'id'>
}

/** A row of `ai_pending_input` as the entries' read selects it (the SQL's, or the API's, its dates parsed). */
export type PendingRow = {
  request_id: string
  kind: 'approval' | 'answer'
  session_id: string
  tool: string
  summary: string
  input_hash: string | null
  prompt: string
  requested_by: Owner | null
  responders: ('browser' | 'grant')[]
  attention: { reason: string; on_timeout: string | null } | null
  flow_run_id: string | null
  hop: 'parent' | 'person' | null
  outside_reason: string | null
  created_at: Date
  expires_at: Date
  expiring: boolean
}

/** Which entries: of one session; of sessions a principal owns or started; approvals of any session. */
export type ProjectionFilter = {
  sessionId?: string
  /** Only sessions this principal owns or started… */
  ownedBy?: Pick<Owner, 'kind' | 'id'>
  /** …except approvals, which a grant holder sees in every session. */
  allApprovals?: boolean
  /**
   * With no `ownedBy` (the browser user's read): the answers of the sessions the user owns only,
   * as classic's QuestionService.listPending lists them (#1218). An entry that outlived a handoff
   * is not the user's to answer. Approvals and a flow run's entries stay listed.
   */
  ownAnswers?: boolean
}

export type RecordedInput = {
  kind: 'approval' | 'answer'
  outcome: InputOutcome
  response: unknown
  responseHash: string | null
  responder: { kind?: string; id?: string }
  /** Why it ended, when it was not answered or decided (a cancel's, an expiry's). */
  reason: string | null
}

/** An open entry's trace (#5041): its tool, when it was asked, and the parked call's span context. */
export type ParkTrace = { tool: string; createdAt: Date; traceparent: string | null }

/**
 * The projection's reads and its one write, through the API's CRUD routes, area `pending-input`
 * (gate/crudPending.ts `CrudPending`).
 */
export interface PendingReads {
  entries(filter?: ProjectionFilter): Promise<PendingInputEntry[]>
  inRunOf(requestIds: readonly string[]): Promise<Set<string>>
  inRun(requestId: string): Promise<boolean>
  waitsForTab(sessionId: string): Promise<boolean>
  decidedOf(requestIds: readonly string[]): Promise<Set<string>>
  recorded(requestId: string): Promise<RecordedInput | undefined>
  parkTrace(requestId: string): Promise<ParkTrace | undefined>
  markDecision(requestId: string, traceparent: string): Promise<void>
  session(id: string): Promise<GateSession | undefined>
}

/** The entry of a row. */
export function entryOf(r: PendingRow): PendingInputEntry {
  return {
    id: r.request_id,
    kind: r.kind,
    session_id: r.session_id,
    tool: r.tool,
    summary: r.summary,
    input_hash: r.input_hash,
    prompt: r.prompt,
    requested_by: r.requested_by,
    responders: r.responders,
    created_at: r.created_at.toISOString(),
    expires_at: r.expires_at.toISOString(),
    ...(r.attention ? { attention: r.attention } : {}),
    ...(r.expiring ? { expiring: true as const } : {}),
    ...(r.flow_run_id ? { run: { id: r.flow_run_id, hop: r.hop, reason: r.outside_reason } } : {}),
  }
}
