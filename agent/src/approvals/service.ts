import { createHmac, hkdfSync, randomBytes, randomUUID } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import type { Tier } from '../auth/principal.js'
import type { RiskTier } from '../harness/permissions.js'
import { isUuid } from '../harness/stateDirs.js'
import { type Kek, redact } from '../secrets.js'
import type { EventLog } from '../sessions/eventLog.js'
import {
  type ApprovalDecisionMessage,
  canSee,
  event,
  type Owner,
  PROTOCOL_VERSION,
  sameOwner,
  type ServerEvent,
} from '../sessions/protocol.js'
import { scrubForLog } from '../sessions/sdkEvents.js'
import { approvalEntry, inputRequested, inputResolved } from '../gate/projection.js'
import { type AuditOutcome, type AuditSink, type AuditSurface, SYSTEM_ACTOR } from '../audit/log.js'
import { type Span, SpanKind } from '@opentelemetry/api'
import { linkTo, recordFailure, traceparentOf, tracer } from '../telemetry/trace.js'
import { type SessionFiles, type WorkspaceRef, withWorkspaceSize } from '../workspace/store.js'

// Approvals of outward tool calls (#258, spec §8.2: "Outward tools always need
// a human approval in the ScadBuddy UI, in every auth mode"). Stored in
// Postgres (`ai_approvals`, db/migrations/*_approvals.sql), so any replica can
// decide one and a pending one survives a restart.
//
// WHO ASKS. An outward call over /mcp (#251's prepare/confirm, below). A session's
// turn parks on its workflow's gate instead (temporal/session/gate.ts, ai_pending_input;
// unified runtime plan Steps 6.3 and 9.7), so no release writes a pending session row
// here any more: a session's rows are its history, and a pending one an earlier
// release left is decided or expires like any other, with nothing parked on it.
//
// BINDING. Each approval stores an HMAC-SHA256 of the tool name and the
// canonical JSON of the FULL input (`hash()`), keyed by a server-side key:
// derived from the key-encryption key (`approvalHashKey`), so a hash stays
// comparable across restarts and cannot be brute-forced back to a
// low-entropy input by someone who can read the table. A prepared action is
// used only by a confirm with the same hash, so a changed input needs a new
// approval. A decider may also send the hash it was shown, and a mismatch is
// refused.
//
// WHO MAY DECIDE (`authorize`):
//   - the browser user: spec §8.1, the browser user "approves in the UI", and
//     §8.2, outward tools need "a human approval in the ScadBuddy UI";
//   - a non-browser principal only with a per-token grant: spec §6,
//     "Approvals of outward actions by another agent are off by default and
//     need a per-token grant". The grant is `grants(principal)`; it defaults
//     to none, and #251's token table (PR #368) supplies it. Even with one, a
//     principal never decides its OWN outward calls: not when it asked for
//     the call, owns the session, or started it. "By another agent" is read
//     strictly, so a grant cannot turn into self-approval. A grant holder may
//     see (and decide) approvals in sessions it does not own, which is what
//     the grant is for. `anonymous` (MCP auth `disabled`) is no exception:
//     spec §8.3, "Outward actions still need a human approval in the UI
//     (§8.2); that rule is independent of auth";
//   - and otherwise only for a session the principal may see (spec §6).
//
// ENDING WITHOUT A DECISION: no decision by `expires_at` is EXPIRED. The
// window is the ai_settings key `approval_expiry_seconds` (default below),
// read when the approval is created; an approval, once approved, is usable
// until `usable_until`, the same window counted from the decision. Due rows
// are expired by `expireDue()` (main.ts runs it periodically, and list/decide
// run it first). An MCP prepare past its principal's bound is CANCELLED
// (`createPrepared`).
//
// MCP PREPARE / CONFIRM (#251's tools, wired in approvals/mcp.ts): an outward
// tool called over /mcp is not run but `create()`d with no session and no
// turn, requested by the MCP principal; the UI decides it through `decide()`
// like any other (so `authorize` refuses self-approval here too); and
// `confirm_action` runs it only when `consumePrepared()` returns the row:
// approved, unused, not voided, within `usable_until`, same principal, same
// input hash, in one UPDATE, so it is used exactly once. Nothing parks for
// these: a confirm before the decision answers "pending" and the client asks
// again.

/** ai_settings key: seconds an approval waits for a decision. */
export const SETTING_APPROVAL_EXPIRY_SECONDS = 'approval_expiry_seconds'
export const DEFAULT_APPROVAL_EXPIRY_SECONDS = 600
export const MIN_APPROVAL_EXPIRY_SECONDS = 10
export const MAX_APPROVAL_EXPIRY_SECONDS = 86_400
/**
 * The longest `approval.required` summary, which the panel's card shows: 500 for the call's
 * arguments, plus a resolving tool's line and its newline (APPROVAL_LINE_MAX, #4442 gate
 * re-review N1), so the line never takes the arguments' room. agent-durable gate/store.py and
 * gate/store.ts keep the same cap.
 */
export const APPROVAL_SUMMARY_MAX = 500 + 200 + 1
/** The advisory-lock key MCP prepares serialise on (`createPrepared`). */
const PREPARE_LOCK = 'scadbuddy:ai_approvals:mcp_prepare'

