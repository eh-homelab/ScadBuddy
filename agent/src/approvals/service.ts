import { createHmac, hkdfSync, randomBytes, randomUUID } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import type { Tier } from '../auth/principal.js'
import type { ApprovalGate, ApprovalRequest, ApprovalVerdict, RiskTier } from '../harness/permissions.js'
import { isUuid } from '../harness/stateDirs.js'
import type { Kek } from '../secrets.js'
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
import {
  DurableRefused,
  type DurableSessions,
  DurableUnavailable,
  durableApprovalId,
  type PendingCall,
  parseDurableApprovalId,
} from '../durable/client.js'
import { type AuditOutcome, type AuditSink, type AuditSurface, SYSTEM_ACTOR } from '../audit/log.js'

// Approvals of outward tool calls (#258, spec §8.2: "Outward tools always need
// a human approval in the ScadBuddy UI, in every auth mode"). Stored in
// Postgres (`ai_approvals`, db/migrations/*_approvals.sql), so any replica can
// decide one and a pending one survives a restart.
//
// HOW A CALL WAITS. The harness gives canUseTool a gate (`gate()` below). The
// gate records a pending approval, sets the session to `waiting_approval`,
// emits `approval.required`, and PARKS the SDK's permission request until the
// row is decided: `canUseTool` may wait with no deadline of the SDK's own
// (harness/permissions.ts, measured in test/approvals.sdk.test.ts). The
// waiter wakes at once for a decision made on this replica, and polls the row
// every `pollMs` for one made on another. When the row is decided the gate
// answers the SDK: approved → the approval is used (`consumeById`) and the
// tool runs with the approved input; otherwise it is denied with a message
// the model reads.
//
// WHEN NOTHING IS WAITING. A restart (or a replica dying) ends the query, but
// not the row: the session stays `waiting_approval` and the approval stays
// pending until it is decided or expires. Deciding such an orphan:
//   - approve → the session resumes with a turn asking the model to make the
//     same call again (`resume`, the manager's resumeApproved), and the
//     approval is BOUND to that turn (`resume_turn_id`, `bindResume`). Only
//     that turn can use it (`consume`: same session, same turn, same tool,
//     same input hash, once, before `usable_until`). If the session cannot
//     start that turn, the approval is voided at once and the session gets an
//     `error` event saying so. This is the "deny-then-resume" fallback of spec
//     §3.2, needed only for orphans. The resumed turn runs with the tiers the
//     asking turn had (`requested_tiers`, #300), never more;
//   - deny → recorded; the session goes back to `idle`.
//   Resuming claims the session, and a new turn cancels the session's other
//   pending approvals (below): approving one of several orphans of the same
//   session cancels its siblings, whose calls the resumed turn is not asked
//   to repeat. The model can ask again for any it still needs.
//
// APPROVED BUT UNUSED NEVER OUTLIVES ITS TURN. An approved row that was not
// used is voided (`revoked_at`, an `error` event) when its session is
// interrupted or handed off, when a new turn starts, when the turn it was
// bound to (or that parked on it) ends, and when a resume fails. So no later
// turn, and no new owner after a handoff, can use an approval given for
// something else.
//
// BINDING. Each approval stores an HMAC-SHA256 of the tool name and the
// canonical JSON of the FULL input (`hash()`), keyed by a server-side key:
// derived from the key-encryption key (`approvalHashKey`), so a hash stays
// comparable across restarts and cannot be brute-forced back to a
// low-entropy input by someone who can read the table. A parked call runs
// with the copy of the input taken when it was hashed; an orphan's approval
// is used only by a call with the same hash, so a changed input needs a new
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
// ENDING WITHOUT A DECISION (`cancelPending`, called by the manager):
//   - interrupt: the approval is CANCELLED. Spec §8.6 lets any watcher
//     interrupt a runaway agent; an interrupted turn has nothing left to run
//     the call in, and a later approval must not surprise anyone;
//   - handoff: CANCELLED. The approval was asked for the previous owner's
//     turn; a grant-holding new owner must not inherit it (spec §6: ownership
//     moves "explicitly");
//   - a new turn in the session (a send, or another approval's resume):
//     CANCELLED, superseded;
//   - no decision by `expires_at`: EXPIRED. The window is the ai_settings key
//     `approval_expiry_seconds` (default below), read when the approval is
//     created; an approval, once approved, is usable until `usable_until`,
//     the same window counted from the decision. A parked call gives up at
//     expiry itself; orphans are expired by `expireDue()` (main.ts runs it
//     periodically, and list/decide run it first).
//   - a shutdown leaves the approval pending (it survives the restart).
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
export const DEFAULT_APPROVAL_POLL_MS = 1000
/** The all-pending list asks at most this many durable sessions, this many at a time, within this long. */
const DURABLE_LIST_MAX = 100
const DURABLE_LIST_CONCURRENCY = 8
const DURABLE_LIST_DEADLINE_MS = 15_000
/** The longest `approval.required` summary. */
export const APPROVAL_SUMMARY_MAX = 500
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
}

