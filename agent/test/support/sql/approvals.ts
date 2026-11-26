import type { Sql, TransactionSql } from 'postgres'
import type { ApprovalRoutes, NewApprovalRow, SettleCall } from '../../../src/approvals/crudApprovals.js'
import type { ApprovalRow } from '../../../src/approvals/service.js'
import { type AuditOutcome, type AuditSurface, SYSTEM_ACTOR } from '../../../src/audit/log.js'
import type { Logged } from '../../../src/gate/crudStore.js'
import { approvalEntry, inputRequested, inputResolved } from '../../../src/gate/projection.js'
import { event, type Owner, type ServerEvent } from '../../../src/sessions/protocol.js'
import { SqlAudit } from './audit.js'
import { appendIn } from './gate.js'

// Tests only: the approvals over the test database, the SQL the agent ran before the API took
// area `approvals` (#5299 step 12). src holds no SQL; the API's is tools/go/core/internal/approvals.
// A decision's audit row is written after its transaction here, in it on the API.

const COLUMNS = `id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
  requested_by_kind, requested_by_id, requested_by_label, requested_tiers, created_at, expires_at, decision,
  decided_by_kind, decided_by_id, decided_by_label, decided_at, reason, usable_until, resume_turn_id,
  consumed_at, revoked_at, traceparent, decision_traceparent, (decision IS NULL AND expires_at <= now()) AS due`

/** An approved row that can still be used. */
const USABLE = `decision = 'approved' AND consumed_at IS NULL AND revoked_at IS NULL AND usable_until > now()`

/** The key MCP prepares serialise on (`createPrepared`). */
const PREPARE_LOCK = 'scadbuddy:ai_approvals:mcp_prepare'

/** The longest `approval.required` summary: the arguments' 500, plus a resolving tool's line (#4442). */
const APPROVAL_SUMMARY_MAX = 500 + 200 + 1

