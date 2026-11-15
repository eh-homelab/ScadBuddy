import type { Sql } from 'postgres'
import { crudRead } from '../api/crudAreas.js'

// The agent's questions about the backend's flow tables (#5299 D7): `workflow_runs`,
// `workflow_run_decisions` and `flow_grants`. The backend answers each over a CRUD route of
// area `flows` (backend api/crud_agent.py, flows/agent_reads.py), with the same query.
// `SqlFlowRunReads` keeps today's SQL for an API pod older than the routes (#5299 D9): the
// agent uses it until `GET …/crud/agent/areas` lists `flows` (api/crudAreas.ts), and it goes,
// with the agent's grants on these tables, once every API pod serves the routes.
//
// The gate's own read of `flow_grants` and `flow_grant_uses` (gate/store.ts `grantInput`) is not
// here: it holds the grant row's share lock across the agent's own writes, and moves with the
// gate (#5299 step 10).

export type QueueHop = { hop: string; action: string; actor: string; at: string; reason?: string | null }

/** A run's parked call (`workflow_runs.waiting_on`), as the backend's projection wrote it. */
export type WaitingOn = {
  call_id: string
  kind: 'approval' | 'answer'
  fn: string
  prompt?: string | null
  since?: string | null
  hop?: 'parent' | 'person' | null
  hop_until?: string | null
  reason?: string | null
  proposed?: boolean
  hops?: QueueHop[]
}
export type StepRow = { seq: number; fn: string; call_id: string; status: string; session_id?: string | null }

/** `started_by`'s fields; only a string is anyone's. */
export type StartedBy = { kind?: unknown; principal?: unknown; session?: unknown }

export type RunStarter = {
  /** Running or waiting, and the execution is not one a Reset replaced. */
  open: boolean
  /** The execution is neither the row's nor one a Reset replaced. */
  unrecorded: boolean
  started_by: StartedBy
}

export type DecisionChecks = { approved: boolean; approvedUnresolved: boolean; byPerson: boolean; lapsed: boolean }

export type QueueRun = {
  id: string
  name: string
  status: string
  workflow_run_id: string
  starter_session: string | null
  waiting_on: WaitingOn[]
  steps: StepRow[]
}

/** Where a page of `parentHopApprovals` goes on from: opaque. */
export type ParentHopCursor = { after_updated_at: string; after_id: string }

export type ParentHopRun = {
  id: string
  workflow_run_id: string
  session: unknown
  principal: unknown
  /** Its parked approvals at hop `parent` whose hop is live by Postgres's clock. */
  calls: WaitingOn[]
  steps: StepRow[]
}

export interface FlowRunReads {
  /** Who started run `runId`, under its row's share lock; undefined for no such run. */
  starter(runId: string, execution: string): Promise<RunStarter | undefined>
  /** What the run's decisions say of the call `requestId` approved with `inputHash`. */
  decisions(requestId: string, inputHash: string): Promise<DecisionChecks>
  /** Whether run grant `grantId` is approved, not revoked and not expired. */
  grantInForce(grantId: string): Promise<boolean>
  /** Run `runId` and its grant while it is requested; undefined for no such run (or no flows here). */
  queue(runId: string): Promise<(QueueRun & { requested_grant: { id: string; created_at: Date } | null }) | undefined>
  /** The open runs with parked calls, the 200 most recently moved first. */
  waitingRuns(): Promise<(QueueRun & { updated_at: Date })[]>
  /** Run `runId`'s steps; none for no such run. */
  steps(runId: string): Promise<StepRow[]>
  /** The waiting runs with a live parked approval at hop `parent`, oldest move first, `limit` at most. */
  parentHopApprovals(after: ParentHopCursor | null, limit: number): Promise<{ runs: ParentHopRun[]; next: ParentHopCursor | null }>
  /** Which of `requestIds` still wait at hop `parent`; undefined when this deployment has no flows. */
  stillAtParentHop(requestIds: readonly string[]): Promise<Set<string> | undefined>
}

/** The most request ids one `stillAtParentHop` read asks about (the route's bound). */
export const STILL_WAITING_BATCH = 500

/** A deployment without flows has no `workflow_runs`. */
function noFlows(err: unknown): boolean {
  return (err as { code?: string }).code === '42P01'
}

