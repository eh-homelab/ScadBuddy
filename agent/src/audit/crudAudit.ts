import { wellFormed } from '../api/crudRead.js'
import { type CrudTarget, crudOf } from '../api/crudStores.js'
import { isUuid } from '../harness/stateDirs.js'
import { type AuditEntry, type AuditFilter, type AuditPage, type AuditRecord, cap, DETAIL_MAX, SUMMARY_MAX } from './log.js'

// The audit log through the API's CRUD routes (#5299 step 10; backend api/crud_agent_audit.py,
// agent_data/audit_log.py), area `audit`: a row written (its approver copied in the same
// statement on the API), a page listed, and the rows past the retention pruned. AuditLog
// (audit/log.ts) calls these once the API lists the area, and its own SQL until then (D9).
// Every text is capped here as the SQL caps it, so the API never refuses a row the SQL would
// have written.

/** The audit log's routes, as AuditLog calls them. */
export interface AuditRoutes {
  record(entry: AuditEntry): Promise<void>
  list(filter: AuditFilter): Promise<AuditPage>
  prune(days: number): Promise<number>
}

type WireRow = Omit<AuditRecord, 'actor' | 'approved_by'> & {
  principal_kind: string
  principal_id: string
  principal_label: string
  approved_by_kind: string | null
  approved_by_id: string | null
  approved_by_label: string | null
}

const iso = (at: Date | undefined): string | null => (at === undefined ? null : at.toISOString())
const uuidOrNull = (id: string | null | undefined): string | null => (id && isUuid(id) ? id : null)

/** AuditRoutes over the API's CRUD routes, area `audit`. */
export class CrudAudit implements AuditRoutes {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async record(entry: AuditEntry): Promise<void> {
    const body = {
      kind: entry.kind,
      action: cap(entry.action, 200),
      surface: entry.surface,
      actor: { kind: cap(entry.actor.kind, 50), id: cap(entry.actor.id, 200), label: cap(entry.actor.label, 200) },
      outcome: entry.outcome,
      client_ip: entry.clientIp ?? null,
      session_id: uuidOrNull(entry.sessionId),
      turn_id: uuidOrNull(entry.turnId),
      tool_use_id: entry.toolUseId ?? null,
      tier: entry.tier ?? null,
      input_hash: entry.inputHash ?? null,
      input_summary: entry.inputSummary === undefined ? null : cap(entry.inputSummary, SUMMARY_MAX),
      approval_id: uuidOrNull(entry.approvalId),
      request_id: entry.requestId === undefined ? null : cap(entry.requestId, 500),
      detail: entry.detail === undefined ? null : cap(entry.detail, DETAIL_MAX),
      started_at: iso(entry.startedAt),
      finished_at: iso(entry.finishedAt),
      cost_usd: entry.costUsd !== undefined && Number.isFinite(entry.costUsd) ? entry.costUsd : null,
      cost_priced: entry.costPriced ?? null,
      cost_estimated_usd: entry.costEstimatedUsd !== undefined && Number.isFinite(entry.costEstimatedUsd) ? entry.costEstimatedUsd : null,
    }
    await this.#call('an audit row', '/audit/record', { body: wellFormed(body) })
  }

  async list(filter: AuditFilter): Promise<AuditPage> {
    const before = filter.before !== undefined && /^\d{1,18}$/.test(filter.before) ? Number(filter.before) : undefined
    // A cursor that is no row id names no page, as the SQL's bigint cast refused it.
    if (filter.before !== undefined && before === undefined) return { entries: [], next: null }
    const body = {
      kind: filter.kind ?? null,
      outcome: filter.outcome ?? null,
      surface: filter.surface ?? null,
      action: filter.action ?? null,
      session_id: filter.sessionId ?? null,
      principal: filter.principal ?? null,
      since: filter.since?.toISOString() ?? null,
      until: filter.until?.toISOString() ?? null,
      before: before ?? null,
      limit: filter.limit ?? null,
    }
    const page = await this.#call<{ rows: WireRow[]; next: string | null }>('the audit log', '/audit/list', { body: wellFormed(body) })
    return { entries: page.rows.map(viewOf), next: page.next }
  }

  async prune(days: number): Promise<number> {
    const { deleted } = await this.#call<{ deleted: number }>('the audit retention', '/audit/prune', { body: { days } })
    return deleted
  }
}

const isoOf = (at: string | null): string | null => (at === null ? null : new Date(at).toISOString())

function viewOf(r: WireRow): AuditRecord {
  return {
    id: r.id,
    at: new Date(r.at).toISOString(),
    kind: r.kind,
    action: r.action,
    surface: r.surface,
    actor: { kind: r.principal_kind, id: r.principal_id, label: r.principal_label },
    client_ip: r.client_ip,
    session_id: r.session_id,
    turn_id: r.turn_id,
    tool_use_id: r.tool_use_id,
    tier: r.tier,
    input_hash: r.input_hash,
    input_summary: r.input_summary,
    approval_id: r.approval_id,
    approved_by:
      r.approved_by_kind && r.approved_by_id && r.approved_by_label ? { kind: r.approved_by_kind, id: r.approved_by_id, label: r.approved_by_label } : null,
    outcome: r.outcome,
    detail: r.detail,
    started_at: isoOf(r.started_at),
    finished_at: isoOf(r.finished_at),
    duration_ms: r.duration_ms,
    cost_usd: r.cost_usd,
    cost_priced: r.cost_priced,
    cost_estimated_usd: r.cost_estimated_usd,
  }
}
