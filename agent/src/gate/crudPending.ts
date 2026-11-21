import { wellFormed } from '../api/crudRead.js'
import { type CrudTarget, crudOf, qs, UUID_SEGMENT } from '../api/crudStores.js'
import type { Owner } from '../sessions/protocol.js'
import { type Logged, REQUEST_ID_MAX } from './crudStore.js'
import {
  entryOf,
  type GateSession,
  type ParkTrace,
  type PendingInputEntry,
  type PendingReads,
  type PendingRow,
  type ProjectionFilter,
  type RecordedInput,
} from './projection.js'

// The gate's pending input and responses beyond its writers, through the API's CRUD routes (#5299
// step 10; backend api/crud_agent_pending.py, agent_data/pending_input.py), area `pending-input`:
// the projection's reads (gate/projection.ts), the orphan sweep (gate/sweep.ts, each cancel one
// transaction on the API with its events, status and audit row, the batches it appended answered
// as `logged`), a durable turn's response read and its parked call's span mark (sessions/
// turnHost.ts; the mark fenced by the turn's placement on the API), and a call's recorded approval
// (temporal/toolActivities.ts `PgApprovalRecords`). Each caller uses its own SQL until the API
// lists the area (D9).
//
// A request id travels in the body or the query string, never in the path. One longer than the
// API takes (`REQUEST_ID_MAX`) is never sent: no row can hold it, so it reads as not found.

/** The most request ids one `in-run` or `decided` call takes (backend `IDS_MAX`). */
export const IDS_MAX = 1000
/** The longest input hash the approval read takes (backend `NAME_MAX`). */
export const NAME_MAX = 1024

/** A run whose rows the sweep describes. */
export type SweepRun = { workflow_id: string; workflow_run_id: string }

/** Whose entries an orphan cancel ends: one ended run's, or a session's. */
export type Orphans = { workflowId: string; runId: string } | { sessionId: string }

/** The response row a durable turn waits for, with the decision's span context. */
export type HeldResponse = {
  outcome: string
  response: { answers?: unknown; reconnected?: unknown } | null
  reason: string | null
  responder: { kind?: string } | null
  decision_traceparent: string | null
}

/** The turn placement a trace mark is made for: it lands only while this holds the session. */
export type TurnFence = { sessionId: string; attempt: string; turnId: string }

/** The orphan sweep's routes, as gate/sweep.ts calls them. */
export interface SweepRoutes {
  runs(minAgeS: number, recheckS: number): Promise<SweepRun[]>
  checked(workflowId: string, runId: string): Promise<void>
  cancel(of: Orphans, reason: string): Promise<{ removed: number; logged: Logged[] }>
}

/** A durable turn's routes, as sessions/turnHost.ts calls them. */
export interface TurnRoutes {
  response(requestId: string): Promise<HeldResponse | undefined>
  markPark(requestId: string, traceparent: string, fence: TurnFence): Promise<boolean>
}

/** A call's recorded approval, as temporal/toolActivities.ts reads it. */
export interface ApprovalRoutes {
  /** The responder of the request's approval given for this call (its input hash), or null. */
  approval(requestId: string, inputHash: string): Promise<{ kind?: unknown; id?: unknown } | null>
}

const fits = (requestId: string): boolean => requestId.length > 0 && requestId.length <= REQUEST_ID_MAX

type WireRow = Omit<PendingRow, 'created_at' | 'expires_at'> & { created_at: string; expires_at: string }
type WireRecorded = {
  kind: 'approval' | 'answer'
  outcome: RecordedInput['outcome']
  response: unknown
  response_hash: string | null
  responder: { kind?: string; id?: string }
  reason: string | null
  decision_traceparent: string | null
}

/** The `pending-input` area's routes. */
export class CrudPending implements PendingReads, SweepRoutes, TurnRoutes, ApprovalRoutes {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async entries(filter: ProjectionFilter = {}): Promise<PendingInputEntry[]> {
    const owner: Pick<Owner, 'kind' | 'id'> | undefined = filter.ownedBy
    const body = {
      session_id: filter.sessionId ?? null,
      owned_by: owner === undefined ? null : { kind: owner.kind, id: owner.id },
      all_approvals: filter.allApprovals === true,
      own_answers: filter.ownAnswers === true,
    }
    const { entries } = await this.#call<{ entries: WireRow[] }>('the pending entries', '/pending-input/entries', { body: wellFormed(body) })
    return entries.map((r) => entryOf({ ...r, created_at: new Date(r.created_at), expires_at: new Date(r.expires_at) }))
  }

