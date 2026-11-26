import type { Sql } from 'postgres'
import type { AuditRoutes } from '../../../src/audit/crudAudit.js'
import {
  AuditLog,
  type AuditEntry,
  type AuditFilter,
  type AuditKind,
  type AuditLogDeps,
  type AuditOutcome,
  type AuditPage,
  type AuditRecord,
  type AuditSurface,
  cap,
  DEFAULT_PAGE,
  DETAIL_MAX,
  MAX_PAGE,
  SUMMARY_MAX,
} from '../../../src/audit/log.js'
import type { RiskTier } from '../../../src/harness/permissions.js'
import { isUuid } from '../../../src/harness/stateDirs.js'

// Tests only: the audit log's rows over the test database, the SQL the agent ran before the
// API took area `audit` (#5299 step 12). src holds no SQL; the API's is backend
// tools/go/core/internal/audit.

/** The approvals' and the audit log's input-hash key in tests: one per process, as the API's is one. */
export const TEST_HASH_KEY = Buffer.alloc(32, 7)

/** An AuditLog over the test database. */
export function sqlAuditLog(sql: Sql, deps: Omit<AuditLogDeps, 'routes' | 'hashKey'> & Partial<Pick<AuditLogDeps, 'hashKey'>> = {}): AuditLog {
  return new AuditLog({ hashKey: () => TEST_HASH_KEY, ...deps, routes: new SqlAudit(sql) })
}

type Row = {
  id: string
  at: Date
  kind: AuditKind
  action: string
  surface: AuditSurface
  principal_kind: string
  principal_id: string
  principal_label: string
  client_ip: string | null
  session_id: string | null
  turn_id: string | null
  tool_use_id: string | null
  tier: RiskTier | null
  input_hash: string | null
  input_summary: string | null
  approval_id: string | null
  approved_by_kind: string | null
  approved_by_id: string | null
  approved_by_label: string | null
  outcome: AuditOutcome
  detail: string | null
  started_at: Date | null
  finished_at: Date | null
  duration_ms: number | null
  cost_usd: number | null
  cost_priced: boolean | null
  cost_estimated_usd: number | null
}

function view(row: Row): AuditRecord {
  return {
    id: String(row.id),
    at: row.at.toISOString(),
    kind: row.kind,
    action: row.action,
    surface: row.surface,
    actor: { kind: row.principal_kind, id: row.principal_id, label: row.principal_label },
    client_ip: row.client_ip,
    session_id: row.session_id,
    turn_id: row.turn_id,
    tool_use_id: row.tool_use_id,
    tier: row.tier,
    input_hash: row.input_hash,
    input_summary: row.input_summary,
    approval_id: row.approval_id,
    approved_by:
      row.approved_by_kind && row.approved_by_id && row.approved_by_label
        ? { kind: row.approved_by_kind, id: row.approved_by_id, label: row.approved_by_label }
        : null,
    outcome: row.outcome,
    detail: row.detail,
    started_at: row.started_at?.toISOString() ?? null,
    finished_at: row.finished_at?.toISOString() ?? null,
    duration_ms: row.duration_ms,
    cost_usd: row.cost_usd,
    cost_priced: row.cost_priced,
    cost_estimated_usd: row.cost_estimated_usd,
  }
}

/** A cost as stored: never below zero (a difference of float totals can come out an ulp under), null when unknown. */
function usdOrNull(usd: number | undefined): number | null {
  return usd === undefined || !Number.isFinite(usd) ? null : Math.max(usd, 0)
}