export const DECISIONS = ['approved', 'denied', 'expired', 'cancelled'] as const
export type Decision = (typeof DECISIONS)[number]

export type ApprovalRecord = {
  id: string
  sessionId: string | null
  turnId: string | null
  toolUseId: string
  tool: string
  inputSummary: string
  inputHash: string
  tier: RiskTier
  requestedBy: Owner
  /**
   * The tiers the asking turn ran with, when its sender's were passed in
   * (sessions/manager.ts SendOptions.tiers); a resumed turn gets no more.
   * Null: the owner's default applied.
   */
  requestedTiers: Tier[] | null
  createdAt: string
  expiresAt: string
  decision: Decision | null
  decidedBy: Owner | null
  decidedAt: string | null
  reason: string | null
  /** Approved: usable until then. */
  usableUntil: string | null
  /** Approved after its turn was gone: the resumed turn that may use it. */
  resumeTurnId: string | null
  consumedAt: string | null
  /** Approved but voided before it was used. */
  revokedAt: string | null
  /** The parked call's tool span (W3C traceparent), for the decision's link (#988); never shown to clients. */
  traceparent: string | null
  /** The decision's `agent.approval` span, the parent of what the turn does next (#988). */
  decisionTraceparent: string | null
}

export type ApprovalErrorCode = 'not_found' | 'forbidden' | 'conflict' | 'expired' | 'input_mismatch' | 'invalid'

const STATUS_OF: Record<ApprovalErrorCode, 400 | 403 | 404 | 409 | 410> = {
  not_found: 404,
  forbidden: 403,
  conflict: 409,
  expired: 410,
  input_mismatch: 409,
  invalid: 400,
}

/** How many due approvals one query of `expireDue` takes. */
export const EXPIRE_PAGE = 500

/**
 * How many pages one `expireDue` sweep takes at most. The rest wait for the next tick, so
 * a pathological backlog never holds one sweep open, nor grows its tried list, without end.
 */
export const EXPIRE_PAGES = 10

/** A refused approval operation; `status` is the HTTP status a route answers with. */
export class ApprovalError extends Error {
  override name = 'ApprovalError'
  readonly code: ApprovalErrorCode
  readonly status: 400 | 403 | 404 | 409 | 410
  /** For a conflict, how the approval ended, as a clause ("it was already denied"). */
  readonly reason: string | undefined
  constructor(code: ApprovalErrorCode, message: string, reason?: string) {
    super(message)
    this.code = code
    this.status = STATUS_OF[code]
    this.reason = reason
  }
}

/** Whether a non-browser principal holds a per-token approval grant (spec §6). */
export type GrantCheck = (principal: Owner) => Promise<boolean>

/** Reads ai_settings (credentials.ts SettingsStore is one). */
export type SettingsReader = { get<T>(key: string): Promise<T | undefined> }

export type ApprovalServiceDeps = {
  sql: Sql
  /** The session event log the approval events go to. */
  events: EventLog
  settings?: SettingsReader
  /** Per-token grants; none when omitted. */
  grants?: GrantCheck
  /**
   * The HMAC key for input hashes (`approvalHashKey`). Without one, a random
   * key for this process: hashes then do not survive a restart, so a prepared
   * action approved before one is never confirmed (safe) and needs a new approval.
   */
  hashKey?: Buffer
  /**
   * The audit log (#258, audit/log.ts): every decision, expiry, cancellation
   * and void is recorded as an `approval` row.
   */
  audit?: AuditSink
  /** The workspace (#2329): a workspace upload's size is read from it for the card (`withWorkspaceSize`). */
  workspace?: SessionFiles
}

/** The input-hash key, derived from the key-encryption key (HKDF-SHA256, its own label). */
export function approvalHashKey(kek: Kek): Buffer {
  return Buffer.from(hkdfSync('sha256', kek.key, Buffer.alloc(0), 'scadbuddy:ai_approvals:input_hash:v1', 32))
}

/**
 * What a parked call tells its turn's trace (spec 2026-10-01 §5.4, "Approvals
 * end and link"; sessions/turnHost.ts, telemetry/turn.ts TurnTrace). `traceparent` is the parked tool span's, stored on the row;
 * `parked` ends that span and the open turn segment once the row exists;
 * `decided` hands over the decision (its `decisionTraceparent` is the parent
 * of what the turn does next), and whether the call now runs; `abandoned` says
 * the row was never written (the gate's throw becomes a deny), so the call no
 * longer waits on a decision.
 */
export type ParkTrace = {
  traceparent: string | undefined
  parked(approvalId: string): void
  decided(approval: Pick<ApprovalRecord, 'id' | 'decision' | 'decisionTraceparent'>, runs: boolean): void
  abandoned(): void
}

export type GateTrace = { park(toolUseId: string, toolName: string): ParkTrace }

type Row = {
  id: string
  session_id: string | null
  turn_id: string | null
  tool_use_id: string
  tool: string
  input_summary: string
  input_hash: string
  tier: RiskTier
  requested_by_kind: Owner['kind']
  requested_by_id: string
  requested_by_label: string
  requested_tiers: Tier[] | null
  created_at: Date
  expires_at: Date
  decision: Decision | null
  decided_by_kind: Owner['kind'] | null
  decided_by_id: string | null
  decided_by_label: string | null
  decided_at: Date | null
  reason: string | null
  usable_until: Date | null
  resume_turn_id: string | null
  consumed_at: Date | null
  revoked_at: Date | null
  traceparent: string | null
  decision_traceparent: string | null
  due: boolean
}

