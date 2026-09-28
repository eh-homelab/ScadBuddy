import { randomBytes } from 'node:crypto'
import type { Sql } from 'postgres'
import { inputHash, summariseInput } from '../approvals/service.js'
import type { RiskTier } from '../harness/permissions.js'
import { isUuid } from '../harness/stateDirs.js'
import { redact } from '../secrets.js'

// The audit log of AI actions (#258; spec §8.3 "the audit log records the
// client IP", §8.6 "audit log", "redacted in logs and audit", §9 "the audit
// log" is AI state in Postgres). The MCP spec asks the same of a client:
// "Log tool usage for audit purposes"
// (https://modelcontextprotocol.io/specification/2025-06-18/server/tools,
// "Security Considerations").
//
// One append-only table, `ai_audit` (db/migrations/20260928T0950Z_audit.sql;
// triggers refuse UPDATE, TRUNCATE and any DELETE but the retention sweep's).
// What is recorded, and by whom:
//
//   resource    every /mcp resources/read, subscribe and unsubscribe
//               (resources/server.ts), refused ones included
//   tool_call   every tool call a session turn makes (sessions/manager.ts via
//               TurnAuditor, audit/turn.ts: ScadBuddy's tools AND plugin tools,
//               including calls that were refused or denied), and every call
//               over /mcp (tools/projections.ts createExternalServer)
//   approval    every decision on an approval: approved, denied, expired,
//               cancelled, and an approval voided unused (approvals/service.ts)
//   credential  PUT/DELETE /api/v1/ai/credentials, refused attempts included
//   plugin      POST/PATCH/DELETE /api/v1/ai/plugins, refused attempts included
//               (both through app.ts `auditWrites`)
//   settings    every ai_settings write (credentials.ts SettingsStore.set)
//   token       MCP token mint and revoke (auditedTokenStore below)
//
// NEVER A SECRET. `input_summary` is the approvals' summary
// (approvals/service.ts summariseInput: sessions/sdkEvents.ts scrubForLog,
// which redacts the turn's credential and plugin secrets from every string
// and blanks arguments named like secrets), capped at SUMMARY_MAX;
// `input_hash` is the approvals' keyed HMAC (same key, so a row can be matched
// to its approval, and a low-entropy input cannot be brute-forced back from
// the table); `detail` is redacted of the same secrets and capped.
//
// Recording never fails the action it records: a write that cannot be logged
// is reported through `onError` (main.ts logs it) and the action goes on. The
// alternative, refusing to act when the audit table is unwritable, would let a
// database blip stop every session; the table is in the same database as
// everything the actions touch, so an outage stops those too.

export const AUDIT_KINDS = ['tool_call', 'resource', 'approval', 'credential', 'plugin', 'settings', 'token'] as const
export type AuditKind = (typeof AUDIT_KINDS)[number]
export const AUDIT_OUTCOMES = ['ok', 'error', 'refused', 'denied'] as const
export type AuditOutcome = (typeof AUDIT_OUTCOMES)[number]
export const AUDIT_SURFACES = ['harness', 'mcp', 'http', 'system'] as const
export type AuditSurface = (typeof AUDIT_SURFACES)[number]

/** Who acted (spec §8.1), as sessions/protocol.ts Owner, or ScadBuddy itself. */
export type AuditActor = { kind: string; id: string; label: string }

/** Expiry sweeps, a turn's own clean-up: nobody in particular. */
export const SYSTEM_ACTOR: AuditActor = { kind: 'system', id: 'scadbuddy', label: 'ScadBuddy' }

/** ai_settings key: how many days audit rows are kept. */
export const SETTING_AUDIT_RETENTION_DAYS = 'audit_retention_days'
export const DEFAULT_AUDIT_RETENTION_DAYS = 90
export const MIN_AUDIT_RETENTION_DAYS = 1
export const MAX_AUDIT_RETENTION_DAYS = 3650

/** Longest `input_summary` and `detail` stored. */
export const SUMMARY_MAX = 1000
export const DETAIL_MAX = 500

export const DEFAULT_PAGE = 50
export const MAX_PAGE = 200

export type AuditEntry = {
  kind: AuditKind
  action: string
  surface: AuditSurface
  actor: AuditActor
  outcome: AuditOutcome
  clientIp?: string | undefined
  sessionId?: string | null | undefined
  turnId?: string | null | undefined
  toolUseId?: string | null | undefined
  tier?: RiskTier | undefined
  inputHash?: string | undefined
  inputSummary?: string | undefined
  approvalId?: string | null | undefined
  detail?: string | undefined
  startedAt?: Date | undefined
  finishedAt?: Date | undefined
}

