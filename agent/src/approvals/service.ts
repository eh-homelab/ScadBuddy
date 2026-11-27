import { createHmac, randomUUID } from 'node:crypto'
import type { Tier } from '../auth/principal.js'
import type { RiskTier } from '../harness/permissions.js'
import { isUuid } from '../harness/stateDirs.js'
import { redact } from '../secrets.js'
import type { EventLog } from '../sessions/eventLog.js'
import { event, type Owner, sameOwner } from '../sessions/protocol.js'
import { scrubForLog } from '../sessions/sdkEvents.js'
import type { AuditSurface } from '../audit/log.js'
import { type Span, SpanKind } from '@opentelemetry/api'
import { linkTo, recordFailure, traceparentOf, tracer } from '../telemetry/trace.js'
import { type WorkspaceFiles, type WorkspaceRef, withWorkspaceSize } from '../workspace/store.js'
import type { ApprovalRoutes, NewApprovalRow } from './crudApprovals.js'

// Approvals of outward tool calls (#258, spec §8.2: "Outward tools always need
// a human approval in the ScadBuddy UI, in every auth mode"). Stored on the API
// (`ai_approvals`, approvals/crudApprovals.ts, area `approvals`), so any replica can
// decide one and a pending one survives a restart.
//
// WHO ASKS. An outward call over /mcp (#251's prepare/confirm, below). A session's
// turn parks on its workflow's gate instead (temporal/session/gate.ts, ai_pending_input;
// unified runtime plan Steps 6.3 and 9.7), so no release writes a pending session row
// here any more. A session's rows are its history: none is shown, listed or decided
// here (#7323; prod had none pending on 2026-11-23), and the sweep expires any left.
//
// BINDING. Each approval stores an HMAC-SHA256 of the tool name and the
// canonical JSON of the FULL input (`hash()`), keyed by a server-side key:
// the API derives it from the key-encryption key and releases it, so a hash stays
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
//     the call. "By another agent" is read strictly, so a grant cannot turn
//     into self-approval. A grant holder may see (and decide) other
//     principals' prepares, which is what the grant is for. `anonymous` (MCP auth `disabled`) is no exception:
//     spec §8.3, "Outward actions still need a human approval in the UI
//     (§8.2); that rule is independent of auth".
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
  /** The API's routes, area `approvals` (approvals/crudApprovals.ts). */
  routes: ApprovalRoutes
  /** The session event log the approval events go to. */
  events: EventLog
  settings?: SettingsReader
  /** Per-token grants; none when omitted. */
  grants?: GrantCheck
  /**
   * The HMAC key for input hashes, which the API derives from its key-encryption key and
   * releases (see BINDING). Read at each hash; it throws until the key is released.
   */
  hashKey: () => Buffer
  /** The workspace (#2329): a workspace upload's size is read from it for the card (`withWorkspaceSize`). */
  workspace?: WorkspaceFiles
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

