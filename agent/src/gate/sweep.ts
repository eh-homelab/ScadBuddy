import { type Client, WorkflowNotFoundError } from '@temporalio/client'
import type { Sql } from 'postgres'
import { type AuditSink, SYSTEM_ACTOR } from '../audit/log.js'
import type { EventLog } from '../sessions/eventLog.js'
import { event, type ServerEvent } from '../sessions/protocol.js'
import { inputResolved } from './classic.js'
import { parseRequestId } from './ids.js'

// The orphan sweep of ai_pending_input (spec 2026-10-01 §6.6, "The projection"): a row
// is the workflow's to remove (resolve_input), but a run that ended without resolving
// it (terminated, reset, failed) leaves it behind, and the badge would count it
// forever. Each tick, rows older than `minAgeS` whose run was not checked within
// `recheckS` are grouped by run, and each run is described once. A closed run's rows
// are resolved `cancelled` with the same guarded DELETE … RETURNING resolve_input
// uses, so a row the workflow resolved first is not written twice; an open run's rows
// are stamped `last_checked_at`; a describe that fails leaves its rows for a later
// tick. The writes mirror agent-durable's gate/store.py `_resolved`, except the
// session's status, which goes to `idle`: no run holds the entry any more (plan 5b
// ruling 13).

export type RunState = 'open' | 'closed'
/** Whether a workflow run is still running; one that is not found is closed. */
export type DescribeRun = (workflowId: string, runId: string) => Promise<RunState>

export const ORPHAN_REASON = "the session's workflow run has ended"

type Row = {
  request_id: string
  session_id: string
  kind: 'approval' | 'answer'
  tool: string
  summary: string
  input_hash: string | null
  requested_by: { label?: string } | null
  created_at: Date
}

export type SweepDeps = {
  sql: Sql
  describe: DescribeRun
  events: EventLog
  audit?: AuditSink | undefined
  /** A row younger than this is not described (default 600 s): its workflow is still opening it. */
  minAgeS?: number
  /** An open run is described again only after this long (default 600 s). */
  recheckS?: number
}

export class PendingInputSweep {
  readonly #deps: SweepDeps
  readonly #minAgeS: number
  readonly #recheckS: number

  constructor(deps: SweepDeps) {
    this.#deps = deps
    this.#minAgeS = deps.minAgeS ?? 600
    this.#recheckS = deps.recheckS ?? 600
  }

  /** One pass; returns the rows it removed. */
  async sweep(): Promise<number> {
    const { sql } = this.#deps
    const runs = await sql<{ workflow_id: string; workflow_run_id: string }[]>`
      SELECT DISTINCT workflow_id, workflow_run_id FROM ai_pending_input
      WHERE created_at < now() - make_interval(secs => ${this.#minAgeS})
        AND (last_checked_at IS NULL OR last_checked_at < now() - make_interval(secs => ${this.#recheckS}))
      LIMIT 100`
    let removed = 0
    for (const run of runs) {
      let state: RunState
      try {
        state = await this.#deps.describe(run.workflow_id, run.workflow_run_id)
      } catch {
        continue
      }
      if (state === 'open') {
        await sql`
          UPDATE ai_pending_input SET last_checked_at = now()
          WHERE workflow_id = ${run.workflow_id} AND workflow_run_id = ${run.workflow_run_id}`
      } else {
        removed += await this.#cancelRun(run.workflow_id, run.workflow_run_id)
      }
    }
    return removed
  }

  /** Runs `sweep` every `intervalMs`; returns the stop. */
  start(intervalMs: number, options: { ready?: () => Promise<boolean>; onError?: (err: unknown) => void } = {}): () => void {
    const ready = options.ready ?? (() => Promise.resolve(true))
    let running = false
    const timer = setInterval(() => {
      if (running) return
      running = true
      // Not until the database answers and its migrations have applied.
      ready()
        .then((ok) => (ok ? this.sweep() : 0))
        .catch((err: unknown) => options.onError?.(err))
        .finally(() => {
          running = false
        })
    }, intervalMs)
    timer.unref()
    return () => clearInterval(timer)
  }

  async #cancelRun(workflowId: string, runId: string): Promise<number> {
    const { sql } = this.#deps
    const ids = await sql<{ request_id: string }[]>`
      SELECT request_id FROM ai_pending_input WHERE workflow_id = ${workflowId} AND workflow_run_id = ${runId}`
    let removed = 0
    for (const { request_id } of ids) {
      const done = await sql.begin(async (tx) => {
        const [row] = await tx<Row[]>`
          DELETE FROM ai_pending_input WHERE request_id = ${request_id}
          RETURNING request_id, session_id, kind, tool, summary, input_hash, requested_by, created_at`
        if (!row) return undefined
        await tx`
          INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder, reason)
          VALUES (${row.request_id}, ${row.session_id}, ${row.kind}, 'cancelled', ${tx.json(SYSTEM_ACTOR)}, ${ORPHAN_REASON})`
        const events: ServerEvent[] = [
          row.kind === 'approval'
            ? event({ type: 'approval.resolved', sessionId: row.session_id, id: row.request_id, approved: false, decision: 'cancelled', reason: ORPHAN_REASON })
            : event({ type: 'question.resolved', sessionId: row.session_id, id: row.request_id, answered: false, reason: ORPHAN_REASON }),
          inputResolved(row.session_id, row.request_id, row.kind, 'cancelled', ORPHAN_REASON),
        ]
        const [idle] = await tx`
          UPDATE ai_sessions SET status = 'idle', updated_at = now()
          WHERE id = ${row.session_id} AND status IN ('waiting_approval', 'waiting_input')
            AND NOT EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = ${row.session_id})
          RETURNING id`
        if (idle) events.push(event({ type: 'session.status', sessionId: row.session_id, status: 'idle' }))
        const seqs = await this.#deps.events.append(row.session_id, events, tx)
        return { row, events, seqs }
      })
      if (!done) continue
      removed += 1
      this.#deps.events.committed(done.row.session_id, done.events, done.seqs)
      if (done.row.kind === 'approval') await this.#audit(done.row)
    }
    return removed
  }

  async #audit(row: Row): Promise<void> {
    const parsed = parseRequestId(row.request_id)
    await this.#deps.audit?.record({
      kind: 'approval',
      action: 'cancelled',
      surface: 'system',
      actor: SYSTEM_ACTOR,
      sessionId: row.session_id,
      toolUseId: parsed?.store === 'durable' ? parsed.toolUseId : null,
      tier: 'outward',
      inputHash: row.input_hash ?? undefined,
      inputSummary: row.summary,
      requestId: row.request_id,
      outcome: 'refused',
      detail: `${row.tool}: ${ORPHAN_REASON} (requested by ${row.requested_by?.label ?? '?'})`,
      startedAt: row.created_at,
      finishedAt: new Date(),
    })
  }
}

/** DescribeRun over a Temporal client: closed unless the run's status is RUNNING. */
export function temporalDescriber(client: Client): DescribeRun {
  return async (workflowId, runId) => {
    try {
      const described = await client.workflow.getHandle(workflowId, runId).describe()
      return described.status.name === 'RUNNING' ? 'open' : 'closed'
    } catch (err) {
      if (err instanceof WorkflowNotFoundError) return 'closed'
      throw err
    }
  }
}