type RunRow = QueueRun & { started_by: StartedBy }

function queueRun(row: Omit<RunRow, 'starter_session'> & { started_by: StartedBy }): QueueRun {
  const session = row.started_by.session
  return {
    id: row.id,
    name: row.name,
    status: row.status,
    workflow_run_id: row.workflow_run_id,
    starter_session: typeof session === 'string' ? session : null,
    waiting_on: row.waiting_on,
    steps: Array.isArray(row.steps) ? row.steps : [],
  }
}

/** Today's SQL, on the agent's own connection (see above). */
export class SqlFlowRunReads implements FlowRunReads {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async starter(runId: string, execution: string): Promise<RunStarter | undefined> {
    // FOR SHARE needs UPDATE on workflow_runs, not only SELECT (flowRuns.ts).
    const [row] = await this.#sql<RunStarter[]>`
      SELECT started_by,
             status IN ('running', 'waiting') AND NOT (${execution} = ANY(superseded_run_ids)) AS open,
             workflow_run_id IS DISTINCT FROM ${execution} AND NOT (${execution} = ANY(superseded_run_ids)) AS unrecorded
        FROM workflow_runs WHERE id = ${runId}
         FOR SHARE`
    return row
  }

  async decisions(requestId: string, inputHash: string): Promise<DecisionChecks> {
    const approved = this.#sql`
      FROM workflow_run_decisions d
      WHERE d.request_id = ${requestId} AND d.kind = 'approval' AND d.outcome = 'approved'
        AND d.input_hash = ${inputHash}`
    // A run grant's approval holds only while the grant is in force (#2481).
    const inForce = this.#sql`EXISTS (
      SELECT 1 FROM flow_grants g
      WHERE 'grant:' || g.id = d.responder AND g.status = 'approved'
        AND g.revoked_at IS NULL AND now() < g.expires_at)`
    const [row] = await this.#sql<{ approved: boolean; approved_unresolved: boolean; by_person: boolean; lapsed: boolean }[]>`
      SELECT
        EXISTS (SELECT 1 ${approved} AND (d.responder NOT LIKE 'grant:%' OR ${inForce})) AS approved,
        EXISTS (SELECT 1 ${approved}
                AND (d.responder LIKE 'grant:%' OR d.response ? 'narrowed_by')
                AND (d.responder NOT LIKE 'grant:%' OR ${inForce})) AS approved_unresolved,
        EXISTS (SELECT 1 ${approved} AND d.responder = 'browser') AS by_person,
        EXISTS (
          SELECT 1 FROM workflow_run_decisions d
          JOIN flow_grants g ON 'grant:' || g.id = d.responder
          WHERE d.request_id = ${requestId} AND d.kind = 'approval' AND d.outcome = 'approved'
            AND d.input_hash = ${inputHash}
            AND (g.status <> 'approved' OR g.revoked_at IS NOT NULL OR g.expires_at IS NULL OR now() >= g.expires_at)) AS lapsed`
    return {
      approved: row?.approved === true,
      approvedUnresolved: row?.approved_unresolved === true,
      byPerson: row?.by_person === true,
      lapsed: row?.lapsed === true,
    }
  }

  async grantInForce(grantId: string): Promise<boolean> {
    const rows = await this.#sql`
      SELECT 1 FROM flow_grants
      WHERE id = ${grantId} AND status = 'approved' AND revoked_at IS NULL AND now() < expires_at`
    return rows.length > 0
  }

  async queue(runId: string): Promise<(QueueRun & { requested_grant: { id: string; created_at: Date } | null }) | undefined> {
    let run: RunRow | undefined
    try {
      ;[run] = await this.#sql<RunRow[]>`
        SELECT id, name, status, workflow_run_id, started_by, waiting_on, steps FROM workflow_runs WHERE id = ${runId}`
    } catch (err) {
      if (noFlows(err)) return undefined
      throw err
    }
    if (!run) return undefined
    const [grant] = await this.#sql<{ id: string; created_at: Date }[]>`
      SELECT id, created_at FROM flow_grants WHERE run_id = ${run.id} AND status = 'requested'`
    return { ...queueRun(run), requested_grant: grant ?? null }
  }

  async waitingRuns(): Promise<(QueueRun & { updated_at: Date })[]> {
    let runs: (RunRow & { updated_at: Date })[]
    try {
      runs = await this.#sql<(RunRow & { updated_at: Date })[]>`
        SELECT id, name, status, workflow_run_id, started_by, waiting_on, steps, updated_at FROM workflow_runs
         WHERE status IN ('running', 'waiting') AND jsonb_array_length(waiting_on) > 0
         ORDER BY updated_at DESC LIMIT 200`
    } catch (err) {
      if (noFlows(err)) return []
      throw err
    }
    return runs.map((run) => ({ ...queueRun(run), updated_at: run.updated_at }))
  }

  async steps(runId: string): Promise<StepRow[]> {
    try {
      const [r] = await this.#sql<{ steps: StepRow[] }[]>`SELECT steps FROM workflow_runs WHERE id = ${runId}`
      return r && Array.isArray(r.steps) ? r.steps : []
    } catch (err) {
      if (noFlows(err)) return []
      throw err
    }
  }

  async parentHopApprovals(after: ParentHopCursor | null, limit: number): Promise<{ runs: ParentHopRun[]; next: ParentHopCursor | null }> {
    // One predicate (#3494 review): a parent-hop approval whose hop has not timed out by
    // Postgres's clock (#3411); a malformed hop_until is that call skipped, never a failed scan.
    const live = this.#sql`w->>'hop' = 'parent' AND w->>'kind' = 'approval'
      AND CASE WHEN pg_input_is_valid(w->>'hop_until', 'timestamptz')
               THEN (w->>'hop_until')::timestamptz > now() ELSE false END`
    let rows: (Omit<ParentHopRun, 'session' | 'principal'> & { started_by: StartedBy; cursor: string })[]
    try {
      rows = await this.#sql`
        SELECT id, workflow_run_id, started_by, steps,
               to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS cursor,
               (SELECT coalesce(jsonb_agg(w), '[]'::jsonb) FROM jsonb_array_elements(r.waiting_on) w WHERE ${live}) AS calls
        FROM workflow_runs r
        WHERE status = 'waiting' AND waiting_on @> '[{"hop": "parent"}]'::jsonb
          AND EXISTS (SELECT 1 FROM jsonb_array_elements(r.waiting_on) w WHERE ${live})
          -- The cursor is sent as text: a timestamptz parameter goes through a JS Date, which
          -- drops its microseconds, and the page would start at the same run again.
          AND (${after?.after_updated_at ?? null}::text IS NULL
               OR (updated_at, id) > (${after?.after_updated_at ?? null}::text::timestamptz, ${after?.after_id ?? null}::text))
        ORDER BY updated_at, id LIMIT ${limit}`
    } catch (err) {
      if (noFlows(err)) return { runs: [], next: null }
      throw err
    }
    const runs = rows.map((r) => ({
      id: r.id,
      workflow_run_id: r.workflow_run_id,
      session: r.started_by.session,
      principal: r.started_by.principal,
      calls: r.calls,
      steps: Array.isArray(r.steps) ? r.steps : [],
    }))
    const last = rows.length === limit ? rows.at(-1) : undefined
    return { runs, next: last ? { after_updated_at: last.cursor, after_id: last.id } : null }
  }

  async stillAtParentHop(requestIds: readonly string[]): Promise<Set<string> | undefined> {
    if (requestIds.length === 0) return new Set()
    try {
      const rows = await this.#sql<{ id: string }[]>`
        SELECT x.id FROM unnest(${requestIds as string[]}::text[]) x(id) WHERE EXISTS (
          SELECT 1 FROM workflow_runs r CROSS JOIN LATERAL jsonb_array_elements(r.waiting_on) w
           WHERE r.id = split_part(x.id, ':', 2) AND r.status = 'waiting' AND w ->> 'hop' = 'parent'
             AND x.id = 'flow:' || r.id || ':' || r.workflow_run_id || ':' || (w ->> 'call_id'))`
      return new Set(rows.map((r) => r.id))
    } catch (err) {
      if (noFlows(err)) return undefined
      throw err
    }
  }
}

type WireRun = {
  id: string
  name: string
  status: string
  workflow_run_id: string
  starter_session: string | null
  waiting_on: WaitingOn[]
  steps: StepRow[]
}

const id = encodeURIComponent

/** The backend's CRUD routes of area `flows` (see above). */
export class CrudFlowRunReads implements FlowRunReads {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  #get<T>(step: string, path: string, query?: Record<string, string>): Promise<T> {
    return crudRead<T>(this.#fetch, this.#base, step, query ? `${path}?${new URLSearchParams(query)}` : path)
  }

  async starter(runId: string, execution: string): Promise<RunStarter | undefined> {
    const { run } = await this.#get<{ run: RunStarter | null }>("a flow run's starter", `/flow-runs/${id(runId)}/starter`, { execution })
    return run ?? undefined
  }

  async decisions(requestId: string, inputHash: string): Promise<DecisionChecks> {
    const d = await this.#get<{ approved: boolean; approved_unresolved: boolean; by_person: boolean; lapsed: boolean }>(
      "a flow call's decision",
      '/flow-decisions',
      { request_id: requestId, input_hash: inputHash },
    )
    return { approved: d.approved, approvedUnresolved: d.approved_unresolved, byPerson: d.by_person, lapsed: d.lapsed }
  }

  async grantInForce(grantId: string): Promise<boolean> {
    return (await this.#get<{ in_force: boolean }>('a run grant', '/flow-grants/in-force', { grant_id: grantId })).in_force
  }

  async queue(runId: string): Promise<(QueueRun & { requested_grant: { id: string; created_at: Date } | null }) | undefined> {
    const { run } = await this.#get<{ run: (WireRun & { requested_grant: { id: string; created_at: string } | null }) | null }>(
      "a flow run's queue",
      `/flow-runs/${id(runId)}/queue`,
    )
    if (!run) return undefined
    const { requested_grant: grant, ...rest } = run
    return { ...rest, requested_grant: grant ? { id: grant.id, created_at: new Date(grant.created_at) } : null }
  }

  async waitingRuns(): Promise<(QueueRun & { updated_at: Date })[]> {
    const { runs } = await this.#get<{ runs: (WireRun & { updated_at: string })[] }>('the waiting flow runs', '/flow-runs/waiting')
    return runs.map((run) => ({ ...run, updated_at: new Date(run.updated_at) }))
  }

  async steps(runId: string): Promise<StepRow[]> {
    return (await this.#get<{ steps: StepRow[] }>("a flow run's steps", `/flow-runs/${id(runId)}/steps`)).steps
  }

  async parentHopApprovals(after: ParentHopCursor | null, limit: number): Promise<{ runs: ParentHopRun[]; next: ParentHopCursor | null }> {
    return await this.#get<{ runs: ParentHopRun[]; next: ParentHopCursor | null }>(
      'the calls waiting at hop parent',
      '/flow-runs/parent-hop-approvals',
      { limit: String(limit), ...(after ?? {}) },
    )
  }

  async stillAtParentHop(requestIds: readonly string[]): Promise<Set<string>> {
    const waiting = new Set<string>()
    for (let i = 0; i < requestIds.length; i += STILL_WAITING_BATCH) {
      const answer = await crudRead<{ waiting: string[] }>(this.#fetch, this.#base, 'the calls still at hop parent', '/flow-runs/parent-hop/waiting', {
        method: 'POST',
        body: { request_ids: requestIds.slice(i, i + STILL_WAITING_BATCH) },
      })
      for (const w of answer.waiting) waiting.add(w)
    }
    return waiting
  }
}

/** `crud` once `useCrud()` holds (the API lists area `flows`), `sql` until then; chosen per read. */
export function gatedFlowRunReads(sql: FlowRunReads, crud: FlowRunReads, useCrud: () => boolean): FlowRunReads {
  const pick = () => (useCrud() ? crud : sql)
  return {
    starter: (runId, execution) => pick().starter(runId, execution),
    decisions: (requestId, inputHash) => pick().decisions(requestId, inputHash),
    grantInForce: (grantId) => pick().grantInForce(grantId),
    queue: (runId) => pick().queue(runId),
    waitingRuns: () => pick().waitingRuns(),
    steps: (runId) => pick().steps(runId),
    parentHopApprovals: (after, limit) => pick().parentHopApprovals(after, limit),
    stillAtParentHop: (requestIds) => pick().stillAtParentHop(requestIds),
  }
}