  async #found(step: string, path: string, requestIds: readonly string[]): Promise<Set<string>> {
    const ids = [...new Set(requestIds.filter(fits))]
    const found = new Set<string>()
    for (let i = 0; i < ids.length; i += IDS_MAX) {
      const body = { request_ids: ids.slice(i, i + IDS_MAX) }
      const answer = await this.#call<{ request_ids: string[] }>(step, path, { body: wellFormed(body) })
      for (const id of answer.request_ids) found.add(id)
    }
    return found
  }

  async inRunOf(requestIds: readonly string[]): Promise<Set<string>> {
    return await this.#found("a flow run's entries", '/pending-input/in-run', requestIds)
  }

  async inRun(requestId: string): Promise<boolean> {
    return (await this.inRunOf([requestId])).has(requestId)
  }

  async waitsForTab(sessionId: string): Promise<boolean> {
    if (!UUID_SEGMENT.test(sessionId)) return false
    const { waits } = await this.#call<{ waits: boolean }>('a tab wait', `/pending-input/tab-wait${qs({ session_id: sessionId })}`)
    return waits
  }

  async decidedOf(requestIds: readonly string[]): Promise<Set<string>> {
    return await this.#found('the decided entries', '/pending-input/decided', requestIds)
  }

  async #recorded(requestId: string): Promise<WireRecorded | undefined> {
    if (!fits(requestId)) return undefined
    const { recorded } = await this.#call<{ recorded: WireRecorded | null }>("a request's response", `/pending-input/recorded${qs({ request_id: requestId })}`)
    return recorded ?? undefined
  }

  async recorded(requestId: string): Promise<RecordedInput | undefined> {
    const r = await this.#recorded(requestId)
    return r && { kind: r.kind, outcome: r.outcome, response: r.response, responseHash: r.response_hash, responder: r.responder, reason: r.reason }
  }

  async parkTrace(requestId: string): Promise<ParkTrace | undefined> {
    if (!fits(requestId)) return undefined
    const { trace } = await this.#call<{ trace: { tool: string; created_at: string; traceparent: string | null } | null }>(
      "an entry's trace",
      `/pending-input/park-trace${qs({ request_id: requestId })}`,
    )
    return trace ? { tool: trace.tool, createdAt: new Date(trace.created_at), traceparent: trace.traceparent } : undefined
  }

  async markDecision(requestId: string, traceparent: string): Promise<void> {
    if (!fits(requestId)) return
    await this.#call("a decision's span", '/pending-input/decision-trace', { body: wellFormed({ request_id: requestId, traceparent }) })
  }

  async session(id: string): Promise<GateSession | undefined> {
    type Wire = { owner_kind: Owner['kind']; owner_id: string; owner_label: string; creator_kind: Owner['kind']; creator_id: string }
    const { session } = await this.#call<{ session: Wire | null }>('a session', `/pending-input/session${qs({ session_id: id })}`)
    return session
      ? { owner: { kind: session.owner_kind, id: session.owner_id, label: session.owner_label }, creator: { kind: session.creator_kind, id: session.creator_id } }
      : undefined
  }

  async runs(minAgeS: number, recheckS: number): Promise<SweepRun[]> {
    const { runs } = await this.#call<{ runs: SweepRun[] }>(
      'the runs to sweep',
      `/pending-input/sweep/runs${qs({ min_age_s: String(minAgeS), recheck_s: String(recheckS) })}`,
    )
    return runs
  }

  async checked(workflowId: string, runId: string): Promise<void> {
    await this.#call('a run found open', '/pending-input/sweep/checked', { body: wellFormed({ workflow_id: workflowId, run_id: runId }) })
  }

  async cancel(of: Orphans, reason: string): Promise<{ removed: number; logged: Logged[] }> {
    const body = 'sessionId' in of ? { session_id: of.sessionId, reason } : { run: { workflow_id: of.workflowId, run_id: of.runId }, reason }
    return await this.#call('the orphaned entries cancelled', '/pending-input/orphans/cancel', { body: wellFormed(body) })
  }

  async response(requestId: string): Promise<HeldResponse | undefined> {
    const r = await this.#recorded(requestId)
    return r && { outcome: r.outcome, response: r.response as HeldResponse['response'], reason: r.reason, responder: r.responder, decision_traceparent: r.decision_traceparent }
  }

  async markPark(requestId: string, traceparent: string, fence: TurnFence): Promise<boolean> {
    if (!fits(requestId)) return false
    const body = { request_id: requestId, traceparent, session_id: fence.sessionId, attempt: fence.attempt, turn_id: fence.turnId }
    const { marked } = await this.#call<{ marked: boolean }>("a parked call's span", '/pending-input/trace', { body: wellFormed(body) })
    return marked
  }

  async approval(requestId: string, inputHash: string): Promise<{ kind?: unknown; id?: unknown } | null> {
    if (!fits(requestId) || inputHash.length === 0 || inputHash.length > NAME_MAX) return null
    const { responder } = await this.#call<{ responder: { kind?: unknown; id?: unknown } | null }>(
      "a call's approval",
      `/pending-input/approval${qs({ request_id: requestId, input_hash: inputHash })}`,
    )
    return responder
  }
}