export type ApprovalErrorCode =
  | 'not_found'
  | 'forbidden'
  | 'conflict'
  | 'expired'
  | 'input_mismatch'
  | 'invalid'
  /** A durable session's workflow could not be asked (Temporal unreachable). */
  | 'unavailable'

const STATUS_OF: Record<ApprovalErrorCode, 400 | 403 | 404 | 409 | 410 | 503> = {
  not_found: 404,
  forbidden: 403,
  conflict: 409,
  expired: 410,
  input_mismatch: 409,
  invalid: 400,
  unavailable: 503,
}

/** A refused approval operation; `status` is the HTTP status a route answers with. */
export class ApprovalError extends Error {
  override name = 'ApprovalError'
  readonly code: ApprovalErrorCode
  readonly status: 400 | 403 | 404 | 409 | 410 | 503
  constructor(code: ApprovalErrorCode, message: string) {
    super(message)
    this.code = code
    this.status = STATUS_OF[code]
  }
}

/** Whether a non-browser principal holds a per-token approval grant (spec §6). */
export type GrantCheck = (principal: Owner) => Promise<boolean>

/** Reads ai_settings (credentials.ts SettingsStore is one). */
export type SettingsReader = { get<T>(key: string): Promise<T | undefined> }

/** Why a resume did not start, or that it did. */
export type ResumeResult = { resumed: true } | { resumed: false; reason: string }

export type ApprovalServiceDeps = {
  sql: Sql
  /** The session event log the approval events go to. */
  events: EventLog
  settings?: SettingsReader
  /** Per-token grants; none when omitted. */
  grants?: GrantCheck
  /**
   * Starts the turn that re-runs an approved orphan (manager.resumeApproved),
   * which binds the approval to it with `bindResume`.
   */
  resume?: (approval: ApprovalRecord, by: Owner) => Promise<ResumeResult>
  /**
   * The HMAC key for input hashes (`approvalHashKey`). Without one, a random
   * key for this process: hashes then do not survive a restart, so an orphan
   * approved after one is never used (safe) and its call needs a new approval.
   */
  hashKey?: Buffer
  pollMs?: number
  /**
   * The audit log (#258, audit/log.ts): every decision, expiry, cancellation
   * and void is recorded as an `approval` row.
   */
  audit?: AuditSink
  /**
   * A durable session's approvals live in its workflow (#1056, plan ruling 7): ids
   * `durable:<session>:<tool_use_id>` are decided through its `review` Update and listed
   * from its `pending_approvals` Query, never in ai_approvals.
   */
  durable?: Pick<DurableSessions, 'review' | 'pending'>
  /** How long the all-pending list waits for durable sessions' workflows (DURABLE_LIST_DEADLINE_MS). */
  durableListDeadlineMs?: number
}

/** The input-hash key, derived from the key-encryption key (HKDF-SHA256, its own label). */
export function approvalHashKey(kek: Kek): Buffer {
  return Buffer.from(hkdfSync('sha256', kek.key, Buffer.alloc(0), 'scadbuddy:ai_approvals:input_hash:v1', 32))
}

/** What `gate()` needs to know about the turn it parks. */
export type GateContext = {
  sessionId: string
  turnId: string
  /** The principal the turn runs for; recorded as `requested_by`. */
  requestedBy: Owner
  /** The turn's tiers, when not the owner's default; recorded as `requested_tiers`. */
  requestedTiers?: readonly Tier[]
  /** Redacted from the stored summary and the events (the turn's credential). */
  secrets: () => readonly string[]
  /** The turn's own abort signal (interrupt, shutdown). */
  signal: AbortSignal
}

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
  due: boolean
}

const COLUMNS = `id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
  requested_by_kind, requested_by_id, requested_by_label, requested_tiers, created_at, expires_at, decision,
  decided_by_kind, decided_by_id, decided_by_label, decided_at, reason, usable_until, resume_turn_id,
  consumed_at, revoked_at, (decision IS NULL AND expires_at <= now()) AS due`

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
  }
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

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/** What the model is told when its call was not approved. */
function refusal(approval: ApprovalRecord): string {
  const what = `${approval.tool} (approval ${approval.id})`
  switch (approval.decision) {
    case 'denied':
      return `The user denied ${what}; it was not run. Do not retry it unless the user asks you to.`
    case 'expired':
      return `Nobody decided on ${what} before it expired, so it was not run. Tell the user it needs their approval.`
    case 'cancelled':
      return `The approval for ${what} was cancelled (${approval.reason ?? 'no reason given'}), so it was not run.`
    case 'approved':
      return `The approval for ${what} was withdrawn before it ran (${approval.reason ?? 'no reason given'}), so it was not run.`
    default:
      return `${what} was not approved, so it was not run.`
  }
}

