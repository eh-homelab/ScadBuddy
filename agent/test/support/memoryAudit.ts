import {
  type AuditContext,
  type AuditEntry,
  type AuditFilter,
  type AuditPage,
  type AuditRecord,
  type AuditRepo,
  clampRetentionDays,
  DEFAULT_AUDIT_RETENTION_DAYS,
  DEFAULT_PAGE,
  MAX_PAGE,
} from '../../src/audit/log.js'

/** An in-memory audit log for tests that have no Postgres; the real one is audit/log.ts AuditLog. */
export class MemoryAudit implements AuditRepo {
  readonly entries: AuditEntry[] = []
  readonly filters: AuditFilter[] = []
  retention = DEFAULT_AUDIT_RETENTION_DAYS
  readonly retentionWrites: { days: number; context: AuditContext }[] = []

  async record(entry: AuditEntry): Promise<void> {
    this.entries.push(entry)
  }

  async list(filter: AuditFilter = {}): Promise<AuditPage> {
    this.filters.push(filter)
    const limit = Math.min(Math.max(filter.limit ?? DEFAULT_PAGE, 1), MAX_PAGE)
    const all: AuditRecord[] = this.entries
      .map((e, i) => ({
        id: String(i + 1),
        at: new Date(0).toISOString(),
        kind: e.kind,
        action: e.action,
        surface: e.surface,
        actor: e.actor,
        client_ip: e.clientIp ?? null,
        session_id: e.sessionId ?? null,
        turn_id: e.turnId ?? null,
        tool_use_id: e.toolUseId ?? null,
        tier: e.tier ?? null,
        input_hash: e.inputHash ?? null,
        input_summary: e.inputSummary ?? null,
        approval_id: e.approvalId ?? null,
        approved_by: null,
        outcome: e.outcome,
        detail: e.detail ?? null,
        started_at: e.startedAt?.toISOString() ?? null,
        finished_at: e.finishedAt?.toISOString() ?? null,
        duration_ms: null,
      }))
      .reverse()
      .filter((r) => (filter.kind ? r.kind === filter.kind : true))
      .filter((r) => (filter.outcome ? r.outcome === filter.outcome : true))
      .filter((r) => (filter.before ? Number(r.id) < Number(filter.before) : true))
    const page = all.slice(0, limit)
    return { entries: page, next: all.length > limit ? (page.at(-1)?.id ?? null) : null }
  }

  async retentionDays(): Promise<number> {
    return this.retention
  }

  async setRetentionDays(days: number, context: AuditContext): Promise<number> {
    this.retention = clampRetentionDays(days)
    this.retentionWrites.push({ days: this.retention, context })
    return this.retention
  }
}