/** An `ai_approvals` row as approvals/crudApprovals.ts answers it. */
export type ApprovalRow = {
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
type Row = ApprovalRow

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

  constructor(deps: ApprovalServiceDeps) {
    this.deps = deps
  }

  /** The input hash a decision binds to (see BINDING). */
  hash(tool: string, input: Record<string, unknown>): string {
    return inputHash(this.deps.hashKey(), tool, input)
  }

  // -- reads -------------------------------------------------------------------

  async expirySeconds(): Promise<number> {
    const value = await this.deps.settings?.get<number>(SETTING_APPROVAL_EXPIRY_SECONDS)
    if (typeof value !== 'number' || !Number.isFinite(value)) return DEFAULT_APPROVAL_EXPIRY_SECONDS
    return Math.min(Math.max(Math.round(value), MIN_APPROVAL_EXPIRY_SECONDS), MAX_APPROVAL_EXPIRY_SECONDS)
  }

  private async row(id: string): Promise<(ApprovalRecord & { due: boolean }) | undefined> {
    if (!isUuid(id)) return undefined
    const row = await this.deps.routes.row(id)
    return row ? { ...record(row), due: row.due } : undefined
  }

  /** Whether a non-browser principal holds the per-token approval grant (spec §6). */
  async holdsGrant(principal: Owner): Promise<boolean> {
    return this.hasGrant(principal)
  }

  /**
   * Every pending approval `principal` may see, oldest first: all of them for the
   * browser user and grant holders; for anyone else, its own (MCP prepares). A
   * session's rows are left out (see WHO ASKS).
   */
  async listVisiblePending(principal: Owner): Promise<ApprovalRecord[]> {
    await this.expireDue()
    const everything = principal.kind === 'browser' || (await this.hasGrant(principal))
    const rows = everything ? await this.deps.routes.pending() : await this.deps.routes.prepared(principal)
    return rows.map(record)
  }

  private async hasGrant(principal: Owner): Promise<boolean> {
    return principal.kind !== 'browser' && this.deps.grants !== undefined && (await this.deps.grants(principal))
  }

  /**
   * Whether `principal` may see the approval: the browser user and grant holders
   * see all, others their own. A session's row is no one's (see WHO ASKS).
   */
  private async visible(principal: Owner, approval: ApprovalRecord): Promise<boolean> {
    if (approval.sessionId !== null) return false
    return principal.kind === 'browser' || sameOwner(principal, approval.requestedBy) || (await this.hasGrant(principal))
  }

  /** One approval, if the principal may see it. */
  async get(id: string, principal: Owner): Promise<ApprovalRecord> {
    const approval = await this.row(id)
    if (!approval || !(await this.visible(principal, approval))) {
      throw new ApprovalError('not_found', `no approval ${id}`)
    }
    const { due: _due, ...rest } = approval
    return rest
  }

  // -- writes ------------------------------------------------------------------

  /** A pending approval's row as the API's routes take it: its summary and hash made here. */
  private async newRow(request: CreateApproval, summary: string): Promise<NewApprovalRow> {
    const by = request.requestedBy
    return {
      id: randomUUID(),
      session_id: request.sessionId,
      turn_id: request.turnId,
      tool_use_id: request.toolUseId,
      tool: request.tool,
      input_summary: summary,
      input_hash: this.hash(request.tool, request.input),
      tier: request.tier,
      requested_by: { kind: by.kind, id: by.id, label: by.label },
      requested_tiers: request.requestedTiers ? [...request.requestedTiers] : null,
      expiry_s: await this.expirySeconds(),
      traceparent: request.traceparent ?? null,
    }
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
   * createPrepared writes one now (an MCP prepare, sessionless); tests also seed rows,
   * a session's among them as an earlier release left one.
   */
  async create(request: CreateApproval): Promise<ApprovalRecord> {
    const summary = await this.summary(request)
    // The summary is scrubbed of the turn's secrets already, and the tool and its use id carry none.
    const made = await this.deps.routes.create(await this.newRow(request, summary))
    for (const l of made.logged) this.deps.events.committed(l.session_id, l.events, l.seqs)
    return record(made.approval)
  }

  /**
   * Decides a pending approval, once: the first decision wins. An approval becomes usable for
   * the expiry window, counted from now and fixed on the row. The API writes the decision, its
   * span's context, its events (`approval.resolved` in a session) and its audit row in one
   * transaction. The span is started here from the row as read before; a decision that loses
   * (another won, or it expired) never ends it, so it is never exported.
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
    const before = await this.deps.routes.row(id)
    if (!before || before.decision !== null) return undefined
    const now = new Date().toISOString()
    const span = decisionSpan({ ...record(before), decision, decidedAt: now }, by)
    let settled
    try {
      settled = await this.deps.routes.settle({ id, decision, by, reason, expiryS: ttl, traceparent: traceparentOf(span), surface: where.surface, clientIp: where.clientIp })
    } catch (err) {
      // Whether it was written is not known: the span says so.
      recordFailure(span, err)
      span.setAttribute('scadbuddy.outcome', 'unknown')
      span.end()
      throw err
    }
    if (!settled.approval) return undefined
    span.end()
    for (const l of settled.logged) this.deps.events.committed(l.session_id, l.events, l.seqs)
    return record(settled.approval)
  }

  private async authorize(principal: Owner, approval: ApprovalRecord): Promise<void> {
    if (principal.kind === 'browser') return
    if (!(await this.hasGrant(principal))) {
      throw new ApprovalError(
        'forbidden',
        `${principal.label} may not decide approvals: outward actions need a human approval in the ScadBuddy UI, ` +
          'and another agent needs a per-token approval grant (spec §6, §8.2)',
      )
    }
    if (sameOwner(principal, approval.requestedBy)) {
      throw new ApprovalError(
        'forbidden',
        `${principal.label} may not decide its own outward actions: an approval grant is for approving another ` +
          "agent's (spec §6); ask the user in the ScadBuddy UI",
      )
    }
  }

  /**
   * The decision API: the pending-input respond route, the HTTP routes
   * (routes/approvals.ts), and #251's `sessions.approve/deny`.
   */
  async decide(principal: Owner, id: string, approve: boolean, options: DecideOptions = {}): Promise<ApprovalRecord> {
    const approval = await this.row(id)
    if (!approval || !(await this.visible(principal, approval)) || (options.sessionId !== undefined && options.sessionId !== approval.sessionId)) {
      throw new ApprovalError('not_found', `no approval ${id}`)
    }
    await this.authorize(principal, approval)
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
      const due = await this.deps.routes.due(tried, page)
      for (const id of due) {
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
    return (await this.deps.routes.prepared(by)).map(record)
  }

  /**
   * Records a sessionless pending approval (an MCP prepare) within bounds, in one transaction
   * under one lock on the API, so concurrent prepares cannot overshoot them: the requester's
   * oldest pending ones are cancelled to keep it under `perPrincipal`; at `total` pending
   * sessionless rows from anyone, nothing is inserted and `undefined` is returned (nobody's row
   * is evicted).
   *
   * An eviction is a system cancellation with its own root `agent.approval` span. A span is
   * started here before the call, one for each of the asker's pending rows past its bound as
   * last read, so the API writes each evicted row's context with its cancellation; the spans the
   * API did not use are never ended, so never exported. The count is read before the API takes
   * its lock, so a concurrent prepare of the same asker can make the API evict more rows than
   * there are spans: those rows are cancelled all the same, with no `decision_traceparent`.
   */
  async createPrepared(
    request: Omit<CreateApproval, 'sessionId' | 'turnId'>,
    bounds: { perPrincipal: number; total: number; evictReason: string },
  ): Promise<ApprovalRecord | undefined> {
    const summary = await this.summary(request)
    const own = await this.deps.routes.prepared(request.requestedBy)
    const spans = Array.from({ length: Math.max(own.length - bounds.perPrincipal + 1, 0) }, () =>
      tracer().startSpan('agent.approval', { kind: SpanKind.INTERNAL, root: true }),
    )
    const traceparents = spans.map((span) => traceparentOf(span)).filter((t): t is string => t !== undefined)
    let made
    try {
      made = await this.deps.routes.createPrepared(await this.newRow({ ...request, sessionId: null, turnId: null }, summary), bounds, traceparents)
    } catch (err) {
      // Which rows it evicted, and whether, is not known: each span says so.
      for (const span of spans) {
        recordFailure(span, err)
        span.setAttribute('scadbuddy.outcome', 'unknown')
        span.end()
      }
      throw err
    }
    made.evicted.forEach((row, i) => {
      const span = spans[i]
      if (span === undefined) return
      const evicted = record(row)
      // Started before the API chose the row, so linked to its parked span only now.
      const link = linkTo(evicted.traceparent)
      if (link) span.addLink(link)
      const waited = evicted.decidedAt ? (Date.parse(evicted.decidedAt) - Date.parse(evicted.createdAt)) / 1000 : 0
      span.setAttributes({
        'scadbuddy.approval_id': evicted.id,
        'scadbuddy.tool': evicted.tool,
        'scadbuddy.tier': evicted.tier,
        'scadbuddy.outcome': 'cancelled',
        'scadbuddy.wait_seconds': Math.max(waited, 0),
      })
      span.end()
    })
    return made.approval ? record(made.approval) : undefined
  }

  /** Expires this one approval if it is pending and past its time. */
  async expireIfDue(id: string): Promise<void> {
    if ((await this.row(id))?.due) await this.expire(id)
  }

  /**
   * Uses a sessionless approval once (`confirm_action`): only the principal
   * that asked for it, only for the input it was asked (and approved) for,
   * only while it is approved, unused, not voided and within `usable_until`.
   * One UPDATE on the API decides all of that, so two confirms cannot both win.
   */
  async consumePrepared(id: string, by: Owner, hash: string): Promise<ApprovalRecord | undefined> {
    if (!isUuid(id)) return undefined
    const used = await this.deps.routes.consume(id, by, hash)
    return used ? record(used) : undefined
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
