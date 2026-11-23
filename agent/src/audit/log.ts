import { inputHash, summariseInput } from '../approvals/service.js'
import type { RiskTier } from '../harness/permissions.js'
import { RECORD_WAIT_MS, waitAtMost } from '../db/bounded.js'
import { redact } from '../secrets.js'
import type { AuditRoutes } from './crudAudit.js'

// The audit log of AI actions (#258; spec §8.3 "the audit log records the
// client IP", §8.6 "audit log", "redacted in logs and audit", §9 "the audit
// log" is AI state in Postgres). The MCP spec asks the same of a client:
// "Log tool usage for audit purposes"
// (https://modelcontextprotocol.io/specification/2025-06-18/server/tools,
// "Security Considerations").
//
// One append-only table, `ai_audit` (backend/scadbuddy/migrations_agent/20260928T0950Z_audit.sql;
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
//   memory      every automatic Hindsight recall and retain of a session turn
//               (memory/hindsight.ts `onActivity`, sessions/manager.ts):
//               action `recall` or `retain`, `input_summary` the bank and the
//               document id or result count as JSON (never the query or a
//               memory), `detail` the redacted reason it failed or timed out
//   http        every request the `http_request` tool makes (#827,
//               harness/httpRequest.ts), each redirect hop its own row:
//               action the method, `input_summary` the scheme, host, status
//               and size as JSON (never a path, a header or a body)
//   question    every answer the user gives a question the agent asked
//               (#940, #1075, gate/questions.ts): action `answered`,
//               tied to the call by `tool_use_id`, `input_hash` the keyed
//               hash of the answers (never their text: an answer may be
//               anything the user typed)
//   turn        every session turn as it ends (#1922, audit/turn.ts
//               turnCostEntry): action how it ended (the result's subtype,
//               `interrupted` or `failed`), and what it cost: `cost_usd`,
//               `cost_priced` (false when Claude Code never priced it) and
//               `cost_estimated_usd`, the part ScadBuddy estimated. The only
//               kind with the cost columns set
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
// is reported through `onError` (main.ts logs it) and the action goes on, and
// one that has not landed after RECORD_WAIT_MS stops holding it up (db/bounded.ts,
// #1076): the write carries on, and the wait that ran out is reported. The
// alternative, refusing to act when the audit table is unwritable, would let a
// database blip stop every session; the table is in the same database as
// everything the actions touch, so an outage stops those too.

export const AUDIT_KINDS = ['tool_call', 'resource', 'approval', 'credential', 'plugin', 'settings', 'token', 'memory', 'http', 'question', 'turn', 'payload_key'] as const
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
  /**
   * A durable session's gate request id (`durable:…`, spec §6.6), on a durable
   * approval's `approval` row and on the `tool_call` row of the call: an auditor
   * joins them on it, and the tool_call row's approver is copied from
   * ai_input_responses by it.
   */
  requestId?: string | undefined
  detail?: string | undefined
  startedAt?: Date | undefined
  finishedAt?: Date | undefined
  /** A `turn` row's cost (#1922): what the turn added to its session's spend, in USD. */
  costUsd?: number | undefined
  /** A `turn` row's: whether Claude Code priced the turn (a result came back). */
  costPriced?: boolean | undefined
  /** A `turn` row's: the part of `costUsd` ScadBuddy estimated (the request a stopped turn was cut off in, #991). */
  costEstimatedUsd?: number | undefined
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
  /** Set on `turn` rows only (#1922): what the turn cost, in USD. */
  cost_usd: number | null
  /** False when Claude Code never priced the turn: `cost_usd` is then ScadBuddy's estimate, or 0. */
  cost_priced: boolean | null
  /** The part of `cost_usd` ScadBuddy estimated itself. */
  cost_estimated_usd: number | null
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

/** What the read route and the retention setting need; AuditLog is one, tests have a memory one. */
export interface AuditRepo extends AuditSink {
  list(filter?: AuditFilter): Promise<AuditPage>
  retentionDays(): Promise<number>
  setRetentionDays(days: number, context: AuditContext): Promise<number>
}

/** What a session's turns need of the audit log: its rows, and the hash and summary of a call. */
export type AuditDeps = Pick<AuditLog, 'record' | 'hash' | 'summarise'>

export type AuditLogDeps = {
  /** The API's routes, area `audit` (audit/crudAudit.ts). */
  routes: AuditRoutes
  /** ai_settings, for the retention. A thunk: the settings store audits its writes here, so it is made after the log. */
  settings?: () => AuditSettings | undefined
  /**
   * The input-hash key: the approvals' (approvals/service.ts `hashKey`, released by the API), so
   * an audit row's hash equals its approval's. Read at each hash; it throws until released.
   */
  hashKey: () => Buffer
  /** A write that could not be recorded, or did not land within `waitMs`; nothing is thrown. */
  onError?: (err: unknown, entry: AuditEntry) => void
  /** How long `record` waits for its row before it lets the action go on (db/bounded.ts). */
  waitMs?: number
}

export class AuditLog implements AuditRepo {
  private readonly deps: AuditLogDeps

  constructor(deps: AuditLogDeps) {
    this.deps = deps
  }

  /** The keyed input hash (approvals/service.ts inputHash). */
  hash(tool: string, input: Record<string, unknown>): string {
    return inputHash(this.deps.hashKey(), tool, input)
  }

  /** The scrubbed, capped input as it is stored. */
  summarise(tool: string, input: Record<string, unknown>, secrets: readonly string[] = []): string {
    return cap(summariseInput(tool, input, secrets), SUMMARY_MAX)
  }

  /** Writes the row, waiting for it at most `waitMs` (#1076); never throws. */
  async record(entry: AuditEntry): Promise<void> {
    await waitAtMost(this.insert(entry), this.deps.waitMs ?? RECORD_WAIT_MS, 'the audit row', (err) => this.deps.onError?.(err, entry))
  }

  private async insert(entry: AuditEntry): Promise<void> {
    try {
      await this.deps.routes.record(entry)
    } catch (err) {
      this.deps.onError?.(err, entry)
    }
  }

  async list(filter: AuditFilter = {}): Promise<AuditPage> {
    return await this.deps.routes.list(filter)
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

  /** Deletes rows older than the retention on the API; returns how many. */
  async prune(): Promise<number> {
    return await this.deps.routes.prune(await this.retentionDays())
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
