import type { Sql } from 'postgres'
import { event, type InputEntry, type InputOutcome, type Owner, type QuestionView, type ServerEvent } from '../sessions/protocol.js'

// The durable entries' projection, `ai_pending_input` (durable-agents spec §6.6, "Two
// reads, two sources"): what GET /api/v1/ai/pending-input, the badge's read, unions
// with the classic stores in one Postgres read, never a Query per workflow. The
// agent-durable worker's open_input / resolve_input write it; this only reads.
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

/** A session as the gate needs it: its mode, and who owns and started it. */
export type GateSession = {
  mode: 'classic' | 'durable'
  owner: Owner
  creator: Pick<Owner, 'kind' | 'id'>
}

type Row = {
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

export class PendingProjection {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async entries(filter: ProjectionFilter = {}): Promise<PendingInputEntry[]> {
    const sql = this.#sql
    const owner = filter.ownedBy
    const rows = await sql<Row[]>`
      SELECT p.request_id, p.kind, p.session_id, p.tool, p.summary, p.input_hash, p.prompt, p.requested_by,
             p.responders, p.attention, p.flow_run_id, p.hop, p.outside_reason, p.created_at, p.expires_at, (p.expires_at <= now()) AS expiring
      FROM ai_pending_input p JOIN ai_sessions s ON s.id = p.session_id
      WHERE (${filter.sessionId ?? null}::uuid IS NULL OR p.session_id = ${filter.sessionId ?? null}::uuid)
        AND (${owner === undefined}
             -- A flow run's entries are the person's (#2482, spec 2026-10-11 §4.2): no grant sees them.
             OR (${filter.allApprovals === true} AND p.kind = 'approval' AND p.flow_run_id IS NULL)
             OR (s.owner_kind = ${owner?.kind ?? null} AND s.owner_id = ${owner?.id ?? null})
             OR (s.creator_kind = ${owner?.kind ?? null} AND s.creator_id = ${owner?.id ?? null}))
        AND (${filter.ownAnswers !== true} OR p.kind = 'approval' OR p.flow_run_id IS NOT NULL OR s.owner_kind = 'browser')
      ORDER BY p.created_at, p.request_id
      LIMIT 500`
    return rows.map((r) => ({
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
    }))
  }

  /** Which of `requestIds` are a flow run's (`inRun` for many, in one query, #2863). */
  async inRunOf(requestIds: readonly string[]): Promise<Set<string>> {
    if (requestIds.length === 0) return new Set()
    const rows = await this.#sql<{ request_id: string }[]>`
      SELECT request_id FROM ai_pending_input WHERE request_id = ANY(${this.#sql.array([...requestIds])}) AND flow_run_id IS NOT NULL`
    return new Set(rows.map((r) => r.request_id))
  }

  /** Whether the entry is a flow run's (`flow_run_id`): in its run's queue, the person's (#2482). */
  async inRun(requestId: string): Promise<boolean> {
    const [row] = await this.#sql<{ n: number }[]>`
      SELECT 1 AS n FROM ai_pending_input WHERE request_id = ${requestId} AND flow_run_id IS NOT NULL`
    return row !== undefined
  }

  /**
   * Whether an AgentSession session has a pending `tab_disconnected` entry (#5003): its turn waits
   * for its tab. Only `runtime = 'unified'`: DurableSession's workflow has no `tab_reconnected`.
   */
  async waitsForTab(sessionId: string): Promise<boolean> {
    const [row] = await this.#sql<{ n: number }[]>`
      SELECT 1 AS n FROM ai_pending_input p JOIN ai_sessions s ON s.id = p.session_id AND s.runtime = 'unified'
      WHERE p.session_id = ${sessionId}::uuid AND p.attention->>'reason' = 'tab_disconnected' LIMIT 1`
    return row !== undefined
  }

  /**
   * Which of `requestIds` have ended in their record (`ai_input_responses`), in one query: an
   * entry the route decided (R5) that its workflow has not yet read is listed by it no longer.
   */
  async decidedOf(requestIds: readonly string[]): Promise<Set<string>> {
    if (requestIds.length === 0) return new Set()
    const rows = await this.#sql<{ request_id: string }[]>`
      SELECT request_id FROM ai_input_responses WHERE request_id = ANY(${this.#sql.array([...requestIds])})`
    return new Set(rows.map((r) => r.request_id))
  }

  /** What the gate recorded for a resolved request (`ai_input_responses`), or undefined. */
  async recorded(requestId: string): Promise<RecordedInput | undefined> {
    const [row] = await this.#sql<RecordedInput[]>`
      SELECT kind, outcome, response, response_hash AS "responseHash", responder, reason FROM ai_input_responses WHERE request_id = ${requestId}`
    return row
  }

  /** An open entry's trace (#5041): its tool, when it was asked, and the parked call's span context. */
  async parkTrace(requestId: string): Promise<{ tool: string; createdAt: Date; traceparent: string | null } | undefined> {
    const [row] = await this.#sql<{ tool: string; created_at: Date; traceparent: string | null }[]>`
      SELECT tool, created_at, traceparent FROM ai_pending_input WHERE request_id = ${requestId}`
    return row ? { tool: row.tool, createdAt: row.created_at, traceparent: row.traceparent } : undefined
  }

  /**
   * The decision's span on the entry, before the workflow is asked (#5041): resolve_input carries
   * it to the response row, which the waiting turn reads, so the turn goes on as its child. A
   * refused decision's is replaced by the next one's.
   */
  async markDecision(requestId: string, traceparent: string): Promise<void> {
    await this.#sql`UPDATE ai_pending_input SET decision_traceparent = ${traceparent} WHERE request_id = ${requestId}`
  }

  /** The session, or undefined when there is none. */
  async session(id: string): Promise<GateSession | undefined> {
    const [row] = await this.#sql<
      { mode: GateSession['mode']; owner_kind: Owner['kind']; owner_id: string; owner_label: string; creator_kind: Owner['kind']; creator_id: string }[]
    >`SELECT mode, owner_kind, owner_id, owner_label, creator_kind, creator_id FROM ai_sessions WHERE id = ${id}`
    return row
      ? {
          mode: row.mode,
          owner: { kind: row.owner_kind, id: row.owner_id, label: row.owner_label },
          creator: { kind: row.creator_kind, id: row.creator_id },
        }
      : undefined
  }
}
