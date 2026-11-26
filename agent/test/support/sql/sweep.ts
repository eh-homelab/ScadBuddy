import type { Sql } from 'postgres'
import { SYSTEM_ACTOR } from '../../../src/audit/log.js'
import type { Orphans, SweepRoutes, SweepRun } from '../../../src/gate/crudPending.js'
import type { Logged } from '../../../src/gate/crudStore.js'
import { parseRequestId } from '../../../src/gate/ids.js'
import { inputResolved } from '../../../src/gate/projection.js'
import { event, type ServerEvent } from '../../../src/sessions/protocol.js'
import { SqlAudit } from './audit.js'
import { appendIn } from './gate.js'

// Tests only: the orphan sweep's reads and cancels over the test database, the SQL the agent ran
// before the API took area `pending-input` (#5299 step 12). src holds no SQL; the API's is
// tools/go/core/internal/pending. Each entry is one transaction; an approval's audit row is
// written after it.

type Row = {
  request_id: string
  session_id: string
  kind: 'approval' | 'answer'
  tool: string
  summary: string
  input_hash: string | null
  requested_by: { label?: string } | null
  created_at: Date
  /** `created_at::text`: microseconds, which a Date loses. */
  created_at_text: string
}

export class SqlSweep implements SweepRoutes {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async runs(minAgeS: number, recheckS: number): Promise<SweepRun[]> {
    return await this.#sql<SweepRun[]>`
      SELECT DISTINCT workflow_id, workflow_run_id FROM ai_pending_input
      WHERE created_at < now() - make_interval(secs => ${minAgeS})
        AND (last_checked_at IS NULL OR last_checked_at < now() - make_interval(secs => ${recheckS}))
      LIMIT 100`
  }

  async checked(workflowId: string, runId: string): Promise<void> {
    await this.#sql`
      UPDATE ai_pending_input SET last_checked_at = now()
      WHERE workflow_id = ${workflowId} AND workflow_run_id = ${runId}`
  }

  async cancel(of: Orphans, reason: string): Promise<{ removed: number; logged: Logged[] }> {
    const sql = this.#sql
    const ids =
      'sessionId' in of
        ? await sql<{ request_id: string }[]>`SELECT request_id FROM ai_pending_input WHERE session_id = ${of.sessionId}`
        : await sql<{ request_id: string }[]>`
            SELECT request_id FROM ai_pending_input WHERE workflow_id = ${of.workflowId} AND workflow_run_id = ${of.runId}`
    let removed = 0
    const logged: Logged[] = []
    for (const { request_id } of ids) {
      const done = await sql.begin(async (tx) => {
        const [row] = await tx<Row[]>`
          DELETE FROM ai_pending_input WHERE request_id = ${request_id}
          RETURNING request_id, session_id, kind, tool, summary, input_hash, requested_by, created_at, created_at::text AS created_at_text`
        if (!row) return undefined
        await tx`
          INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder, reason, tool, requested_at)
          VALUES (${row.request_id}, ${row.session_id}, ${row.kind}, 'cancelled', ${tx.json(SYSTEM_ACTOR)}, ${reason}, ${row.tool}, ${row.created_at_text}::timestamptz)`
        const events: ServerEvent[] = [
          row.kind === 'approval'
            ? event({ type: 'approval.resolved', sessionId: row.session_id, id: row.request_id, approved: false, decision: 'cancelled', reason })
            : event({ type: 'question.resolved', sessionId: row.session_id, id: row.request_id, answered: false, reason }),
          inputResolved(row.session_id, row.request_id, row.kind, 'cancelled', reason),
        ]
        const [idle] = await tx`
          UPDATE ai_sessions SET status = 'idle', updated_at = now()
          WHERE id = ${row.session_id} AND status IN ('waiting_approval', 'waiting_input')
            AND NOT EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = ${row.session_id})
          RETURNING id`
        if (idle) events.push(event({ type: 'session.status', sessionId: row.session_id, status: 'idle' }))
        const seqs = await appendIn(tx, row.session_id, events)
        return { row, events, seqs }
      })
      if (!done) continue
      removed += 1
      logged.push({ session_id: done.row.session_id, events: done.events, seqs: done.seqs })
      if (done.row.kind === 'approval') await this.#audit(done.row, reason)
    }
    return { removed, logged }
  }

  async #audit(row: Row, reason: string): Promise<void> {
    const parsed = parseRequestId(row.request_id)
    await new SqlAudit(this.#sql).record({
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
      detail: `${row.tool}: ${reason} (requested by ${row.requested_by?.label ?? '?'})`,
      startedAt: row.created_at,
      finishedAt: new Date(),
    })
  }
}
