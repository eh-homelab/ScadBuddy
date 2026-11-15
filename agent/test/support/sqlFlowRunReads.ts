import type { Sql } from 'postgres'
import type {
  DecisionChecks,
  FlowRunReads,
  ParentHopCursor,
  ParentHopRun,
  QueueRun,
  RunStarter,
  StartedBy,
  StepRow,
} from '../../src/flows/backendReads.js'

// The backend's answers to the agent's flow reads, as its CRUD routes of area `flows` give them
// (backend flows/agent_reads.py, the same queries), run straight on the test database: the
// agent's Postgres tests have no API to ask. The agent itself reads these tables only through
// the routes (src/flows/backendReads.ts `CrudFlowRunReads`, #5299). test/retiredTables.test.ts
// guards that, since the agent still connects as the database owner and the API's revoke of
// its role's grants (backend auth/revoke.py `RETIRED_GRANTS`) binds nothing until #5299 step 12.

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

/** The backend's queries, on the test database (see above). */
export class SqlFlowRunReads implements FlowRunReads {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async starter(runId: string, execution: string): Promise<RunStarter | undefined> {
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