export class SqlAudit implements AuditRoutes {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async record(entry: AuditEntry): Promise<void> {
    const started = entry.startedAt ?? null
    const finished = entry.finishedAt ?? null
    const duration = started && finished ? Math.max(0, finished.getTime() - started.getTime()) : null
    const approvalId = entry.approvalId && isUuid(entry.approvalId) ? entry.approvalId : null
    // A tool call that ran on an approval names who approved it, copied from
    // ai_approvals now (it goes with its session; this row stays).
    const approved = this.#sql`decision = 'approved' AND ${entry.kind === 'tool_call'}`
    // A durable call has no ai_approvals row: its approver is the responder of its request's `approved` outcome.
    const requestId = entry.requestId ?? null
    const responder = this.#sql`
      SELECT responder FROM ai_input_responses
      WHERE request_id = ${entry.kind === 'tool_call' ? requestId : null} AND outcome = 'approved'`
    await this.#sql`
        INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id, principal_label, client_ip,
                              session_id, turn_id, tool_use_id, tier, input_hash, input_summary, approval_id,
                              approved_by_kind, approved_by_id, approved_by_label, request_id,
                              outcome, detail, started_at, finished_at, duration_ms,
                              cost_usd, cost_priced, cost_estimated_usd)
        VALUES (${entry.kind}, ${cap(entry.action, 200)}, ${entry.surface}, ${cap(entry.actor.kind, 50)},
                ${cap(entry.actor.id, 200)}, ${cap(entry.actor.label, 200)}, ${entry.clientIp ?? null},
                ${entry.sessionId && isUuid(entry.sessionId) ? entry.sessionId : null},
                ${entry.turnId && isUuid(entry.turnId) ? entry.turnId : null},
                ${entry.toolUseId ?? null}, ${entry.tier ?? null}, ${entry.inputHash ?? null},
                ${entry.inputSummary === undefined ? null : cap(entry.inputSummary, SUMMARY_MAX)},
                ${approvalId},
                COALESCE((SELECT decided_by_kind FROM ai_approvals WHERE id = ${approvalId}::uuid AND ${approved}),
                         (SELECT r.responder->>'kind' FROM (${responder}) r)),
                COALESCE((SELECT decided_by_id FROM ai_approvals WHERE id = ${approvalId}::uuid AND ${approved}),
                         (SELECT r.responder->>'id' FROM (${responder}) r)),
                COALESCE((SELECT decided_by_label FROM ai_approvals WHERE id = ${approvalId}::uuid AND ${approved}),
                         (SELECT r.responder->>'label' FROM (${responder}) r)),
                ${requestId === null ? null : cap(requestId, 500)},
                ${entry.outcome}, ${entry.detail === undefined ? null : cap(entry.detail, DETAIL_MAX)},
                ${started}, ${finished}, ${duration},
                ${usdOrNull(entry.costUsd)}, ${entry.costPriced ?? null}, ${usdOrNull(entry.costEstimatedUsd)})`
  }

  async list(filter: AuditFilter): Promise<AuditPage> {
    const where: string[] = []
    const params: (string | number | Date)[] = []
    const add = (clause: (n: number) => string, value: string | number | Date) => {
      params.push(value)
      where.push(clause(params.length))
    }
    if (filter.kind) add((n) => `kind = $${n}`, filter.kind)
    if (filter.outcome) add((n) => `outcome = $${n}`, filter.outcome)
    if (filter.surface) add((n) => `surface = $${n}`, filter.surface)
    if (filter.action) add((n) => `action = $${n}`, filter.action)
    if (filter.sessionId) add((n) => `session_id = $${n}::uuid`, filter.sessionId)
    if (filter.principal) add((n) => `principal_id = $${n}`, filter.principal)
    if (filter.since) add((n) => `at >= $${n}`, filter.since)
    if (filter.until) add((n) => `at < $${n}`, filter.until)
    if (filter.before) add((n) => `id < $${n}::bigint`, filter.before)
    const limit = Math.min(Math.max(filter.limit ?? DEFAULT_PAGE, 1), MAX_PAGE)
    params.push(limit + 1)
    // ORDER BY names ai_audit.id: a bare `id` is the text output column, which sorts "99" above "1000" (#893).
    const rows = await this.#sql.unsafe<Row[]>(
      `SELECT id::text AS id, at, kind, action, surface, principal_kind, principal_id, principal_label, client_ip,
              session_id, turn_id, tool_use_id, tier, input_hash, input_summary, approval_id, approved_by_kind,
              approved_by_id, approved_by_label, outcome, detail, started_at, finished_at, duration_ms,
              cost_usd, cost_priced, cost_estimated_usd
       FROM ai_audit ${where.length ? `WHERE ${where.join(' AND ')}` : ''}
       ORDER BY ai_audit.id DESC LIMIT $${params.length}`,
      params,
    )
    const page = rows.slice(0, limit).map(view)
    return { entries: page, next: rows.length > limit ? (page.at(-1)?.id ?? null) : null }
  }

  async prune(days: number): Promise<number> {
    return this.#sql.begin(async (tx) => {
      await tx`SELECT set_config('scadbuddy.audit_prune', 'on', true)`
      const deleted = await tx`DELETE FROM ai_audit WHERE at < now() - (${days} * interval '1 day')`
      return deleted.count
    })
  }
}