function cap(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

function auditOutcome(decision: SettleCall['decision']): AuditOutcome {
  return decision === 'approved' ? 'ok' : decision === 'denied' ? 'denied' : 'refused'
}

/**
 * Tests only: runs in a write's transaction just before its session events are appended. A throw
 * rolls the write back, as a failed append on the API does; a read of the pool from here sees
 * nothing the write has not committed.
 */
export type BeforeAppend = (sessionId: string, events: readonly ServerEvent[]) => Promise<void>

export class SqlApprovals implements ApprovalRoutes {
  readonly #sql: Sql
  readonly #beforeAppend: BeforeAppend | undefined

  constructor(sql: Sql, options: { beforeAppend?: BeforeAppend } = {}) {
    this.#sql = sql
    this.#beforeAppend = options.beforeAppend
  }

  async row(id: string): Promise<ApprovalRow | undefined> {
    const [row] = await this.#sql.unsafe<ApprovalRow[]>(`SELECT ${COLUMNS} FROM ai_approvals WHERE id = $1`, [id])
    return row
  }

  async pending(): Promise<ApprovalRow[]> {
    return await this.#sql.unsafe<ApprovalRow[]>(`SELECT ${COLUMNS} FROM ai_approvals WHERE decision IS NULL ORDER BY created_at, id LIMIT 500`)
  }

  async prepared(who: Pick<Owner, 'kind' | 'id'>): Promise<ApprovalRow[]> {
    return await this.#sql.unsafe<ApprovalRow[]>(
      `SELECT ${COLUMNS} FROM ai_approvals
       WHERE session_id IS NULL AND requested_by_kind = $1 AND requested_by_id = $2
         AND decision IS NULL AND expires_at > now()
       ORDER BY created_at, id LIMIT 500`,
      [who.kind, who.id],
    )
  }

  async due(tried: readonly string[], limit: number): Promise<string[]> {
    const rows = await this.#sql.unsafe<{ id: string }[]>(
      `SELECT id FROM ai_approvals
       WHERE decision IS NULL AND expires_at <= now() AND NOT (id = ANY($1::uuid[]))
       ORDER BY expires_at LIMIT $2`,
      [[...tried], limit],
    )
    return rows.map((r) => r.id)
  }

  async #insert(tx: TransactionSql, row: NewApprovalRow): Promise<ApprovalRow> {
    const by = row.requested_by
    const [inserted] = await tx.unsafe<ApprovalRow[]>(
      `INSERT INTO ai_approvals (id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
                                 requested_by_kind, requested_by_id, requested_by_label, requested_tiers, expires_at,
                                 traceparent)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now() + ($13 * interval '1 second'), $14)
       RETURNING ${COLUMNS}`,
      [
        row.id,
        row.session_id,
        row.turn_id,
        row.tool_use_id,
        row.tool,
        row.input_summary,
        row.input_hash,
        row.tier,
        by.kind,
        by.id,
        by.label,
        row.requested_tiers,
        row.expiry_s,
        row.traceparent,
      ],
    )
    if (!inserted) throw new Error('approval vanished after insert')
    return inserted
  }

  async create(row: NewApprovalRow): Promise<{ approval: ApprovalRow; logged: Logged[] }> {
    return await this.#sql.begin(async (tx) => {
      const approval = await this.#insert(tx, row)
      if (approval.session_id === null) return { approval, logged: [] }
      const sessionId = approval.session_id
      const events: ServerEvent[] = [
        event({
          type: 'approval.required',
          sessionId,
          id: approval.id,
          tool: row.tool_use_id,
          summary: cap(`${row.tool} ${row.input_summary}`, APPROVAL_SUMMARY_MAX),
          risk: 'outward',
        }),
        inputRequested(
          sessionId,
          approvalEntry({
            id: approval.id,
            sessionId,
            tool: approval.tool,
            inputSummary: approval.input_summary,
            inputHash: approval.input_hash,
            requestedBy: { kind: approval.requested_by_kind, id: approval.requested_by_id, label: approval.requested_by_label },
            createdAt: approval.created_at.toISOString(),
            expiresAt: approval.expires_at.toISOString(),
          }),
        ),
      ]
      await this.#beforeAppend?.(sessionId, events)
      return { approval, logged: [{ session_id: sessionId, events, seqs: await appendIn(tx, sessionId, events) }] }
    })
  }

  async createPrepared(
    row: NewApprovalRow,
    bounds: { perPrincipal: number; total: number; evictReason: string },
    evictTraceparents: readonly string[],
  ): Promise<{ approval: ApprovalRow | undefined; evicted: ApprovalRow[] }> {
    const by = row.requested_by
    return await this.#sql.begin(async (tx) => {
      await tx`SELECT pg_advisory_xact_lock(hashtextextended(${PREPARE_LOCK}, 0))`
      const own = await tx<{ id: string }[]>`
        SELECT id FROM ai_approvals
        WHERE session_id IS NULL AND requested_by_kind = ${by.kind} AND requested_by_id = ${by.id}
          AND decision IS NULL AND expires_at > now()
        ORDER BY created_at, id`
      const evict = own.slice(0, Math.max(own.length - bounds.perPrincipal + 1, 0)).map((r) => r.id)
      const evicted: ApprovalRow[] = []
      if (evict.length > 0) {
        const cancelled = await tx.unsafe<ApprovalRow[]>(
          `UPDATE ai_approvals SET decision = 'cancelled', decided_at = now(), reason = $2
           WHERE id = ANY($1::uuid[]) AND decision IS NULL
           RETURNING ${COLUMNS}`,
          [evict, bounds.evictReason],
        )
        for (const [i, c] of cancelled.entries()) {
          const traceparent = evictTraceparents[i]
          if (traceparent !== undefined) await tx`UPDATE ai_approvals SET decision_traceparent = ${traceparent} WHERE id = ${c.id}`
          evicted.push({ ...c, decision_traceparent: traceparent ?? null })
        }
      } else {
        const [n] = await tx<{ n: number }[]>`
          SELECT count(*)::int AS n FROM ai_approvals
          WHERE session_id IS NULL AND decision IS NULL AND expires_at > now()`
        if ((n?.n ?? 0) >= bounds.total) return { approval: undefined, evicted }
      }
      return { approval: await this.#insert(tx, row), evicted }
    })
  }

  async settle(call: SettleCall): Promise<{ approval: ApprovalRow | undefined; logged: Logged[] }> {
    const { id, decision, by, reason } = call
    const done = await this.#sql.begin(async (tx) => {
      const [row] = await tx.unsafe<ApprovalRow[]>(
        `UPDATE ai_approvals
         SET decision = $2, decided_by_kind = $3, decided_by_id = $4, decided_by_label = $5,
             decided_at = now(), reason = $6,
             usable_until = CASE WHEN $2 = 'approved' THEN now() + ($7 * interval '1 second') END,
             decision_traceparent = $8
         WHERE id = $1 AND decision IS NULL AND ($2 IN ('expired', 'cancelled') OR expires_at > now())
         RETURNING ${COLUMNS}`,
        [id, decision, by?.kind ?? null, by?.id ?? null, by?.label ?? null, reason, call.expiryS, call.traceparent ?? null],
      )
      if (!row) return { approval: undefined, logged: [] }
      if (row.session_id === null) return { approval: row, logged: [] }
      const events: ServerEvent[] = [
        event({
          type: 'approval.resolved',
          sessionId: row.session_id,
          id,
          approved: decision === 'approved',
          decision,
          ...(by && (decision === 'approved' || decision === 'denied') ? { by } : {}),
          ...(reason && (decision === 'expired' || decision === 'cancelled') ? { reason } : {}),
        }),
        inputResolved(row.session_id, `approval:${id}`, 'approval', decision, decision === 'expired' || decision === 'cancelled' ? reason : null),
      ]
      await this.#beforeAppend?.(row.session_id, events)
      return { approval: row, logged: [{ session_id: row.session_id, events, seqs: await appendIn(tx, row.session_id, events) }] }
    })
    const approval = done.approval
    if (approval) {
      await new SqlAudit(this.#sql).record({
        kind: 'approval',
        action: decision,
        surface: by ? ((call.surface as AuditSurface | undefined) ?? 'http') : 'system',
        actor: by ?? SYSTEM_ACTOR,
        clientIp: call.clientIp,
        sessionId: approval.session_id,
        turnId: approval.turn_id,
        toolUseId: approval.tool_use_id,
        tier: approval.tier,
        inputHash: approval.input_hash,
        inputSummary: approval.input_summary,
        approvalId: approval.id,
        outcome: auditOutcome(decision),
        detail: `${approval.tool}${reason ? `: ${reason}` : ''} (requested by ${approval.requested_by_label})`,
        startedAt: approval.created_at,
        finishedAt: new Date(),
      })
    }
    return done
  }

  async consume(id: string, by: Pick<Owner, 'kind' | 'id'>, inputHash: string): Promise<ApprovalRow | undefined> {
    const [row] = await this.#sql.unsafe<ApprovalRow[]>(
      `UPDATE ai_approvals SET consumed_at = now()
       WHERE id = $1 AND session_id IS NULL AND requested_by_kind = $2 AND requested_by_id = $3
         AND input_hash = $4 AND ${USABLE}
       RETURNING ${COLUMNS}`,
      [id, by.kind, by.id, inputHash],
    )
    return row
  }
}