const COLUMNS = `id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
  requested_by_kind, requested_by_id, requested_by_label, requested_tiers, created_at, expires_at, decision,
  decided_by_kind, decided_by_id, decided_by_label, decided_at, reason, usable_until, resume_turn_id,
  consumed_at, revoked_at, traceparent, decision_traceparent, (decision IS NULL AND expires_at <= now()) AS due`

/** An approved row that can still be used. */
const USABLE = `decision = 'approved' AND consumed_at IS NULL AND revoked_at IS NULL AND usable_until > now()`

function record(row: Row): ApprovalRecord {
  return {
    id: row.id,
    sessionId: row.session_id,
    turnId: row.turn_id,
    toolUseId: row.tool_use_id,
    tool: row.tool,
    inputSummary: row.input_summary,
    inputHash: row.input_hash,
    tier: row.tier,
    requestedBy: { kind: row.requested_by_kind, id: row.requested_by_id, label: row.requested_by_label },
    requestedTiers: row.requested_tiers,
    createdAt: row.created_at.toISOString(),
    expiresAt: row.expires_at.toISOString(),
    decision: row.decision,
    decidedBy:
      row.decided_by_kind && row.decided_by_id && row.decided_by_label
        ? { kind: row.decided_by_kind, id: row.decided_by_id, label: row.decided_by_label }
        : null,
    decidedAt: row.decided_at?.toISOString() ?? null,
    reason: row.reason,
    usableUntil: row.usable_until?.toISOString() ?? null,
    resumeTurnId: row.resume_turn_id,
    consumedAt: row.consumed_at?.toISOString() ?? null,
    revokedAt: row.revoked_at?.toISOString() ?? null,
    traceparent: row.traceparent,
    decisionTraceparent: row.decision_traceparent,
  }
}

/** How an approval that can no longer be decided ended, as a clause, from its row's own reason (#1400). */
export function conflictReason(now: ApprovalRecord | undefined): string {
  if (now?.decision === 'cancelled') return now.reason ?? 'it was cancelled'
  if (now?.decision === 'approved' && now.revokedAt !== null) {
    return `it was approved, then withdrawn${now.reason ? ` (${now.reason})` : ''}`
  }
  return `it was already ${now?.decision ?? 'decided'}`
}

/** JSON with object keys sorted at every depth, so equal inputs hash equally. */
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (typeof value === 'object' && value !== null) {
    const entries = Object.entries(value)
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

/** What a decision binds to: an HMAC of the tool and its exact input. */
export function inputHash(key: Buffer, tool: string, input: Record<string, unknown>): string {
  return createHmac('sha256', key).update(canonicalJson({ tool, input }), 'utf8').digest('hex')
}

/** The longest line a resolving tool puts above its call's arguments (#4442). */
export const APPROVAL_LINE_MAX = 200

/**
 * A resolving tool's line (ApprovalRequest.summary, #4442) above the call's own scrubbed
 * arguments, so a person sees what it resolved and every argument the model chose. The
 * line is one line, redacted of the turn's secrets and capped: it can name a printer.
 */
export function withApprovalLine(line: string, summary: string, secrets: readonly string[]): string {
  const one = redact(line, secrets)
    // eslint-disable-next-line no-control-regex -- the control characters are what is stripped
    .replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]+/g, ' ')
    .trim()
  return `${cap(one, APPROVAL_LINE_MAX)}\n${summary}`
}

/** The input as the event log would show it: scrubbed and size-capped by scrubForLog. */
export function summariseInput(tool: string, input: Record<string, unknown>, secrets: readonly string[]): string {
  const scrubbed = scrubForLog(
    event({ type: 'tool.call', sessionId: '-', id: '-', name: tool, input, risk: 'outward' }),
    secrets,
  )
  return JSON.stringify(scrubbed.type === 'tool.call' ? scrubbed.input : {})
}