/** Where entries go. Implementations never throw. */
export interface AuditSink {
  record(entry: AuditEntry): Promise<void>
}

/** Where a write happened and who made it, for the stores that audit their own writes. */
export type AuditContext = { actor: AuditActor; surface: AuditSurface; clientIp?: string | undefined }

export type AuditRecord = {
  id: string
  at: string
  kind: AuditKind
  action: string
  surface: AuditSurface
  actor: AuditActor
  client_ip: string | null
  session_id: string | null
  turn_id: string | null
  tool_use_id: string | null
  tier: RiskTier | null
  input_hash: string | null
  input_summary: string | null
  approval_id: string | null
  /** For a tool call that ran on an approval: who approved it. */
  approved_by: AuditActor | null
  outcome: AuditOutcome
  detail: string | null
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
}

export type AuditFilter = {
  kind?: AuditKind
  outcome?: AuditOutcome
  surface?: AuditSurface
  /** The tool name, or the verb for other kinds. */
  action?: string
  sessionId?: string
  /** principal_id, exactly. */
  principal?: string
  since?: Date
  until?: Date
  /** Keyset cursor: only rows with an id below this (the previous page's `next`). */
  before?: string
  limit?: number
}

export type AuditPage = { entries: AuditRecord[]; next: string | null }

/** Reads and writes ai_settings (credentials.ts SettingsStore). */
export type AuditSettings = {
  get<T>(key: string): Promise<T | undefined>
  set(key: string, value: unknown, context?: AuditContext): Promise<void>
}

/** At most `max` UTF-16 units, cut between code points: a surrogate pair at the boundary goes whole, never as a lone half. */
export function cap(text: string, max: number): string {
  if (text.length <= max) return text
  let kept = text.slice(0, max - 1)
  const last = kept.charCodeAt(kept.length - 1)
  if (last >= 0xd800 && last <= 0xdbff) kept = kept.slice(0, -1)
  return `${kept}…`
}

/** A retention value from Settings, clamped; the default when unset or not a number. */
export function clampRetentionDays(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return DEFAULT_AUDIT_RETENTION_DAYS
  return Math.min(Math.max(Math.round(value), MIN_AUDIT_RETENTION_DAYS), MAX_AUDIT_RETENTION_DAYS)
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
  }
}

/** What the read route and the retention setting need; AuditLog is one, tests have a memory one. */
export interface AuditRepo extends AuditSink {
  list(filter?: AuditFilter): Promise<AuditPage>
  retentionDays(): Promise<number>
  setRetentionDays(days: number, context: AuditContext): Promise<number>
}

export type AuditLogDeps = {
  sql: Sql
  /** ai_settings, for the retention. A thunk: the settings store audits its writes here, so it is made after the log. */
  settings?: () => AuditSettings | undefined
  /**
   * The input-hash key: the approvals' (`approvalHashKey`), so an audit row's
   * hash equals its approval's. Random per process when omitted.
   */
  hashKey?: Buffer
  /** A write that could not be recorded; nothing is thrown. */
  onError?: (err: unknown, entry: AuditEntry) => void
}

export class AuditLog implements AuditRepo {
  private readonly deps: AuditLogDeps
  private readonly hashKey: Buffer

  constructor(deps: AuditLogDeps) {
    this.deps = deps
    this.hashKey = deps.hashKey ?? randomBytes(32)
  }

  /** The keyed input hash (approvals/service.ts inputHash). */
  hash(tool: string, input: Record<string, unknown>): string {
    return inputHash(this.hashKey, tool, input)
  }

  /** The scrubbed, capped input as it is stored. */
  summarise(tool: string, input: Record<string, unknown>, secrets: readonly string[] = []): string {
    return cap(summariseInput(tool, input, secrets), SUMMARY_MAX)
  }

