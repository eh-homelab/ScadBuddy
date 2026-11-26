import type { Sql } from 'postgres'
import {
  entryOf,
  type GateSession,
  type ParkTrace,
  type PendingInputEntry,
  type PendingReads,
  type PendingRow,
  type ProjectionFilter,
  type RecordedInput,
} from '../../../src/gate/projection.js'
import type { Owner } from '../../../src/sessions/protocol.js'

// Tests only: the projection's reads over the test database, the SQL the agent ran before the
// API took area `pending-input` (#5299 step 12). src holds no SQL; the API's are
// tools/go/core/internal/pending.

export class SqlPendingProjection implements PendingReads {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async entries(filter: ProjectionFilter = {}): Promise<PendingInputEntry[]> {
    const sql = this.#sql
    const owner = filter.ownedBy
    const rows = await sql<PendingRow[]>`
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
    return rows.map(entryOf)
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
  async parkTrace(requestId: string): Promise<ParkTrace | undefined> {
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
      { owner_kind: Owner['kind']; owner_id: string; owner_label: string; creator_kind: Owner['kind']; creator_id: string }[]
    >`SELECT owner_kind, owner_id, owner_label, creator_kind, creator_id FROM ai_sessions WHERE id = ${id}`
    return row
      ? {
          owner: { kind: row.owner_kind, id: row.owner_id, label: row.owner_label },
          creator: { kind: row.creator_kind, id: row.creator_id },
        }
      : undefined
  }
}