function cap(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

type SessionAccess = {
  owner: Owner
  creator: Pick<Owner, 'kind' | 'id'>
  /** A live handoff offer's target, who may see the session too (sessions/protocol.ts `canSee`). */
  offer: { to: Pick<Owner, 'kind' | 'id'> } | null
}

export type CreateApproval = {
  sessionId: string | null
  turnId: string | null
  toolUseId: string
  tool: string
  input: Record<string, unknown>
  tier: RiskTier
  requestedBy: Owner
  /** The asking turn's tiers, when not the owner's default; recorded as `requested_tiers`. */
  requestedTiers?: readonly Tier[]
  secrets?: readonly string[]
  /** The parked tool span's traceparent (ParkTrace.traceparent). */
  traceparent?: string
  /** The workspace the call runs in (workspaceOf), for an upload's size; a session's own when omitted. */
  workspace?: WorkspaceRef
  /** The tool's own line for a call it resolved (ApprovalRequest.summary, #4442); else the scrubbed input. */
  summary?: string
}

export type DecideOptions = {
  /** When given, must be the session the approval belongs to. */
  sessionId?: string
  /** When given, the hash the decider was shown; a mismatch is refused. */
  inputHash?: string
  /** For the audit log: the decider's address and the surface it decided on ('http' when omitted). */
  clientIp?: string | undefined
  surface?: AuditSurface
}

/** An approval decision as the audit log's outcome: approved ok, denied denied, the rest refused. */
function auditOutcome(decision: Decision): AuditOutcome {
  return decision === 'approved' ? 'ok' : decision === 'denied' ? 'denied' : 'refused'
}

/**
 * The decision's own trace (spec 2026-10-01 §5.4): a principal's decision is a
 * child of the request that made it; an expiry or a cancellation, which no one
 * asked for, is a root. Either way it links to the parked call's span.
 */
function decisionSpan(approval: ApprovalRecord, by: Owner | undefined): Span {
  const link = linkTo(approval.traceparent)
  const waited = approval.decidedAt ? (Date.parse(approval.decidedAt) - Date.parse(approval.createdAt)) / 1000 : 0
  return tracer().startSpan('agent.approval', {
    kind: SpanKind.INTERNAL,
    root: by === undefined,
    ...(link ? { links: [link] } : {}),
    attributes: {
      'scadbuddy.approval_id': approval.id,
      'scadbuddy.tool': approval.tool,
      'scadbuddy.tier': approval.tier,
      'scadbuddy.outcome': approval.decision ?? 'unknown',
      'scadbuddy.wait_seconds': Math.max(waited, 0),
      ...(approval.sessionId ? { 'scadbuddy.session_id': approval.sessionId } : {}),
      ...(approval.turnId ? { 'scadbuddy.turn_id': approval.turnId } : {}),
      ...(by ? { 'scadbuddy.decided_by_kind': by.kind } : {}),
    },
  })
}

export class ApprovalService {
  private readonly deps: ApprovalServiceDeps
  private readonly hashKey: Buffer

  constructor(deps: ApprovalServiceDeps) {
    this.deps = deps
    this.hashKey = deps.hashKey ?? randomBytes(32)
  }

  /** The input hash a decision binds to (see BINDING). */
  hash(tool: string, input: Record<string, unknown>): string {
    return inputHash(this.hashKey, tool, input)
  }

  // -- reads -------------------------------------------------------------------

  async expirySeconds(): Promise<number> {
    const value = await this.deps.settings?.get<number>(SETTING_APPROVAL_EXPIRY_SECONDS)
    if (typeof value !== 'number' || !Number.isFinite(value)) return DEFAULT_APPROVAL_EXPIRY_SECONDS
    return Math.min(Math.max(Math.round(value), MIN_APPROVAL_EXPIRY_SECONDS), MAX_APPROVAL_EXPIRY_SECONDS)
  }

  private async row(id: string): Promise<(ApprovalRecord & { due: boolean }) | undefined> {
    if (!isUuid(id)) return undefined
    const [row] = await this.deps.sql.unsafe<Row[]>(`SELECT ${COLUMNS} FROM ai_approvals WHERE id = $1`, [id])
    return row ? { ...record(row), due: row.due } : undefined
  }

  private async session(id: string): Promise<SessionAccess | undefined> {
    const [row] = await this.deps.sql<
      {
        owner_kind: Owner['kind']
        owner_id: string
        owner_label: string
        creator_kind: Owner['kind']
        creator_id: string
        offer_kind: Owner['kind'] | null
        offer_id: string | null
      }[]
    >`
      SELECT owner_kind, owner_id, owner_label, creator_kind, creator_id,
             CASE WHEN pending_owner_until > now() THEN pending_owner_kind END AS offer_kind,
             CASE WHEN pending_owner_until > now() THEN pending_owner_id END AS offer_id
      FROM ai_sessions WHERE id = ${id}`
    if (!row) return undefined
    return {
      owner: { kind: row.owner_kind, id: row.owner_id, label: row.owner_label },
      creator: { kind: row.creator_kind, id: row.creator_id },
      offer: row.offer_kind && row.offer_id ? { to: { kind: row.offer_kind, id: row.offer_id } } : null,
    }
  }

  /** Whether a non-browser principal holds the per-token approval grant (spec §6). */
  async holdsGrant(principal: Owner): Promise<boolean> {
    return this.hasGrant(principal)
  }

  /**
   * Every pending approval `principal` may see, oldest first: all of them for the
   * browser user and grant holders; for anyone else, those of sessions it owns or
   * started and its own session-less ones (MCP prepares).
   */
  async listVisiblePending(principal: Owner): Promise<ApprovalRecord[]> {
    if (principal.kind === 'browser' || (await this.hasGrant(principal))) return this.list(principal, { pending: true })
    await this.expireDue()
    const rows = await this.deps.sql.unsafe<Row[]>(
      `SELECT ${COLUMNS} FROM ai_approvals a
       WHERE a.decision IS NULL AND (
         (a.session_id IS NULL AND a.requested_by_kind = $1 AND a.requested_by_id = $2)
         OR EXISTS (SELECT 1 FROM ai_sessions s WHERE s.id = a.session_id
                    AND ((s.owner_kind = $1 AND s.owner_id = $2) OR (s.creator_kind = $1 AND s.creator_id = $2))))
       ORDER BY a.created_at, a.id LIMIT 500`,
      [principal.kind, principal.id],
    )
    return rows.map(record)
  }

  private async hasGrant(principal: Owner): Promise<boolean> {
    return principal.kind !== 'browser' && this.deps.grants !== undefined && (await this.deps.grants(principal))
  }

  /**
   * Whether `principal` may see the approval: the browser user and grant
   * holders see all; others see their sessions' (spec §6) and, outside a
   * session, their own. `null` means visible with no session.
   */
  private async visible(principal: Owner, approval: ApprovalRecord): Promise<SessionAccess | null | false> {
    const everything = principal.kind === 'browser' || (await this.hasGrant(principal))
    if (approval.sessionId === null) return everything || sameOwner(principal, approval.requestedBy) ? null : false
    const session = await this.session(approval.sessionId)
    if (!session) return false
    return everything || canSee(principal, session) ? session : false
  }

  /** One approval, if the principal may see it. */
  async get(id: string, principal: Owner): Promise<ApprovalRecord> {
    const approval = await this.row(id)
    if (!approval || (await this.visible(principal, approval)) === false) {
      throw new ApprovalError('not_found', `no approval ${id}`)
    }
    const { due: _due, ...rest } = approval
    return rest
  }

  /**
   * A session's approvals, oldest first (pending ones only with `pending`).
   * Without a session: every pending approval (sessionless ones included),
   * for the browser user and grant holders; others must name a session.
   */
  async list(principal: Owner, filter: { sessionId?: string; pending?: boolean } = {}): Promise<ApprovalRecord[]> {
    await this.expireDue()
    const { sessionId, pending } = filter
    const everything = principal.kind === 'browser' || (await this.hasGrant(principal))
    if (sessionId !== undefined) {
      const session = isUuid(sessionId) ? await this.session(sessionId) : undefined
      if (!session || !(everything || canSee(principal, session))) {
        throw new ApprovalError('not_found', `no session ${sessionId}`)
      }
      const rows = await this.deps.sql.unsafe<Row[]>(
        `SELECT ${COLUMNS} FROM ai_approvals WHERE session_id = $1 ${pending ? 'AND decision IS NULL' : ''}
         ORDER BY created_at, id LIMIT 500`,
        [sessionId],
      )
      return rows.map(record)
    }
    if (!everything) throw new ApprovalError('invalid', 'name a session to list its approvals')
    const rows = await this.deps.sql.unsafe<Row[]>(
      `SELECT ${COLUMNS} FROM ai_approvals WHERE decision IS NULL ORDER BY created_at, id LIMIT 500`,
    )
    return rows.map(record)
  }

  // -- writes ------------------------------------------------------------------

  /** The INSERT of a pending approval, on `db` (the pool, or a transaction). */
  private async insert(db: Sql | TransactionSql, request: CreateApproval, summary: string): Promise<Row | undefined> {
    const ttl = await this.expirySeconds()
    const { requestedBy: by } = request
    const [row] = await db.unsafe<Row[]>(
      `INSERT INTO ai_approvals (id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
                                 requested_by_kind, requested_by_id, requested_by_label, requested_tiers, expires_at,
                                 traceparent)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now() + ($13 * interval '1 second'), $14)
       RETURNING ${COLUMNS}`,
      [
        randomUUID(),
        request.sessionId,
        request.turnId,
        request.toolUseId,
        request.tool,
        summary,
        this.hash(request.tool, request.input),
        request.tier,
        by.kind,
        by.id,
        by.label,
        request.requestedTiers ? [...request.requestedTiers] : null,
        ttl,
        request.traceparent ?? null,
      ],
    )
    return row
  }

  /** The row's and the card's summary of the call: its input with a workspace upload's size (never hashed). */
  private async summary(request: Omit<CreateApproval, 'sessionId' | 'turnId'> & { sessionId?: string | null }): Promise<string> {
    const ws = request.workspace ?? (request.sessionId ? { kind: 'session' as const, sessionId: request.sessionId } : undefined)
    const shown = await withWorkspaceSize(this.deps.workspace, ws, request.input)
    const input = summariseInput(request.tool, shown, request.secrets ?? [])
    return request.summary === undefined ? input : withApprovalLine(request.summary, input, request.secrets ?? [])
  }

  /**
   * Records a pending approval (and, in a session, emits `approval.required`). Only
   * createPrepared writes one now (an MCP prepare, sessionless); tests also seed rows.
   */
  async create(request: CreateApproval): Promise<ApprovalRecord> {
    const secrets = request.secrets ?? []
    const summary = await this.summary(request)
    // The row and its `approval.required` commit together: a decision can only
    // see the row once it has committed, so its `approval.resolved` cannot be
    // logged before the event that asked for it.
    let logged: { sessionId: string; events: ServerEvent[]; seqs: number[] } | undefined
    const approval = await this.deps.sql.begin(async (tx) => {
      const row = await this.insert(tx, request, summary)
      if (!row) throw new Error('approval vanished after insert')
      const created = record(row)
      if (created.sessionId === null) return created
      const tail: ServerEvent[] = [
        event({
          type: 'approval.required',
          sessionId: created.sessionId,
          id: created.id,
          tool: request.toolUseId,
          summary: cap(`${request.tool} ${summary}`, APPROVAL_SUMMARY_MAX),
          risk: 'outward',
        }),
        inputRequested(created.sessionId, approvalEntry(created)),
      ]
      const events = tail.map((e) => scrubForLog(e, secrets))
      logged = { sessionId: created.sessionId, events, seqs: await this.deps.events.append(created.sessionId, events, tx) }
      return created
    })
    // Committed: wake followers, and announce it on the bus (#300).
    if (logged) this.deps.events.committed(logged.sessionId, logged.events, logged.seqs)
    return approval
  }

  /**
   * Decides a pending approval, once: the first decision wins. An approval
   * becomes usable for the expiry window, counted from now and fixed on the
   * row. Emits `approval.resolved` in a session.
   * Undefined when it was already decided (or, for approve/deny, has expired).
   */
  private async settle(
    id: string,
    decision: Decision,
    by: Owner | undefined,
    reason: string | null,
    where: Pick<DecideOptions, 'clientIp' | 'surface'> = {},
  ): Promise<ApprovalRecord | undefined> {
    const ttl = decision === 'approved' ? await this.expirySeconds() : 0
    // The decision and its `approval.resolved` commit together, so a watcher never sees
    // one without the other.
    let logged: { sessionId: string; events: ServerEvent[]; seqs: number[] } | undefined
    // Started only once the UPDATE has won, so a lost race leaves no span.
    const traced: { span?: Span; committed?: boolean } = {}
    try {
      const approval = await this.deps.sql.begin(async (tx) => {
        const [row] = await tx.unsafe<Row[]>(
          `UPDATE ai_approvals
           SET decision = $2, decided_by_kind = $3, decided_by_id = $4, decided_by_label = $5,
               decided_at = now(), reason = $6,
               usable_until = CASE WHEN $2 = 'approved' THEN now() + ($7 * interval '1 second') END
           WHERE id = $1 AND decision IS NULL AND ($2 IN ('expired', 'cancelled') OR expires_at > now())
           RETURNING ${COLUMNS}`,
          [id, decision, by?.kind ?? null, by?.id ?? null, by?.label ?? null, reason, ttl],
        )
        if (!row) return undefined
        const settled = record(row)
        // In the same transaction, so whoever sees the decision sees its span too.
        traced.span = decisionSpan(settled, by)
        const traceparent = traceparentOf(traced.span)
        if (traceparent !== undefined) {
          await tx`UPDATE ai_approvals SET decision_traceparent = ${traceparent} WHERE id = ${id}`
          settled.decisionTraceparent = traceparent
        }
        if (settled.sessionId !== null) {
          const resolved = event({
            type: 'approval.resolved',
            sessionId: settled.sessionId,
            id,
            approved: decision === 'approved',
            decision,
            ...(by && (decision === 'approved' || decision === 'denied') ? { by } : {}),
            ...(reason && (decision === 'expired' || decision === 'cancelled') ? { reason } : {}),
          })
          const events = [
            scrubForLog(resolved, []),
            inputResolved(settled.sessionId, `approval:${id}`, 'approval', decision, decision === 'expired' || decision === 'cancelled' ? reason : null),
          ]
          logged = { sessionId: settled.sessionId, events, seqs: await this.deps.events.append(settled.sessionId, events, tx) }
        }
        return settled
      })
      traced.committed = true
      if (!approval) return undefined
      // Committed: wake followers, and announce it on the bus (#300).
      if (logged) this.deps.events.committed(logged.sessionId, logged.events, logged.seqs)
      await this.audited(approval, decision, auditOutcome(decision), by, reason, where)
      return approval
    } catch (err) {
      if (traced.span) {
        recordFailure(traced.span, err)
        // The span says the decision; a rollback means nothing was decided.
        if (!traced.committed) traced.span.setAttribute('scadbuddy.outcome', 'rolled_back')
      }
      throw err
    } finally {
      traced.span?.end()
    }
  }

  /** One `approval` row in the audit log (#258). */
  private async audited(
    approval: ApprovalRecord,
    action: string,
    outcome: AuditOutcome,
    by: Owner | undefined,
    reason: string | null,
    where: Pick<DecideOptions, 'clientIp' | 'surface'> = {},
  ): Promise<void> {
    await this.deps.audit?.record({
      kind: 'approval',
      action,
      surface: by ? (where.surface ?? 'http') : 'system',
      actor: by ?? SYSTEM_ACTOR,
      clientIp: where.clientIp,
      sessionId: approval.sessionId,
      turnId: approval.turnId,
      toolUseId: approval.toolUseId,
      tier: approval.tier,
      inputHash: approval.inputHash,
      inputSummary: approval.inputSummary,
      approvalId: approval.id,
      outcome,
      detail: `${approval.tool}${reason ? `: ${reason}` : ''} (requested by ${approval.requestedBy.label})`,
      startedAt: new Date(approval.createdAt),
      finishedAt: new Date(),
    })
  }

  private async authorize(principal: Owner, approval: ApprovalRecord, session: SessionAccess | null): Promise<void> {
    if (principal.kind === 'browser') return
    if (!(await this.hasGrant(principal))) {
      throw new ApprovalError(
        'forbidden',
        `${principal.label} may not decide approvals: outward actions need a human approval in the ScadBuddy UI, ` +
          'and another agent needs a per-token approval grant (spec §6, §8.2)',
      )
    }
    const own =
      sameOwner(principal, approval.requestedBy) ||
      (session !== null && (sameOwner(principal, session.owner) || sameOwner(principal, session.creator)))
    if (own) {
      throw new ApprovalError(
        'forbidden',
        `${principal.label} may not decide its own outward actions: an approval grant is for approving another ` +
          "agent's (spec §6); ask the user in the ScadBuddy UI",
      )
    }
  }

  /**
   * The decision API: the panel's `approval.decision` (through `decision()`),
   * the HTTP routes (routes/approvals.ts), and #251's `sessions.approve/deny`.
   */
  async decide(principal: Owner, id: string, approve: boolean, options: DecideOptions = {}): Promise<ApprovalRecord> {
    const approval = await this.row(id)
    const access = approval ? await this.visible(principal, approval) : false
    if (!approval || access === false || (options.sessionId !== undefined && options.sessionId !== approval.sessionId)) {
      throw new ApprovalError('not_found', `no approval ${id}`)
    }
    await this.authorize(principal, approval, access)
    if (options.inputHash !== undefined && options.inputHash !== approval.inputHash) {
      throw new ApprovalError(
        'input_mismatch',
        `approval ${id} is for a different input than the one you were shown; the call needs a new approval`,
      )
    }
    const settled = await this.settle(id, approve ? 'approved' : 'denied', principal, null, {
      clientIp: options.clientIp,
      ...(options.surface ? { surface: options.surface } : {}),
    })
    if (!settled) {
      const now = await this.row(id)
      if (now?.due) {
        await this.expire(id)
        throw new ApprovalError('expired', `approval ${id} expired before it was decided`)
      }
      if (now?.decision === 'expired') throw new ApprovalError('expired', `approval ${id} expired before it was decided`)
      throw new ApprovalError(
        'conflict',
        `approval ${id} was already ${now?.decision ?? 'decided'}`,
        conflictReason(now),
      )
    }
    return settled
  }

  /** The panel's `approval.decision` client message; the seam #266's socket calls. */
  async decision(
    principal: Owner,
    message: ApprovalDecisionMessage,
    where: Pick<DecideOptions, 'clientIp' | 'surface'> = {},
  ): Promise<ApprovalRecord> {
    if (message.v !== PROTOCOL_VERSION || message.type !== 'approval.decision') {
      throw new ApprovalError('invalid', 'not an approval.decision message')
    }
    return this.decide(principal, message.id, message.approve, { sessionId: message.sessionId, ...where })
  }

  private async expire(id: string): Promise<ApprovalRecord | undefined> {
    return this.settle(id, 'expired', undefined, 'no decision before it expired')
  }

  /**
   * Expires every pending approval past its time; returns how many. Taken `page` at a
   * time, oldest first, each page leaving out what this sweep already tried: a page of
   * rows that keep failing never holds back the ones after it (#2382). At most `pages`
   * pages a sweep: the next sweep takes up what is left.
   */
  async expireDue(page = EXPIRE_PAGE, pages = EXPIRE_PAGES): Promise<number> {
    let n = 0
    const tried: string[] = []
    // One approval that fails to expire never holds back the rest: the failures are
    // thrown once every other one has been tried, the first alone or all of them.
    const failures: unknown[] = []
    let full = false
    for (let taken = 0; taken < pages; taken += 1) {
      const due = await this.deps.sql.unsafe<{ id: string }[]>(
        `SELECT id FROM ai_approvals
         WHERE decision IS NULL AND expires_at <= now() AND NOT (id = ANY($1::uuid[]))
         ORDER BY expires_at LIMIT $2`,
        [tried, page],
      )
      for (const { id } of due) {
        tried.push(id)
        try {
          if (await this.expire(id)) n += 1
        } catch (err) {
          failures.push(err)
        }
      }
      full = due.length === page
      if (!full) break
    }
    // Said, so a backlog past the cap (rows that keep failing, say) is seen, not silent.
    if (full) console.warn(`approval expiry sweep stopped at its cap of ${pages} pages of ${page}; the rest wait for the next sweep`)
    if (failures.length === 1) throw failures[0]
    if (failures.length > 1) throw new AggregateError(failures, `${failures.length} approvals failed to expire`)
    return n
  }

  // -- prepared actions (MCP prepare / confirm_action, approvals/mcp.ts) ------

  /** A principal's sessionless approvals still waiting for a decision, oldest first. */
  async listPrepared(by: Owner): Promise<ApprovalRecord[]> {
    const rows = await this.deps.sql.unsafe<Row[]>(
      `SELECT ${COLUMNS} FROM ai_approvals
       WHERE session_id IS NULL AND requested_by_kind = $1 AND requested_by_id = $2
         AND decision IS NULL AND expires_at > now()
       ORDER BY created_at, id LIMIT 500`,
      [by.kind, by.id],
    )
    return rows.map(record)
  }

  /**
   * Records a sessionless pending approval (an MCP prepare) within bounds, in
   * one transaction under one advisory lock, so concurrent prepares cannot
   * overshoot them: the requester's oldest pending ones are cancelled to keep
   * it under `perPrincipal`; at `total` pending sessionless rows from anyone,
   * nothing is inserted and `undefined` is returned (nobody's row is evicted).
   */
  async createPrepared(
    request: Omit<CreateApproval, 'sessionId' | 'turnId'>,
    bounds: { perPrincipal: number; total: number; evictReason: string },
  ): Promise<ApprovalRecord | undefined> {
    const summary = await this.summary(request)
    const by = request.requestedBy
    // An eviction is a system cancellation: each evicted row gets its root
    // `agent.approval` span and `decision_traceparent` in this transaction, as
    // settle() gives one. The spans end once the transaction has settled.
    const evicted: Span[] = []
    try {
      const row = await this.deps.sql.begin(async (tx) => {
        // One key for every MCP prepare: the per-principal and the global
        // bound are both read and written under it. Held until commit.
        await tx`SELECT pg_advisory_xact_lock(hashtextextended(${PREPARE_LOCK}, 0))`
        const own = await tx<{ id: string }[]>`
          SELECT id FROM ai_approvals
          WHERE session_id IS NULL AND requested_by_kind = ${by.kind} AND requested_by_id = ${by.id}
            AND decision IS NULL AND expires_at > now()
          ORDER BY created_at, id`
        const evict = own.slice(0, Math.max(own.length - bounds.perPrincipal + 1, 0)).map((r) => r.id)
        if (evict.length > 0) {
          const cancelled = await tx.unsafe<Row[]>(
            `UPDATE ai_approvals SET decision = 'cancelled', decided_at = now(), reason = $2
             WHERE id = ANY($1::uuid[]) AND decision IS NULL
             RETURNING ${COLUMNS}`,
            [evict, bounds.evictReason],
          )
          for (const cancelledRow of cancelled) {
            const span = decisionSpan(record(cancelledRow), undefined)
            evicted.push(span)
            const traceparent = traceparentOf(span)
            if (traceparent !== undefined) {
              await tx`UPDATE ai_approvals SET decision_traceparent = ${traceparent} WHERE id = ${cancelledRow.id}`
            }
          }
        } else {
          const [n] = await tx<{ n: number }[]>`
            SELECT count(*)::int AS n FROM ai_approvals
            WHERE session_id IS NULL AND decision IS NULL AND expires_at > now()`
          if ((n?.n ?? 0) >= bounds.total) return undefined
        }
        const inserted = await this.insert(tx, { ...request, sessionId: null, turnId: null }, summary)
        if (!inserted) throw new Error('approval vanished after insert')
        return inserted
      })
      return row ? record(row) : undefined
    } catch (err) {
      // Only the transaction can throw here, and it rolled the cancellations back.
      for (const span of evicted) {
        recordFailure(span, err)
        span.setAttribute('scadbuddy.outcome', 'rolled_back')
      }
      throw err
    } finally {
      for (const span of evicted) span.end()
    }
  }

  /** Expires this one approval if it is pending and past its time. */
  async expireIfDue(id: string): Promise<void> {
    if ((await this.row(id))?.due) await this.expire(id)
  }

  /**
   * Uses a sessionless approval once (`confirm_action`): only the principal
   * that asked for it, only for the input it was asked (and approved) for,
   * only while it is approved, unused, not voided and within `usable_until`.
   * One UPDATE decides all of that, so two confirms cannot both win.
   */
  async consumePrepared(id: string, by: Owner, hash: string): Promise<ApprovalRecord | undefined> {
    if (!isUuid(id)) return undefined
    const [row] = await this.deps.sql.unsafe<Row[]>(
      `UPDATE ai_approvals SET consumed_at = now()
       WHERE id = $1 AND session_id IS NULL AND requested_by_kind = $2 AND requested_by_id = $3
         AND input_hash = $4 AND ${USABLE}
       RETURNING ${COLUMNS}`,
      [id, by.kind, by.id, hash],
    )
    return row ? record(row) : undefined
  }

  /**
   * Expires due approvals every `intervalMs` (once `ready` says the schema is
   * there) until the returned function is called.
   */
  startSweeper(
    intervalMs: number,
    options: { ready?: () => Promise<boolean>; onError?: (err: unknown) => void } = {},
  ): () => void {
    const ready = options.ready ?? (() => Promise.resolve(true))
    // One sweep at a time: a slow one (a pod just started) is not joined by the next tick.
    let running = false
    const timer = setInterval(() => {
      if (running) return
      running = true
      ready()
        .then((ok) => (ok ? this.expireDue() : 0))
        .catch(options.onError ?? (() => {}))
        .finally(() => {
          running = false
        })
    }, intervalMs)
    timer.unref()
    return () => clearInterval(timer)
  }
}

/**
 * A sweep's failure as its log line says it (#2382): the message, and for a Postgres
 * error the statement it came from (whitespace folded, cut to 300 characters), so the
 * next one names the query. Never the parameters: they can hold an input's summary.
 */
export function sweepErrorText(err: unknown): string {
  // Several failed: the first as one is said, and how many more.
  if (err instanceof AggregateError && err.errors.length > 0) {
    return `${sweepErrorText(err.errors[0])} (and ${err.errors.length - 1} more)`
  }
  const message = err instanceof Error ? err.message : String(err)
  const query = (err as { query?: unknown } | null)?.query
  if (typeof query !== 'string' || query.trim() === '') return message
  const folded = query.replace(/\s+/g, ' ').trim()
  return `${message} (in: ${folded.length > 300 ? `${folded.slice(0, 299)}…` : folded})`
}