  async record(entry: AuditEntry): Promise<void> {
    const started = entry.startedAt ?? null
    const finished = entry.finishedAt ?? null
    const duration = started && finished ? Math.max(0, finished.getTime() - started.getTime()) : null
    const approvalId = entry.approvalId && isUuid(entry.approvalId) ? entry.approvalId : null
    // A tool call that ran on an approval names who approved it, copied from
    // ai_approvals now (it goes with its session; this row stays).
    const approved = this.deps.sql`decision = 'approved' AND ${entry.kind === 'tool_call'}`
    try {
      await this.deps.sql`
        INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id, principal_label, client_ip,
                              session_id, turn_id, tool_use_id, tier, input_hash, input_summary, approval_id,
                              approved_by_kind, approved_by_id, approved_by_label,
                              outcome, detail, started_at, finished_at, duration_ms)
        VALUES (${entry.kind}, ${cap(entry.action, 200)}, ${entry.surface}, ${cap(entry.actor.kind, 50)},
                ${cap(entry.actor.id, 200)}, ${cap(entry.actor.label, 200)}, ${entry.clientIp ?? null},
                ${entry.sessionId && isUuid(entry.sessionId) ? entry.sessionId : null},
                ${entry.turnId && isUuid(entry.turnId) ? entry.turnId : null},
                ${entry.toolUseId ?? null}, ${entry.tier ?? null}, ${entry.inputHash ?? null},
                ${entry.inputSummary === undefined ? null : cap(entry.inputSummary, SUMMARY_MAX)},
                ${approvalId},
                (SELECT decided_by_kind FROM ai_approvals WHERE id = ${approvalId}::uuid AND ${approved}),
                (SELECT decided_by_id FROM ai_approvals WHERE id = ${approvalId}::uuid AND ${approved}),
                (SELECT decided_by_label FROM ai_approvals WHERE id = ${approvalId}::uuid AND ${approved}),
                ${entry.outcome}, ${entry.detail === undefined ? null : cap(entry.detail, DETAIL_MAX)},
                ${started}, ${finished}, ${duration})`
    } catch (err) {
      this.deps.onError?.(err, entry)
    }
  }

  async list(filter: AuditFilter = {}): Promise<AuditPage> {
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
    const rows = await this.deps.sql.unsafe<Row[]>(
      `SELECT id::text AS id, at, kind, action, surface, principal_kind, principal_id, principal_label, client_ip,
              session_id, turn_id, tool_use_id, tier, input_hash, input_summary, approval_id, approved_by_kind,
              approved_by_id, approved_by_label, outcome, detail, started_at, finished_at, duration_ms
       FROM ai_audit ${where.length ? `WHERE ${where.join(' AND ')}` : ''}
       ORDER BY id DESC LIMIT $${params.length}`,
      params,
    )
    const page = rows.slice(0, limit).map(view)
    return { entries: page, next: rows.length > limit ? (page.at(-1)?.id ?? null) : null }
  }

  async retentionDays(): Promise<number> {
    return clampRetentionDays(await this.deps.settings?.()?.get<number>(SETTING_AUDIT_RETENTION_DAYS))
  }

  /** Stores a new retention (clamped); the write is audited by the settings store. */
  async setRetentionDays(days: number, context: AuditContext): Promise<number> {
    const settings = this.deps.settings?.()
    if (!settings) throw new Error('no settings store')
    const value = clampRetentionDays(days)
    await settings.set(SETTING_AUDIT_RETENTION_DAYS, value, context)
    return value
  }

  /**
   * Deletes rows older than the retention; returns how many. The only DELETE
   * the table's trigger lets through, because it sets `scadbuddy.audit_prune`
   * for this transaction only (`set_config(..., true)` is transaction-local,
   * https://www.postgresql.org/docs/17/functions-admin.html#FUNCTIONS-ADMIN-SET).
   */
  async prune(): Promise<number> {
    const days = await this.retentionDays()
    return this.deps.sql.begin(async (tx) => {
      await tx`SELECT set_config('scadbuddy.audit_prune', 'on', true)`
      const deleted = await tx`DELETE FROM ai_audit WHERE at < now() - (${days} * interval '1 day')`
      return deleted.count
    })
  }

  /** Prunes every `intervalMs` (once `ready` says the schema is there) until the returned function is called. */
  startRetention(
    intervalMs: number,
    options: { ready?: () => Promise<boolean>; onError?: (err: unknown) => void } = {},
  ): () => void {
    const ready = options.ready ?? (() => Promise.resolve(true))
    const tick = () => {
      ready()
        .then((ok) => (ok ? this.prune() : 0))
        .catch(options.onError ?? (() => {}))
    }
    const timer = setInterval(tick, intervalMs)
    timer.unref()
    return () => clearInterval(timer)
  }
}

/** A detail string with the given secrets redacted and capped. */
export function safeDetail(text: string, secrets: readonly string[] = []): string {
  return cap(redact(text, secrets), DETAIL_MAX)
}