type SessionAccess = {
  owner: Owner
  creator: Pick<Owner, 'kind' | 'id'>
  /** A live handoff offer's target, who may see the session too (sessions/protocol.ts `canSee`). */
  offer: { to: Pick<Owner, 'kind' | 'id'> } | null
  status: string
  mode: string
  turnId: string | null
  turnActive: boolean
}

export type CreateApproval = {
  sessionId: string | null
  turnId: string | null
  toolUseId: string
  tool: string
  input: Record<string, unknown>
  tier: RiskTier
  requestedBy: Owner
  /** See GateContext.requestedTiers. */
  requestedTiers?: readonly Tier[]
  secrets?: readonly string[]
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

export type RevokeFilter = {
  /**
   * Only rows that the turn with this id parked on, and that no other turn
   * has been resumed for: a decision that landed after this turn released
   * the session may already have resumed it (`bindResume`), and that turn's
   * approval must survive this one's trailing clean-up.
   */
  turnId?: string
  /** Leave the row bound to this resumed turn alone. */
  exceptResumeTurn?: string
}

export class ApprovalService {
  private readonly deps: ApprovalServiceDeps
  private readonly pollMs: number
  private readonly hashKey: Buffer
  /** Parked waiters on this replica, by approval id: wake them at once. */
  private readonly waiters = new Map<string, Set<() => void>>()

  constructor(deps: ApprovalServiceDeps) {
    this.deps = deps
    this.pollMs = deps.pollMs ?? DEFAULT_APPROVAL_POLL_MS
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
        status: string
        mode: string
        turn_id: string | null
        turn_active: boolean
      }[]
    >`
      SELECT owner_kind, owner_id, owner_label, creator_kind, creator_id,
             CASE WHEN pending_owner_until > now() THEN pending_owner_kind END AS offer_kind,
             CASE WHEN pending_owner_until > now() THEN pending_owner_id END AS offer_id,
             status, mode, turn_id,
             (turn_id IS NOT NULL AND lease_until > now()) AS turn_active
      FROM ai_sessions WHERE id = ${id}`
    if (!row) return undefined
    return {
      owner: { kind: row.owner_kind, id: row.owner_id, label: row.owner_label },
      creator: { kind: row.creator_kind, id: row.creator_id },
      offer: row.offer_kind && row.offer_id ? { to: { kind: row.offer_kind, id: row.offer_id } } : null,
      status: row.status,
      mode: row.mode,
      turnId: row.turn_id,
      turnActive: row.turn_active,
    }
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
    if (approval.sessionId === null) {
      const everything = principal.kind === 'browser' || (await this.hasGrant(principal))
      return everything || sameOwner(principal, approval.requestedBy) ? null : false
    }
    return this.sessionVisible(principal, approval.sessionId)
  }

  /** The session-level half of `visible`: the browser user and grant holders see all, others theirs. */
  private async sessionVisible(principal: Owner, sessionId: string): Promise<SessionAccess | false> {
    const session = isUuid(sessionId) ? await this.session(sessionId) : undefined
    if (!session) return false
    const everything = principal.kind === 'browser' || (await this.hasGrant(principal))
    return everything || canSee(principal, session) ? session : false
  }

  /**
   * Whether `principal` may decide an approval of the session's that `requestedBy` asked
   * for: `visible` + `authorize` for an approval with no ai_approvals row (a durable one).
   */
  async authorizeSession(principal: Owner, sessionId: string, requestedBy?: Owner): Promise<SessionAccess> {
    const session = await this.sessionVisible(principal, sessionId)
    if (!session) throw new ApprovalError('not_found', `no session ${sessionId}`)
    await this.authorize(principal, requestedBy ?? session.owner, session)
    return session
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
      if (session.mode === 'durable') return this.durablePending(sessionId, session, { pending: pending === true })
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
    return [...rows.map(record), ...(await this.durableWaiting())]
  }

  /**
   * Every durable session's waiting calls, for `list` without a session (which only the
   * browser user and grant holders may ask for, as for classic ones). A session whose
   * workflow cannot be asked now is left out rather than failing the whole list.
   */
  private async durableWaiting(): Promise<ApprovalRecord[]> {
    if (!this.deps.durable) return []
    const sessions = await this.deps.sql<{ id: string }[]>`
      SELECT id FROM ai_sessions WHERE mode = 'durable' AND status = 'waiting_approval'
      ORDER BY updated_at LIMIT ${DURABLE_LIST_MAX}`
    // A few at a time, all within one deadline: a session not answered by then is left out.
    let timer: NodeJS.Timeout | undefined
    const deadline = new Promise<'late'>((resolve) => {
      timer = setTimeout(() => resolve('late'), this.deps.durableListDeadlineMs ?? DURABLE_LIST_DEADLINE_MS)
    })
    const found: ApprovalRecord[][] = sessions.map(() => [])
    let next = 0
    const worker = async () => {
      while (next < sessions.length) {
        const i = next++
        const id = sessions[i]!.id
        const listed = (async () => {
          const session = await this.session(id)
          return session ? await this.durablePending(id, session) : []
        })().catch((err: unknown) => {
          if (err instanceof ApprovalError && err.code === 'unavailable') return []
          throw err
        })
        const answer = await Promise.race([listed, deadline])
        if (answer === 'late') {
          listed.catch(() => {}) // answered after the deadline: left out
          return
        }
        found[i] = answer
      }
    }
    try {
      await Promise.all(Array.from({ length: Math.min(DURABLE_LIST_CONCURRENCY, sessions.length) }, worker))
    } finally {
      clearTimeout(timer)
    }
    return found.flat()
  }

  /** The latest approval a session's tool call asked for, if any (the audit log's link to it). */
  async idForToolUse(sessionId: string, toolUseId: string): Promise<string | undefined> {
    const [row] = await this.deps.sql<{ id: string }[]>`
      SELECT id FROM ai_approvals WHERE session_id = ${sessionId} AND tool_use_id = ${toolUseId}
      ORDER BY created_at DESC LIMIT 1`
    return row?.id
  }

  async hasPending(sessionId: string): Promise<boolean> {
    const [row] = await this.deps.sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM ai_approvals WHERE session_id = ${sessionId} AND decision IS NULL`
    return (row?.n ?? 0) > 0
  }

  // -- writes ------------------------------------------------------------------

  private async append(sessionId: string | null, events: ServerEvent[], secrets: readonly string[] = []): Promise<void> {
    if (sessionId === null || events.length === 0) return
    await this.deps.events.append(sessionId, events.map((e) => scrubForLog(e, secrets)))
  }

  /** The INSERT of a pending approval, on `db` (the pool, or a transaction). */
  private async insert(db: Sql | TransactionSql, request: CreateApproval, summary: string): Promise<Row | undefined> {
    const ttl = await this.expirySeconds()
    const { requestedBy: by } = request
    const [row] = await db.unsafe<Row[]>(
      `INSERT INTO ai_approvals (id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
                                 requested_by_kind, requested_by_id, requested_by_label, requested_tiers, expires_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now() + ($13 * interval '1 second'))
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
      ],
    )
    return row
  }

  /** Records a pending approval (and, in a session, emits `approval.required`). */
  async create(request: CreateApproval): Promise<ApprovalRecord> {
    const secrets = request.secrets ?? []
    const summary = summariseInput(request.tool, request.input, secrets)
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
      ]
      // Only the parked turn itself moves the session to waiting_approval.
      if (request.turnId !== null) {
        const moved = await tx`
          UPDATE ai_sessions SET status = 'waiting_approval', updated_at = now()
          WHERE id = ${created.sessionId} AND turn_id = ${request.turnId} AND status <> 'waiting_approval'`
        if (moved.count > 0) {
          tail.push(event({ type: 'session.status', sessionId: created.sessionId, status: 'waiting_approval' }))
        }
      }
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
   * row. Emits `approval.resolved` and wakes a waiter on this replica.
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
    // The decision and its `approval.resolved` commit together: a parked gate
    // polling the row must not see the decision (and log the session's
    // `running`) before the event that reports it is in the log.
    let logged: { sessionId: string; events: ServerEvent[]; seqs: number[] } | undefined
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
      if (settled.sessionId !== null) {
        const resolved = event({
          type: 'approval.resolved',
          sessionId: settled.sessionId,
          id,
          approved: decision === 'approved',
          ...(by && (decision === 'approved' || decision === 'denied') ? { by } : {}),
        })
        const events = [scrubForLog(resolved, [])]
        logged = { sessionId: settled.sessionId, events, seqs: await this.deps.events.append(settled.sessionId, events, tx) }
      }
      return settled
    })
    if (!approval) return undefined
    // Committed: wake followers, and announce it on the bus (#300).
    if (logged) this.deps.events.committed(logged.sessionId, logged.events, logged.seqs)
    this.wakeWaiters(id)
    await this.audited(approval, decision, auditOutcome(decision), by, reason, where)
    return approval
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

  /**
   * Sets a session that no longer waits on an approval back from
   * `waiting_approval`: to `waiting_input` while a question of the turn is
   * still unanswered (#940, questions/service.ts), to `running` while its turn
   * is live, else `idle`.
   */
  async refreshStatus(sessionId: string): Promise<void> {
    const [row] = await this.deps.sql<{ status: 'running' | 'idle' | 'waiting_input' }[]>`
      UPDATE ai_sessions
      SET status = CASE
            WHEN EXISTS (SELECT 1 FROM ai_questions WHERE session_id = ${sessionId} AND outcome IS NULL)
              THEN 'waiting_input'
            WHEN turn_id IS NOT NULL AND lease_until > now() THEN 'running'
            ELSE 'idle'
          END,
          updated_at = now()
      WHERE id = ${sessionId} AND status = 'waiting_approval'
        AND NOT EXISTS (SELECT 1 FROM ai_approvals WHERE session_id = ${sessionId} AND decision IS NULL)
      RETURNING status`
    if (row) await this.append(sessionId, [event({ type: 'session.status', sessionId, status: row.status })])
  }

  private async authorize(principal: Owner, requestedBy: Owner, session: SessionAccess | null): Promise<void> {
    if (principal.kind === 'browser') return
    if (!(await this.hasGrant(principal))) {
      throw new ApprovalError(
        'forbidden',
        `${principal.label} may not decide approvals: outward actions need a human approval in the ScadBuddy UI, ` +
          'and another agent needs a per-token approval grant (spec §6, §8.2)',
      )
    }
    const own =
      sameOwner(principal, requestedBy) ||
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
    const durable = parseDurableApprovalId(id)
    if (durable) return this.decideDurable(principal, durable.sessionId, durable.toolUseId, approve, options)
    const approval = await this.row(id)
    const access = approval ? await this.visible(principal, approval) : false
    if (!approval || access === false || (options.sessionId !== undefined && options.sessionId !== approval.sessionId)) {
      throw new ApprovalError('not_found', `no approval ${id}`)
    }
    await this.authorize(principal, approval.requestedBy, access)
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
      throw new ApprovalError('conflict', `approval ${id} was already ${now?.decision ?? 'decided'}`)
    }
    // A parked turn (on any replica) picks the decision up itself. A turn
    // that is finishing as the decision lands looks parked here; it voids
    // the approval itself once it has released the session
    // (sessions/manager.ts finish). Nothing is parked when the turn that
    // asked is gone (restart): resume or settle here.
    if (settled.sessionId !== null) {
      const session = await this.session(settled.sessionId)
      const parked = session?.turnActive === true && session.turnId === settled.turnId && settled.turnId !== null
      if (!parked) {
        await this.refreshStatus(settled.sessionId)
        if (approve) await this.resumeOrphan(settled, principal)
      }
    }
    return settled
  }

  /**
   * A durable approval (plan ruling 7): the same visibility and authority as `decide`,
   * then the workflow's `review` Update, whose validator refuses a call that is not
   * waiting or was decided (409 `conflict`: the timer's denial, a second click). The
   * decision is logged as `approval.resolved {approved, by}` once the Update succeeded.
   */
  private async decideDurable(
    principal: Owner,
    sessionId: string,
    toolUseId: string,
    approve: boolean,
    options: DecideOptions,
  ): Promise<ApprovalRecord> {
    const id = durableApprovalId(sessionId, toolUseId)
    const durable = this.deps.durable
    if (!durable || (options.sessionId !== undefined && options.sessionId !== sessionId)) {
      throw new ApprovalError('not_found', `no approval ${id}`)
    }
    const session = await this.authorizeSession(principal, sessionId)
    if (session.mode !== 'durable') throw new ApprovalError('not_found', `no approval ${id}`)
    const pending = (await this.durablePending(sessionId, session)).find((a) => a.id === id)
    if (options.inputHash !== undefined && pending && options.inputHash !== pending.inputHash) {
      throw new ApprovalError(
        'input_mismatch',
        `approval ${id} is for a different input than the one you were shown; the call needs a new approval`,
      )
    }
    try {
      await durable.review(sessionId, toolUseId, approve, `${principal.kind}:${principal.id}`)
    } catch (err) {
      if (err instanceof DurableRefused) throw new ApprovalError('conflict', err.message)
      if (err instanceof DurableUnavailable) throw new ApprovalError('unavailable', err.message)
      throw err
    }
    await this.append(sessionId, [event({ type: 'approval.resolved', sessionId, id, approved: approve, by: principal })])
    const decision: Decision = approve ? 'approved' : 'denied'
    const decided: ApprovalRecord = {
      ...(pending ?? this.durableRecord(sessionId, session, { id: toolUseId, name: toolUseId, input: {} }, new Date())),
      decision,
      decidedBy: principal,
      decidedAt: new Date().toISOString(),
    }
    await this.audited(decided, decision, auditOutcome(decision), principal, null, {
      clientIp: options.clientIp,
      ...(options.surface ? { surface: options.surface } : {}),
    })
    return decided
  }

  /**
   * A durable session's approvals: its waiting calls (the `pending_approvals` Query), and
   * with `pending: false` also those its log shows resolved (approved, denied, expired, or
   * stopped). Temporal unreachable is `unavailable` (503), never a 500.
   */
  private async durablePending(
    sessionId: string,
    session: SessionAccess,
    { pending = true }: { pending?: boolean } = {},
  ): Promise<ApprovalRecord[]> {
    let calls: PendingCall[]
    try {
      calls = (await this.deps.durable?.pending(sessionId)) ?? []
    } catch (err) {
      if (err instanceof DurableUnavailable) {
        throw new ApprovalError('unavailable', `the approvals of session ${sessionId} cannot be read: ${err.message}`)
      }
      throw err
    }
    if (calls.length === 0 && pending) return []
    // The log's approval events (parsed here, not by Postgres, which refuses some text as jsonb).
    const rows = await this.deps.sql<{ event: string; created_at: Date }[]>`
      SELECT event, created_at FROM ai_session_events
      WHERE session_id = ${sessionId} AND strpos(event, '"approval.') > 0 ORDER BY seq`
    const asked = new Map<string, { at: Date; tool: string; summary: string }>()
    const resolved = new Map<string, { at: Date; approved: boolean; by?: Owner; reason?: string }>()
    for (const row of rows) {
      const e = JSON.parse(row.event) as ServerEvent
      if (e.type === 'approval.required') asked.set(e.id, { at: row.created_at, tool: e.tool, summary: e.summary })
      if (e.type === 'approval.resolved') {
        resolved.set(e.id, {
          at: row.created_at,
          approved: e.approved,
          ...(e.by ? { by: e.by } : {}),
          ...(e.reason ? { reason: e.reason } : {}),
        })
      }
    }
    const expiry = await this.expirySeconds()
    const waiting = calls.map((c) => {
      const record = this.durableRecord(sessionId, session, c, asked.get(durableApprovalId(sessionId, c.id))?.at ?? new Date())
      // The run's own expiry (its timer's start plus the expiry fixed when it started);
      // an older worker's Query does not say, and then the current setting stands in.
      const expiresAt =
        typeof c.expires_at === 'string' && !Number.isNaN(Date.parse(c.expires_at))
          ? new Date(c.expires_at).toISOString()
          : new Date(Date.parse(record.createdAt) + expiry * 1000).toISOString()
      return { ...record, expiresAt }
    })
    if (pending) return waiting
    const open = new Set(waiting.map((a) => a.id))
    const decided: ApprovalRecord[] = []
    for (const [id, r] of resolved) {
      const ask = asked.get(id)
      if (!ask || open.has(id)) continue
      const space = ask.summary.indexOf(' ')
      const decision: Decision = r.approved ? 'approved' : r.by ? 'denied' : r.reason ? 'cancelled' : 'expired'
      decided.push({
        ...this.durableRecord(sessionId, session, { id: ask.tool, name: ask.tool, input: {} }, ask.at),
        tool: space > 0 ? ask.summary.slice(0, space) : ask.summary,
        inputSummary: space > 0 ? ask.summary.slice(space + 1) : '',
        inputHash: '',
        expiresAt: r.at.toISOString(),
        decision,
        decidedBy: r.by ?? null,
        decidedAt: r.at.toISOString(),
        reason: r.reason ?? null,
      })
    }
    return [...decided, ...waiting].sort((a, b) => Date.parse(a.createdAt) - Date.parse(b.createdAt))
  }

  private durableRecord(
    sessionId: string,
    session: SessionAccess,
    call: { id: string; name: string; input: unknown },
    createdAt: Date,
  ): ApprovalRecord {
    const input =
      typeof call.input === 'object' && call.input !== null && !Array.isArray(call.input)
        ? (call.input as Record<string, unknown>)
        : { input: call.input }
    const summary = typeof call.input === 'string' ? call.input : summariseInput(call.name, input, [])
    return {
      id: durableApprovalId(sessionId, call.id),
      sessionId,
      turnId: null,
      toolUseId: call.id,
      tool: call.name,
      inputSummary: cap(summary, APPROVAL_SUMMARY_MAX),
      inputHash: this.hash(call.name, input),
      tier: 'outward',
      requestedBy: session.owner,
      requestedTiers: null,
      createdAt: createdAt.toISOString(),
      expiresAt: createdAt.toISOString(),
      decision: null,
      decidedBy: null,
      decidedAt: null,
      reason: null,
      usableUntil: null,
      resumeTurnId: null,
      consumedAt: null,
      revokedAt: null,
    }
  }

  /** Resumes the session for an approved orphan; voids it, and says so, when it cannot. */
  private async resumeOrphan(approval: ApprovalRecord, by: Owner): Promise<void> {
    const sessionId = approval.sessionId
    if (sessionId === null) return
    let result: ResumeResult
    try {
      result = this.deps.resume ? await this.deps.resume(approval, by) : { resumed: false, reason: 'nothing can resume it here' }
    } catch (err) {
      result = { resumed: false, reason: describe(err) }
    }
    if (result.resumed) return
    await this.revoke(approval.id, `the session could not resume: ${result.reason}`)
    await this.refreshStatus(sessionId)
  }

  /** The panel's `approval.decision` client message; the seam #266's socket calls. */
  async decision(principal: Owner, message: ApprovalDecisionMessage): Promise<ApprovalRecord> {
    if (message.v !== PROTOCOL_VERSION || message.type !== 'approval.decision') {
      throw new ApprovalError('invalid', 'not an approval.decision message')
    }
    return this.decide(principal, message.id, message.approve, { sessionId: message.sessionId })
  }

  private async expire(id: string): Promise<ApprovalRecord | undefined> {
    const approval = await this.settle(id, 'expired', undefined, 'no decision before it expired')
    if (approval?.sessionId) await this.refreshStatus(approval.sessionId)
    return approval
  }

  /** Expires every pending approval past its time; returns how many. */
  async expireDue(): Promise<number> {
    const due = await this.deps.sql<{ id: string }[]>`
      SELECT id FROM ai_approvals WHERE decision IS NULL AND expires_at <= now() ORDER BY expires_at LIMIT 500`
    let n = 0
    for (const { id } of due) if (await this.expire(id)) n += 1
    return n
  }

  /** Emits the `error` event that tells watchers an approval was voided unused. */
  private async announceRevoked(rows: { id: string; session_id: string | null; tool: string }[], reason: string): Promise<void> {
    for (const r of rows) {
      await this.append(r.session_id, [
        event({
          type: 'error',
          ...(r.session_id ? { sessionId: r.session_id } : {}),
          code: 'approval_void',
          message: `approval ${r.id} for ${r.tool} was not used and no longer applies: ${reason}. Ask again if it is still needed.`,
        }),
      ])
      this.wakeWaiters(r.id)
      if (this.deps.audit) {
        const voided = await this.row(r.id)
        if (voided) await this.audited(voided, 'voided', 'refused', undefined, reason)
      }
    }
  }

  /** Voids one approved, unused approval. */
  private async revoke(id: string, reason: string): Promise<boolean> {
    const rows = await this.deps.sql<{ id: string; session_id: string | null; tool: string }[]>`
      UPDATE ai_approvals SET revoked_at = now(), reason = ${reason}
      WHERE id = ${id} AND decision = 'approved' AND consumed_at IS NULL AND revoked_at IS NULL
      RETURNING id, session_id, tool`
    await this.announceRevoked(rows, reason)
    return rows.length > 0
  }

  /** Voids a session's approved, unused approvals (see APPROVED BUT UNUSED); returns how many. */
  async revokeUnused(sessionId: string, reason: string, filter: RevokeFilter = {}): Promise<number> {
    const rows = await this.deps.sql<{ id: string; session_id: string | null; tool: string }[]>`
      UPDATE ai_approvals SET revoked_at = now(), reason = ${reason}
      WHERE session_id = ${sessionId} AND decision = 'approved' AND consumed_at IS NULL AND revoked_at IS NULL
        AND (${filter.turnId ?? null}::uuid IS NULL
             OR (turn_id = ${filter.turnId ?? null}::uuid
                 -- A row a later turn has already been resumed for is that turn's now.
                 AND (resume_turn_id IS NULL OR resume_turn_id = ${filter.turnId ?? null}::uuid)))
        AND (${filter.exceptResumeTurn ?? null}::uuid IS NULL
             OR resume_turn_id IS DISTINCT FROM ${filter.exceptResumeTurn ?? null}::uuid)
      RETURNING id, session_id, tool`
    await this.announceRevoked(rows, reason)
    return rows.length
  }

  /**
   * Cancels a session's pending approvals and voids its approved, unused ones
   * (interrupt, handoff, a new turn); returns how many. A parked call is
   * denied with `reason`. `keepResumeTurn`: the resumed turn starting now
   * keeps the approval bound to it.
   */
  async cancelPending(
    sessionId: string,
    reason: string,
    options: { refresh?: boolean; keepResumeTurn?: string } = {},
  ): Promise<number> {
    const pending = await this.deps.sql<{ id: string }[]>`
      SELECT id FROM ai_approvals WHERE session_id = ${sessionId} AND decision IS NULL ORDER BY created_at`
    let n = 0
    for (const { id } of pending) if (await this.settle(id, 'cancelled', undefined, reason)) n += 1
    n += await this.revokeUnused(sessionId, reason, options.keepResumeTurn ? { exceptResumeTurn: options.keepResumeTurn } : {})
    // `refresh: false`: the caller (a finishing turn) sets the status itself.
    if (n > 0 && options.refresh !== false) await this.refreshStatus(sessionId)
    return n
  }

  /**
   * Binds an approved orphan to the turn resuming for it; false when it can
   * no longer be used (voided, used, out of time, or bound already).
   */
  async bindResume(id: string, turnId: string): Promise<boolean> {
    const rows = await this.deps.sql.unsafe(
      `UPDATE ai_approvals SET resume_turn_id = $2
       WHERE id = $1 AND resume_turn_id IS NULL AND ${USABLE}`,
      [id, turnId],
    )
    return rows.count > 0
  }

  /**
   * Uses the approval bound to this resumed turn for exactly this tool and
   * input, once (see WHEN NOTHING IS WAITING).
   */
  async consume(sessionId: string, turnId: string, tool: string, hash: string): Promise<ApprovalRecord | undefined> {
    const [row] = await this.deps.sql.unsafe<Row[]>(
      `UPDATE ai_approvals SET consumed_at = now()
       WHERE id = (
         SELECT id FROM ai_approvals
         WHERE session_id = $1 AND resume_turn_id = $2 AND tool = $3 AND input_hash = $4 AND ${USABLE}
         ORDER BY decided_at LIMIT 1 FOR UPDATE SKIP LOCKED)
         AND ${USABLE}
       RETURNING ${COLUMNS}`,
      [sessionId, turnId, tool, hash],
    )
    return row ? record(row) : undefined
  }

  /** Marks one approved approval used; undefined if it is not usable (or already used). */
  async consumeById(id: string): Promise<ApprovalRecord | undefined> {
    const [row] = await this.deps.sql.unsafe<Row[]>(
      `UPDATE ai_approvals SET consumed_at = now() WHERE id = $1 AND ${USABLE} RETURNING ${COLUMNS}`,
      [id],
    )
    return row ? record(row) : undefined
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
    const summary = summariseInput(request.tool, request.input, request.secrets ?? [])
    const by = request.requestedBy
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
        await tx`
          UPDATE ai_approvals SET decision = 'cancelled', decided_at = now(), reason = ${bounds.evictReason}
          WHERE id = ANY(${evict}::uuid[]) AND decision IS NULL`
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

  // -- waiting -----------------------------------------------------------------

  private wakeWaiters(id: string): void {
    for (const wake of this.waiters.get(id) ?? []) wake()
  }

  private pause(id: string, signal: AbortSignal): Promise<void> {
    return new Promise((resolve) => {
      const set = this.waiters.get(id) ?? new Set()
      this.waiters.set(id, set)
      const done = () => {
        clearTimeout(timer)
        set.delete(done)
        if (set.size === 0 && this.waiters.get(id) === set) this.waiters.delete(id)
        signal.removeEventListener('abort', done)
        resolve()
      }
      const timer = setTimeout(done, this.pollMs)
      set.add(done)
      signal.addEventListener('abort', done, { once: true })
    })
  }

  /**
   * Waits until the approval is decided, expiring it when its time is up.
   * Rejects when `signal` aborts, and leaves the row as it is then: a
   * shutdown must not decide anything.
   */
  async waitFor(id: string, signal: AbortSignal): Promise<ApprovalRecord> {
    for (;;) {
      if (signal.aborted) throw signal.reason instanceof Error ? signal.reason : new Error('aborted')
      const approval = await this.row(id)
      if (!approval) throw new Error(`approval ${id} no longer exists`)
      const { due, ...rest } = approval
      if (rest.decision !== null) return rest
      if (due) {
        await this.expire(id)
        continue
      }
      await this.pause(id, signal)
    }
  }

  /** The canUseTool gate for one turn (harness/permissions.ts ApprovalGate). */
  gate(context: GateContext): ApprovalGate {
    return async (request: ApprovalRequest): Promise<ApprovalVerdict> => {
      // Claude Code can still be running briefly after its turn was aborted
      // (sessions/manager.ts finish); nothing it asks for then is recorded.
      if (context.signal.aborted) return { approved: false, message: 'The turn is stopping; nothing was run.' }
      // A copy taken now is what runs, so the input cannot change after the hash.
      const input = structuredClone(request.input)
      const hash = this.hash(request.toolName, input)
      // This turn resumes an orphan approved for this very call: use it once.
      const resumed = await this.consume(context.sessionId, context.turnId, request.toolName, hash)
      if (resumed) return { approved: true, input, approvalId: resumed.id, decision: 'approved' }

      const approval = await this.create({
        sessionId: context.sessionId,
        turnId: context.turnId,
        toolUseId: request.toolUseId,
        tool: request.toolName,
        input,
        tier: request.tier,
        requestedBy: context.requestedBy,
        ...(context.requestedTiers ? { requestedTiers: context.requestedTiers } : {}),
        secrets: context.secrets(),
      })
      // An abort (interrupt, shutdown) ends the wait and leaves the row
      // pending: the finishing turn cancels it or, on shutdown, keeps it
      // (sessions/manager.ts finish). The SDK has dropped the request by then.
      const decided = await this.waitFor(approval.id, AbortSignal.any([context.signal, request.signal]))
      await this.refreshStatus(context.sessionId)
      const source = { approvalId: decided.id, decision: decided.decision ?? undefined }
      if (decided.decision === 'approved') {
        if (await this.consumeById(decided.id)) return { approved: true, input, ...source }
        // Voided between the decision and now (interrupt, handoff).
        const now = await this.row(decided.id)
        return { approved: false, message: refusal(now ?? decided), ...source }
      }
      return { approved: false, message: refusal(decided), ...source }
    }
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
    const timer = setInterval(() => {
      ready()
        .then((ok) => (ok ? this.expireDue() : 0))
        .catch(options.onError ?? (() => {}))
    }, intervalMs)
    timer.unref()
    return () => clearInterval(timer)
  }
}
