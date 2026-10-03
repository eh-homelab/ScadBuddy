import { randomUUID } from 'node:crypto'
import path from 'node:path'
import {
  forkSession as sdkForkSession,
  type McpSdkServerConfigWithInstance,
  type SDKMessage,
  type SDKResultMessage,
} from '@anthropic-ai/claude-agent-sdk'
import type { Sql } from 'postgres'
import type { Tier } from '../auth/principal.js'
import type { Credential } from '../credentials.js'
import { redact } from '../secrets.js'
import type { ProbeVerdict } from '../harness/credentialErrors.js'
import { type CredentialSource, runWithFallback } from '../harness/fallback.js'
import type { HarnessPaths } from '../harness/options.js'
import type { TierResolver } from '../harness/permissions.js'
import {
  DEFAULT_MAX_BUDGET_USD,
  DEFAULT_MAX_TURNS,
  harnessTierOf,
  type HarnessRun,
  runHarness,
} from '../harness/run.js'
import { browserTierOf, browserToolsGuide, GRANT_SERVER, SETTING_HEADLESS_BROWSER } from '../harness/headlessBrowser.js'
import { headlessGrantServer } from '../harness/headlessGrants.js'
import {
  HTTP_SERVER,
  type HttpLimits,
  httpRequestEnabled,
  httpRequestServer,
  httpTierOf,
  SETTING_HTTP_REQUEST,
} from '../harness/httpRequest.js'
import type { Resolver } from '../http/egress.js'
import { loadApprovedOrigins, rememberApprovedOrigin } from '../harness/browserOrigins.js'
import type { PluginsForRun } from '../plugins/forwarder.js'
import type { CheckedPlugin } from '../plugins/registry.js'
import {
  createMemoryHooks,
  HindsightClient,
  type MemoryActivity,
  type MemoryHooks,
  type MemoryHooksOptions,
  hindsightTarget,
} from '../memory/hindsight.js'
import type { PackagesForRun } from '../plugins/packages/install.js'
import {
  ensureSessionDir,
  isUuid,
  removeSessionBrowserDirs,
  sessionBrowserDir,
  sessionBrowserTmpDir,
  sessionWorkDir,
} from '../harness/stateDirs.js'
import { type ApprovalRecord, ApprovalService, type GrantCheck, type ResumeResult } from '../approvals/service.js'
import { QuestionService } from '../questions/service.js'
import { isQuestionTool } from '../harness/questions.js'
import { type AuditContext, type AuditLog, safeDetail } from '../audit/log.js'
import { TurnAuditor } from '../audit/turn.js'
import { UNTRUSTED_CONTENT_POLICY } from '../safety/untrusted.js'
import type { AppendHook } from './busEvents.js'
import { EventLog, type LoggedEvent } from './eventLog.js'
import {
  canSee,
  event,
  type Origin,
  type Owner,
  ownerSeenBy,
  publicLabel,
  sameOwner,
  type ServerEvent,
  type SessionStatus,
} from './protocol.js'
import { scrubForLog, SdkEventMapper } from './sdkEvents.js'
import { PostgresSessionStore } from './store.js'
import { SessionResources, type TouchedRecord } from './touched.js'

// The session manager (#300, spec §6): durable, shared sessions that a human
// in the browser, an external agent over /mcp, or an internal flow can start,
// watch, steer, fork, interrupt and hand off.
//
//   start     create a session (and optionally send its first turn)
//   send      add a user turn: one turn at a time per session, owner only
//   get/list  what a principal may see
//   fork      branch a session (SDK `forkSession`), parent recorded
//   interrupt stop the running turn, from any replica, by any watcher
//   handoff   move ownership explicitly: to the browser user at once, to
//             another MCP principal as an offer only it may accept
//   attach    replay the session's events, then follow them live
//
// SEAMS:
//   - The `sessions_*` tools (#300, src/tools/sessions.ts) sit on these
//     methods, over /mcp and in-process alike: sessions_list → list,
//     sessions_start → start (origin 'mcp', or 'chat' for the browser user),
//     sessions_send → send, sessions_get → get + the event log,
//     sessions_attach → attach, sessions_fork → fork, sessions_interrupt →
//     interrupt, sessions_handoff → handoff, sessions_accept_handoff →
//     acceptHandoff, sessions_cancel_handoff → cancelHandoff, and sessions_approve/deny →
//     approvals.decide (#258, src/approvals/service.ts) with the token's
//     approval grant as `approvalGrants` (auth/tokens.ts `approvalGrantCheck`).
//     The caller's principal is mapped to an `Owner` by approvals/mcp.ts
//     `ownerOf`, and a start or send passes its tiers (`tiers` below), which
//     the turn's in-process tools run with. An approval a turn asks for
//     records them, so resuming it restores them (`resumeApproved`).
//   - The registry supplies `tierOf` and the in-process MCP servers
//     (`mcpServers` below; main.ts passes tools/harness.ts `harnessTools`).
//     Tool payloads: tool.call inputs and tool.result summaries go into the
//     durable, multi-watcher event log, scrubbed only by sdkEvents.ts
//     `scrubForLog` (the turn's credential, arguments named like secrets, a
//     size cap). No registry tool takes a secret argument; one that takes a
//     secret under another name must declare it to the registry, and
//     scrubForLog must read that declaration.
//   - #266's WebSocket gateway maps the panel's client messages onto send
//     (user.message), interrupt, handoff and attach, and sends `snapshot()`.
//   - `onAppend` publishes `session.*` on the bus, and main.ts's LISTEN
//     consumer calls EventLog.wake() (busEvents.ts, #300).
//
// Concurrency. A turn CLAIMS its session row with one conditional UPDATE
// (status 'running', a fresh turn_id, a lease), so two sends — on one replica
// or on two — cannot both win; the loser gets SessionError 'busy' (spec §6:
// "a send while a turn is running gets a clear error"). The running turn
// renews the lease every `renewMs`; if its replica dies, the lease runs out
// after `leaseMs` and the session can be sent to again, and the reaper
// (`reapExpired`, every 30 s from main.ts) ends that turn: an interrupted event
// and the session back to idle, since its own finish() never ran. The same
// renewal reads `interrupt_requested`, which is how an interrupt reaches a turn
// running on another replica. On SIGTERM, `stopTurns` drains: no new turn,
// running ones may finish, the rest are aborted and waited for.
//
// Approvals (#258). Every turn's queries get `approvals.gate()`: an outward
// call parks the turn in `waiting_approval` until a human decides
// (src/approvals/service.ts has the whole flow). interrupt and handoff cancel
// a session's pending approvals, and so does claiming a new turn; a shutdown
// leaves them pending, and the session `waiting_approval`, across the restart.
// Approving such an orphan resumes the session through `resumeApproved`.
//
// Budget and turns. Each session gets `max_turns` and `budget_usd` from
// ai_settings at start (keys below; defaults from harness/run.ts). max_turns
// is the SDK's per-query `maxTurns`; the budget is for the whole session: a
// turn is given what is left as `maxBudgetUsd`, and a spent session refuses
// sends. Settings writes the two keys (routes/sessionLimits.ts, #790); only
// the browser user can raise one session's budget (`raiseBudget`), because
// that spends money.

/** ai_settings keys (non-secret, spec §9). */
export const SETTING_MODEL = 'model'
export const SETTING_SESSION_MAX_TURNS = 'session_max_turns'
export const SETTING_SESSION_BUDGET_USD = 'session_max_budget_usd'

/** The largest session budget Settings takes, and the most a raise can take one session's budget to. */
export const MAX_SESSION_BUDGET_USD = 100
/** The largest `max_turns` per reply Settings takes. */
export const MAX_SESSION_MAX_TURNS = 200

/** Money as the user reads it: dollars, rounded to cents ("$0.03", never "$0.025546000000000007"). */
export function usd(amount: number): string {
  return `$${amount.toFixed(2)}`
}

/** Dollars to whole cents, so a budget stored from a form is never a long float. */
export function cents(amount: number): number {
  return Math.round(amount * 100) / 100
}

/** abortAll()'s abort reason: a shutdown, which leaves pending approvals pending. */
export const SHUTTING_DOWN = 'shutting down'

function abortMessage(signal: AbortSignal): string {
  const reason: unknown = signal.reason
  return reason instanceof Error ? reason.message : 'the turn was interrupted'
}

export const DEFAULT_LEASE_MS = 60_000
/** How long a handoff offer waits for its target (`handoff`). */
export const HANDOFF_OFFER_TTL_MS = 60 * 60_000
export const DEFAULT_RENEW_MS = 1_000

export type SessionErrorCode =
  | 'not_found'
  | 'forbidden'
  | 'busy'
  | 'budget_exhausted'
  | 'closed'
  | 'invalid'
  | 'rate_limited'

type SessionErrorStatus = 400 | 403 | 404 | 409 | 429

const STATUS_OF: Record<SessionErrorCode, SessionErrorStatus> = {
  not_found: 404,
  forbidden: 403,
  busy: 409,
  budget_exhausted: 409,
  closed: 409,
  invalid: 400,
  rate_limited: 429,
}

/**
 * New sessions one owner may start per window, across every route (the chat
 * socket and POST /api/v1/ai/sessions), every connection and every replica:
 * counted from `ai_sessions.created_at`. Each session has its own budget, so
 * this is what bounds how many an owner can have spending at once.
 */
export const MAX_NEW_SESSIONS = 10
export const NEW_SESSION_WINDOW_MS = 60_000

/** A refused session operation; `status` is the HTTP status a route would answer with. */

/** The most of one memory, or of a memory event's input, a panel event carries. */
export const MEMORY_TEXT_MAX = 4000
/**
 * A memory event's query or memory text, redacted and then capped. Redaction
 * must come first: a secret the cut splits would no longer match, and its
 * fragment would reach the log.
 */
export function memoryText(text: string, secrets: readonly (string | undefined)[]): string {
  const clean = redact(text, secrets)
  return clean.length > MEMORY_TEXT_MAX ? `${clean.slice(0, MEMORY_TEXT_MAX)}… (${clean.length} characters)` : clean
}

export class SessionError extends Error {
  override name = 'SessionError'
  readonly code: SessionErrorCode
  readonly status: SessionErrorStatus
  /** With `budget_exhausted`: what the session spent and may spend, for the panel's meter. */
  readonly budget: { costUsd: number; budgetUsd: number } | undefined
  constructor(code: SessionErrorCode, message: string, budget?: { costUsd: number; budgetUsd: number }) {
    super(message)
    this.code = code
    this.status = STATUS_OF[code]
    this.budget = budget
  }
}

/** A pending handoff (`handoff`): who may accept it, and until when. */
export type HandoffOffer = { to: Owner; until: string }

export type SessionRecord = {
  id: string
  origin: Origin
  owner: Owner
  creator: Pick<Owner, 'kind' | 'id'>
  /** The live handoff offer, if any; an expired one reads as none. */
  offer: HandoffOffer | null
  status: SessionStatus
  title: string
  tags: string[]
  scope: Record<string, unknown>
  parentId: string | null
  maxTurns: number
  budgetUsd: number
  costUsd: number
  turns: number
  /** A turn holds the claim (on some replica) right now. */
  turnActive: boolean
  createdAt: string
  updatedAt: string
}

export type TurnOutcome =
  | { kind: 'result'; subtype: SDKResultMessage['subtype']; costUsd: number; turns: number }
  | { kind: 'interrupted' }
  | { kind: 'failed'; message: string }
  /** Another replica took the claim over after this one's lease ran out. */
  | { kind: 'lost_claim' }

export type Turn = { turnId: string; done: Promise<TurnOutcome> }

export type StartOptions = {
  origin: Origin
  title?: string
  tags?: string[]
  scope?: Record<string, unknown>
  /** Sent as the first turn when given. */
  prompt?: string
  /** With `prompt`: see SendOptions.context. */
  context?: string
  /** With `prompt`: see SendOptions.tiers. */
  tiers?: readonly Tier[]
}

export type SendOptions = {
  /**
   * Text the model gets after the user's message in this turn only, and that
   * the transcript's `user.turn` event does not show: the panel's page context
   * (route, open model, what the page reports, #256), rendered by the chat
   * route (routes/chat.ts `renderPageContext`).
   */
  context?: string
  /**
   * The sender's tiers (spec §8.1), for the turn's in-process tools: an MCP
   * token's, as `/mcp` authenticated it for this send (tools/sessions.ts).
   * Left out, the owner's default applies (auth/principal.ts
   * `harnessPrincipal`: everything for the browser user, `read` otherwise).
   */
  tiers?: readonly Tier[]
}

/** Who a turn's in-process tools act for, beyond the session's owner (SendOptions.tiers). */
export type TurnPrincipal = { tiers?: readonly Tier[] }

export type ListFilter = { status?: SessionStatus; origin?: Origin; limit?: number }

/** Reads ai_settings; SettingsStore (credentials.ts) is one. */
export type SettingsReader = { get<T>(key: string): Promise<T | undefined> }

/** Runs one query; `runHarness` in production, a scripted stand-in in unit tests. */
export type QueryRunner = (run: HarnessRun) => AsyncIterable<SDKMessage>

export type SessionManagerDeps = {
  sql: Sql
  paths: HarnessPaths
  /**
   * The Claude credentials a turn may use, in priority order, and where what
   * each attempt learned goes (harness/fallback.ts, #1093). `candidates`
   * throws when none is usable now.
   */
  credentials: CredentialSource
  /** Asks the endpoint about a refused or rate-limited credential; fallback.ts's own probe when omitted. */
  probe?: (credential: Credential, model: string | undefined) => Promise<ProbeVerdict>
  settings?: SettingsReader
  tierOf?: TierResolver
  /** #251's registry: the in-process MCP servers a session's queries get. */
  mcpServers?: (session: SessionRecord, turn: TurnPrincipal) => Record<string, McpSdkServerConfigWithInstance>
  pluginPaths?: string[]
  /** ScadBuddy's own plugin (harness/ownPlugin.ts, #896); its Skill and Agent tools come with it. */
  ownPlugin?: string
  /**
   * The registered, enabled remote MCP plugins for a turn (#297), registered
   * with the loopback forwarder: in production
   * `forwardForRun(await loadEnabledPlugins(store, kek), forwarder)`. Read once
   * per turn, so enabling or changing a plugin applies from the next turn on;
   * released when the turn ends.
   */
  remotePlugins?: () => Promise<PluginsForRun>
  /**
   * The enabled plugin packages for a turn (#297), each materialised from its
   * pin and verified: in production
   * `loadPackagesForRun(packageStore, installer)`. Read once per turn; a
   * package that cannot be loaded is reported in the session and left out.
   */
  packagePlugins?: () => Promise<PackagesForRun>
  /**
   * Automatic memory (memory/hindsight.ts): when a turn's remote plugins
   * include an enabled `hindsight` plugin, its prompt is recalled against and
   * its transcript retained to that plugin's bank. These are the hook knobs
   * (hooks.py `MemoryHookConfig` and friends); `false` turns it off.
   */
  memory?: Omit<MemoryHooksOptions, 'client' | 'secrets' | 'log' | 'sessionStore' | 'onActivity'> | false
  /**
   * The headless browser (#349, spec §5.3). A session's turns get it only when
   * this is set AND the `headless_browser_enabled` setting is `true`; it is off
   * by default. `backendUrl` is SCADBUDDY_BACKEND_URL, which serves the SPA;
   * `publicUrl` and `uiOrigins` (SCADBUDDY_PUBLIC_URL, SCADBUDDY_ALLOWED_ORIGINS)
   * are rewritten onto it; `browserAllowedOrigins`
   * (SCADBUDDY_BROWSER_ALLOWED_ORIGINS) is what else a human may let it open,
   * once per origin per session (harness/browserOrigins.ts, `ai_browser_origins`).
   */
  headlessBrowser?: {
    backendUrl: string
    publicUrl?: string
    uiOrigins?: string
    browserAllowedOrigins?: string
    /** Tests only: a Chromium other than the pinned one. */
    executablePath?: string
    /** Whether Chromium's sandbox works here (harness/headlessSandbox.ts); asked once per turn. */
    sandbox?: () => Promise<boolean>
  }
  /**
   * The assistant's `http_request` tool (#827, harness/httpRequest.ts). A
   * session's turns get it when this is set and the `http_request_enabled`
   * setting is not `false`; it is on by default.
   */
  httpRequest?: {
    /** Tests only: a resolver other than the system's. */
    resolve?: Resolver
    /** Tests only: smaller caps. */
    limits?: Partial<HttpLimits>
  }
  run?: QueryRunner
  /** Per-token approval grants (spec §6); nobody but the browser user may approve without one. */
  approvalGrants?: GrantCheck
  /**
   * The tiers a principal holds now, when that can be known without its
   * credential (a bearer token's row; auth/tokens.ts `liveTokenTiers`), or
   * undefined when it cannot. A resumed approval's turn gets no more than
   * these (`resumeApproved`).
   */
  currentTiers?: (owner: Owner) => Promise<readonly Tier[] | undefined>
  /** HMAC key for approval input hashes (approvals/service.ts `approvalHashKey`); per process when omitted. */
  approvalHashKey?: Buffer
  /**
   * The audit log (#258, audit/log.ts): every tool call a turn makes
   * (audit/turn.ts), and every approval decision.
   */
  audit?: AuditLog
  /**
   * How often a parked turn polls its approval, or its question (#940), for a
   * decision or answer given on another replica: one interval for both.
   */
  approvalPollMs?: number
  /** How long a handoff offer lasts (HANDOFF_OFFER_TTL_MS by default). */
  handoffOfferTtlMs?: number
  /** New sessions per owner per window (MAX_NEW_SESSIONS per NEW_SESSION_WINDOW_MS by default). */
  newSessions?: { max: number; windowMs: number }
  leaseMs?: number
  renewMs?: number
  /** How often followers on other replicas poll the event log (EventLog). */
  pollMs?: number
  /**
   * Told of every event-log append once it has committed: in production the
   * `session.*` publisher on the event bus (busEvents.ts, #300).
   */
  onAppend?: AppendHook
  stderr?: (line: string) => void
}

type Row = {
  id: string
  origin: Origin
  owner_kind: Owner['kind']
  owner_id: string
  owner_label: string
  creator_kind: Owner['kind']
  creator_id: string
  offer_kind: Owner['kind'] | null
  offer_id: string | null
  offer_label: string | null
  offer_until: Date | null
  status: SessionStatus
  title: string
  tags: string[]
  scope: Record<string, unknown>
  parent_id: string | null
  max_turns: number
  budget_usd: number
  cost_usd: number
  turns: number
  turn_active: boolean
  created_at: Date
  updated_at: Date
}

/** A pending handoff offer, read as none once it has expired (the columns stay until the next change clears them). */
const LIVE_OFFER = 'pending_owner_until > now()'

const COLUMNS = `id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_kind END AS offer_kind,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_id END AS offer_id,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_label END AS offer_label,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_until END AS offer_until,
  status, title, tags, scope, parent_id, max_turns, budget_usd, cost_usd, turns,
  (turn_id IS NOT NULL AND lease_until > now()) AS turn_active, created_at, updated_at`

function record(row: Row): SessionRecord {
  return {
    id: row.id,
    origin: row.origin,
    owner: { kind: row.owner_kind, id: row.owner_id, label: row.owner_label },
    creator: { kind: row.creator_kind, id: row.creator_id },
    offer:
      row.offer_kind && row.offer_id && row.offer_label && row.offer_until
        ? { to: { kind: row.offer_kind, id: row.offer_id, label: row.offer_label }, until: row.offer_until.toISOString() }
        : null,
    status: row.status,
    title: row.title,
    tags: row.tags,
    scope: row.scope,
    parentId: row.parent_id,
    maxTurns: row.max_turns,
    budgetUsd: row.budget_usd,
    costUsd: row.cost_usd,
    turns: row.turns,
    turnActive: row.turn_active,
    createdAt: row.created_at.toISOString(),
    updatedAt: row.updated_at.toISOString(),
  }
}

export { canSee }

function positive(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback
}

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export const TITLE_MAX = 80

/**
 * Title for a session started with a prompt and no title: the first line, at
 * most TITLE_MAX code points. Counted by code point (`[...line]`), not UTF-16
 * unit, so an emoji at the cut is never split into a lone surrogate.
 */
export function titleFrom(prompt: string): string {
  const points = [...(prompt.trim().split('\n')[0] ?? '')]
  return points.length > TITLE_MAX ? `${points.slice(0, TITLE_MAX - 1).join('')}…` : points.join('')
}

/**
 * The SQL behind `list()`, exported so a test can EXPLAIN it: the owner-or-
 * creator-or-offered filter is served by the ai_sessions_owner,
 * ai_sessions_creator (db/migrations/20260928T0107Z_sessions.sql) and
 * ai_sessions_pending_owner (20260929T1825Z_session_handoff_offers.sql) indexes.
 */
export function listQuery(principal: Owner, filter: ListFilter = {}): { text: string; params: (string | number)[] } {
  const where: string[] = []
  const params: (string | number)[] = []
  if (principal.kind !== 'browser') {
    params.push(principal.kind, principal.id)
    where.push(
      '((owner_kind = $1 AND owner_id = $2) OR (creator_kind = $1 AND creator_id = $2)' +
        ` OR (pending_owner_kind = $1 AND pending_owner_id = $2 AND ${LIVE_OFFER}))`,
    )
  }
  if (filter.status) {
    params.push(filter.status)
    where.push(`status = $${params.length}`)
  }
  if (filter.origin) {
    params.push(filter.origin)
    where.push(`origin = $${params.length}`)
  }
  params.push(Math.min(Math.max(filter.limit ?? 100, 1), 500))
  const text = `SELECT ${COLUMNS} FROM ai_sessions ${where.length ? `WHERE ${where.join(' AND ')}` : ''}
       ORDER BY updated_at DESC LIMIT $${params.length}`
  return { text, params }
}

/** A turn running in this process. */
type LocalTurn = {
  controller: AbortController
  /**
   * Set once the SDK has produced the turn's result (or the stream ended):
   * the turn is finishing on its own, and aborting now would only cut off the
   * SDK's last transcript appends, so interrupt() leaves it alone and says so.
   */
  settling: boolean
  /** The turn's run, final events included, its claim released; it never rejects (startTurn's `done`). */
  done?: Promise<TurnOutcome>
}

/** What a send gets while the service drains for a restart (`drain`). */
export const RESTARTING = 'the agent service is restarting; send again in a moment'

export class SessionManager {
  readonly store: PostgresSessionStore
  readonly events: EventLog
  /** Approvals of outward calls (#258); `approvals.decide` is the decision API. */
  readonly approvals: ApprovalService
  /** Questions the agent asks the user (#940); `questions.answer` is the panel's answer. */
  readonly questions: QuestionService
  private readonly deps: SessionManagerDeps
  private readonly run: QueryRunner
  private readonly leaseMs: number
  private readonly renewMs: number
  /** Turns running in THIS process, by session id. */
  private readonly active = new Map<string, LocalTurn>()
  /** Set by `drain`: a restart is coming, so no new turn starts here. */
  private draining = false
  /** What sessions touched (#931), read by `resources`. */
  private readonly touched: SessionResources

  constructor(deps: SessionManagerDeps) {
    this.deps = deps
    this.store = new PostgresSessionStore(deps.sql)
    this.touched = new SessionResources(deps.sql)
    this.events = new EventLog(deps.sql, {
      ...(deps.pollMs === undefined ? {} : { pollMs: deps.pollMs }),
      ...(deps.onAppend ? { onAppend: deps.onAppend } : {}),
    })
    this.approvals = new ApprovalService({
      sql: deps.sql,
      events: this.events,
      ...(deps.settings ? { settings: deps.settings } : {}),
      ...(deps.approvalGrants ? { grants: deps.approvalGrants } : {}),
      ...(deps.approvalPollMs === undefined ? {} : { pollMs: deps.approvalPollMs }),
      ...(deps.audit ? { audit: deps.audit } : {}),
      ...(deps.approvalHashKey ? { hashKey: deps.approvalHashKey } : {}),
      resume: (approval, by) => this.resumeApproved(approval, by),
    })
    this.questions = new QuestionService({
      sql: deps.sql,
      events: this.events,
      ...(deps.approvalPollMs === undefined ? {} : { pollMs: deps.approvalPollMs }),
      ...(deps.audit ? { audit: deps.audit } : {}),
    })
    this.run = deps.run ?? runHarness
    this.leaseMs = deps.leaseMs ?? DEFAULT_LEASE_MS
    this.renewMs = deps.renewMs ?? DEFAULT_RENEW_MS
  }

  // -- reads -------------------------------------------------------------------

  private async row(id: string): Promise<SessionRecord | undefined> {
    // Anything but a canonical UUID would make Postgres throw on the uuid
    // column; it is simply not a session, so not_found.
    if (!isUuid(id)) return undefined
    const [row] = await this.deps.sql.unsafe<Row[]>(`SELECT ${COLUMNS} FROM ai_sessions WHERE id = $1`, [id])
    return row ? record(row) : undefined
  }

  /** The session if the principal may see it; otherwise not_found (existence is not revealed). */
  async get(id: string, principal: Owner): Promise<SessionRecord> {
    const session = await this.row(id)
    if (!session || !canSee(principal, session)) throw new SessionError('not_found', `no session ${id}`)
    return session
  }

  /** What the session's tool calls touched, oldest first (#931, touched.ts); a session the principal may not see is not found. */
  async resources(id: string, principal: Owner): Promise<TouchedRecord[]> {
    await this.get(id, principal)
    return this.touched.list(id)
  }

  /** Newest first. */
  async list(principal: Owner, filter: ListFilter = {}): Promise<SessionRecord[]> {
    const { text, params } = listQuery(principal, filter)
    const rows = await this.deps.sql.unsafe<Row[]>(text, params)
    return rows.map(record)
  }

  /** The panel's `sessions.snapshot` for a principal (the session picker). */
  async snapshot(principal: Owner): Promise<ServerEvent> {
    const sessions = await this.list(principal)
    return event({
      type: 'sessions.snapshot',
      sessions: sessions.map((s) => ({
        sessionId: s.id,
        title: s.title,
        origin: s.origin,
        owner: s.owner,
        status: s.status,
      })),
    })
  }

  /**
   * Replays the session's events after `afterSeq`, then follows them live
   * until `signal` aborts. Anyone who may see the session may attach
   * (spec §6: "Watchers are unlimited").
   */
  async attach(
    id: string,
    principal: Owner,
    options: { afterSeq?: number; signal?: AbortSignal } = {},
  ): Promise<AsyncGenerator<LoggedEvent>> {
    await this.get(id, principal)
    return this.events.follow(id, options.afterSeq ?? 0, options.signal)
  }

  // -- writes ------------------------------------------------------------------

  private async limits(): Promise<{ maxTurns: number; budgetUsd: number }> {
    const [maxTurns, budgetUsd] = await Promise.all([
      this.deps.settings?.get<number>(SETTING_SESSION_MAX_TURNS),
      this.deps.settings?.get<number>(SETTING_SESSION_BUDGET_USD),
    ])
    return {
      maxTurns: Math.floor(positive(maxTurns, DEFAULT_MAX_TURNS)),
      budgetUsd: positive(budgetUsd, DEFAULT_MAX_BUDGET_USD),
    }
  }

  private async insert(
    id: string,
    principal: Owner,
    fields: { origin: Origin; title: string; tags: string[]; scope: Record<string, unknown>; parentId: string | null },
    options: { rateLimited?: boolean } = {},
  ): Promise<SessionRecord> {
    const { maxTurns, budgetUsd } = await this.limits()
    const insert = async (sql: Sql) => {
      await sql`
        INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,
                                 status, title, tags, scope, parent_id, max_turns, budget_usd)
        VALUES (${id}, ${fields.origin}, ${principal.kind}, ${principal.id}, ${principal.label},
                ${principal.kind}, ${principal.id}, 'idle', ${fields.title}, ${sql.array(fields.tags)},
                ${sql.json(fields.scope as never)}, ${fields.parentId}, ${maxTurns}, ${budgetUsd})`
    }
    if (options.rateLimited) {
      await this.deps.sql.begin(async (tx) => {
        // Per owner, held until commit, so concurrent starts count each other.
        await tx`SELECT pg_advisory_xact_lock(hashtextextended(${`ai_sessions.start:${principal.kind}:${principal.id}`}, 0))`
        await this.withinNewSessionLimit(principal, tx as unknown as Sql)
        await insert(tx as unknown as Sql)
      })
    } else {
      await insert(this.deps.sql)
    }
    const session = await this.row(id)
    if (!session) throw new Error(`session ${id} vanished after insert`)
    return session
  }

  /** Throws `rate_limited` when `principal` has made MAX_NEW_SESSIONS in the window. */
  private async withinNewSessionLimit(principal: Owner, sql: Sql): Promise<void> {
    const { max, windowMs } = this.deps.newSessions ?? { max: MAX_NEW_SESSIONS, windowMs: NEW_SESSION_WINDOW_MS }
    const [recent] = await sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM ai_sessions
      WHERE owner_kind = ${principal.kind} AND owner_id = ${principal.id}
        AND created_at > now() - (${windowMs} * interval '1 millisecond')`
    if ((recent?.n ?? 0) >= max) {
      throw new SessionError(
        'rate_limited',
        `too many new sessions: at most ${max} per ${Math.round(windowMs / 1000)} s; wait and try again`,
      )
    }
  }

  async start(principal: Owner, options: StartOptions): Promise<{ session: SessionRecord; turn?: Turn }> {
    const prompt = options.prompt?.trim()
    const id = randomUUID()
    const title = options.title?.trim() || (prompt ? titleFrom(prompt) : '')
    const session = await this.insert(id, principal, {
      origin: options.origin,
      title,
      tags: options.tags ?? [],
      scope: options.scope ?? {},
      parentId: null,
    }, { rateLimited: true })
    await this.events.append(id, [
      event({
        type: 'session.started',
        sessionId: id,
        origin: session.origin,
        owner: session.owner,
        title,
        budgetUsd: session.budgetUsd,
      }),
      event({ type: 'session.status', sessionId: id, status: 'idle' }),
    ])
    if (!prompt) return { session }
    const turn = await this.send(id, principal, prompt, {
      ...(options.context ? { context: options.context } : {}),
      ...(options.tiers ? { tiers: options.tiers } : {}),
    })
    return { session: await this.get(id, principal), turn }
  }

  /**
   * Adds a user turn and starts it. Resolves once the turn has been claimed
   * and started; `done` settles when it ends. Only the owner may send.
   */
  async send(id: string, principal: Owner, text: string, options: SendOptions = {}): Promise<Turn> {
    const prompt = text.trim()
    if (!prompt) throw new SessionError('invalid', 'the message is empty')
    if (this.draining) throw new SessionError('busy', RESTARTING)
    const before = await this.get(id, principal)
    const turnId = randomUUID()
    const [claimed] = await this.deps.sql.unsafe<Row[]>(
      `UPDATE ai_sessions
       SET status = 'running', turn_id = $2, lease_until = now() + ($5 * interval '1 millisecond'),
           interrupt_requested = false, updated_at = now()
       WHERE id = $1 AND owner_kind = $3 AND owner_id = $4 AND status <> 'done' AND cost_usd < budget_usd
         AND (turn_id IS NULL OR lease_until <= now())
       RETURNING ${COLUMNS}`,
      [id, turnId, principal.kind, principal.id, this.leaseMs],
    )
    if (!claimed) throw await this.whyNotClaimed(id, principal, before)
    return this.startTurn(record(claimed), turnId, prompt, principal, {
      ...(options.context ? { context: options.context } : {}),
      ...(options.tiers ? { tiers: options.tiers } : {}),
    })
  }

  /**
   * Runs the turn that re-makes an approved call after its turn was lost
   * (#258: the approval was decided after a restart). Claims the session
   * like send() does, but for the decider rather than the owner: approving is
   * what authorises it (approvals/service.ts `decide`). The approval is bound
   * to the new turn, which alone may use it. Not resumed when the session
   * cannot start a turn now, or the approval can no longer be used; the
   * service then voids it and says so. A claim taken and then not used is
   * released at once.
   */
  async resumeApproved(approval: ApprovalRecord, by: Owner): Promise<ResumeResult> {
    if (!approval.sessionId) return { resumed: false, reason: 'the approval has no session' }
    if (this.draining) return { resumed: false, reason: RESTARTING }
    const turnId = randomUUID()
    const [claimed] = await this.deps.sql.unsafe<Row[]>(
      `UPDATE ai_sessions
       SET status = 'running', turn_id = $2, lease_until = now() + ($3 * interval '1 millisecond'),
           interrupt_requested = false, updated_at = now()
       WHERE id = $1 AND status <> 'done' AND cost_usd < budget_usd
         AND (turn_id IS NULL OR lease_until <= now())
       RETURNING ${COLUMNS}`,
      [approval.sessionId, turnId, this.leaseMs],
    )
    if (!claimed) {
      return { resumed: false, reason: 'it is running another turn, is done, or has spent its budget' }
    }
    try {
      if (!(await this.approvals.bindResume(approval.id, turnId))) {
        await this.releaseClaim(approval.sessionId, turnId)
        return { resumed: false, reason: 'the approval was already withdrawn, used or out of time' }
      }
      const prompt =
        `${publicLabel(by)} approved ${approval.tool} (approval ${approval.id}) after the turn that asked for it had ` +
        'ended. Make that same call again now, with exactly the same input: the approval is bound to that input ' +
        'and is used once. If you no longer need it, say so instead.'
      const tiers = await this.resumeTiers(approval, record(claimed))
      await this.startTurn(record(claimed), turnId, prompt, by, { keepResumeTurn: turnId, ...(tiers ? { tiers } : {}) })
      return { resumed: true }
    } catch (err) {
      await this.releaseClaim(approval.sessionId, turnId)
      return { resumed: false, reason: describe(err) }
    }
  }

  /**
   * The tiers a resumed turn runs with: those of the turn that asked
   * (`requestedTiers`), so the approved tool is offered again, but only while
   * that principal still owns the session, and cut down to what it holds now
   * (`currentTiers`: a revoked token holds nothing). Undefined leaves the
   * owner's default (auth/principal.ts `harnessPrincipal`).
   */
  private async resumeTiers(approval: ApprovalRecord, session: SessionRecord): Promise<readonly Tier[] | undefined> {
    const requested = approval.requestedTiers
    if (!requested || !sameOwner(approval.requestedBy, session.owner)) return undefined
    const now = this.deps.currentTiers ? await this.deps.currentTiers(session.owner) : undefined
    const tiers = now ? requested.filter((t) => now.includes(t)) : requested
    return tiers.length > 0 ? tiers : undefined
  }

  /** Gives back a claim no turn ran on (a resume that could not start). */
  private async releaseClaim(id: string, turnId: string): Promise<void> {
    const released = await this.deps.sql`
      UPDATE ai_sessions SET status = 'idle', turn_id = NULL, lease_until = NULL, updated_at = now()
      WHERE id = ${id} AND turn_id = ${turnId}`
    if (released.count > 0) await this.events.append(id, [event({ type: 'session.status', sessionId: id, status: 'idle' })])
  }

  private async startTurn(
    session: SessionRecord,
    turnId: string,
    prompt: string,
    author: Owner,
    options: { keepResumeTurn?: string; context?: string; tiers?: readonly Tier[] } = {},
  ): Promise<Turn> {
    const id = session.id
    // A new turn supersedes approvals left pending, or approved and unused,
    // by one that is gone (claiming proved no turn is live): their calls can
    // no longer run. A resumed turn keeps the approval bound to it; its
    // sibling orphans are cancelled with the rest (approvals/service.ts).
    await this.approvals.cancelPending(
      id,
      'superseded by a new turn',
      options.keepResumeTurn === undefined ? {} : { keepResumeTurn: options.keepResumeTurn },
    )
    await this.events.append(id, [
      event({ type: 'user.turn', sessionId: id, turnId, text: prompt, author }),
      event({ type: 'session.status', sessionId: id, status: 'running' }),
    ])
    const controller = new AbortController()
    const local: LocalTurn = { controller, settling: false }
    this.active.set(id, local)
    const query = options.context ? `${prompt}\n\n${options.context}` : prompt
    const done = this.runTurn(session, turnId, query, local, prompt, options.tiers ? { tiers: options.tiers } : {})
      // Never rejects: callers may ignore `done`, and an unhandled rejection
      // would take the process down. The lease frees the claim if the
      // release itself failed.
      .catch((err: unknown): TurnOutcome => ({ kind: 'failed', message: describe(err) }))
      .finally(() => {
        if (this.active.get(id) === local) this.active.delete(id)
      })
    local.done = done
    return { turnId, done }
  }

  private async whyNotClaimed(id: string, principal: Owner, before: SessionRecord): Promise<SessionError> {
    const now = (await this.row(id)) ?? before
    if (!sameOwner(principal, now.owner)) {
      return new SessionError(
        'forbidden',
        `session ${id} is controlled by ${ownerSeenBy(principal, now.owner).label}; only its owner can send, so ` +
          'take it over with a handoff first',
      )
    }
    if (now.status === 'done') return new SessionError('closed', `session ${id} is done`)
    if (now.costUsd >= now.budgetUsd) {
      return new SessionError(
        'budget_exhausted',
        `session ${id} has spent its budget (${usd(now.costUsd)} of ${usd(now.budgetUsd)}); ` +
          'continue in a new chat, raise its budget, or start a new one',
        { costUsd: now.costUsd, budgetUsd: now.budgetUsd },
      )
    }
    return new SessionError(
      'busy',
      `a turn is already running in session ${id}; wait for it to finish or interrupt it`,
    )
  }

  private async runTurn(
    session: SessionRecord,
    turnId: string,
    prompt: string,
    local: LocalTurn,
    /** The user's words without the page context: what memory is recalled against. */
    userText: string,
    principal: TurnPrincipal,
  ): Promise<TurnOutcome> {
    const { controller } = local
    const id = session.id
    const sql = this.deps.sql
    const registryTierOf = this.deps.tierOf ?? (() => undefined)
    // The http tools' tier depends on the method in their input (#827).
    const tierOf: TierResolver = (name, input) => httpTierOf(name, input) ?? registryTierOf(name, input)
    // Widened with the plugins' tiers once they are loaded below, so the
    // panel shows a plugin tool at the tier the permission seam applies. The
    // headless browser's tools are tiered too (spec §5.3, "Tiers").
    let eventTierOf: TierResolver = (name, input) => browserTierOf(name) ?? tierOf(name, input)
    // AskUserQuestion and ask_user (#940) only ask the user: shown and audited as `read`, as the harness tiers them.
    const asksUser = session.owner.kind === 'browser'
    const shownTierOf: TierResolver = (name, input) =>
      asksUser && isQuestionTool(name) ? 'read' : eventTierOf(name, input)
    const mapper = new SdkEventMapper(id, shownTierOf)
    let lost = false
    /** Redacted from everything this turn writes to the durable event log. */
    let secrets: string[] = []
    // One audit row per tool call of this turn (#258, audit/turn.ts).
    const auditor = this.deps.audit
      ? new TurnAuditor(this.deps.audit, {
          sessionId: id,
          turnId,
          actor: session.owner,
          tierOf: shownTierOf,
          secrets: () => secrets,
        })
      : undefined

    // Lease renewal, and the interrupt flag from other replicas.
    let renewing: Promise<unknown> = Promise.resolve()
    const renew = setInterval(() => {
      renewing = sql<{ interrupt_requested: boolean }[]>`
        UPDATE ai_sessions SET lease_until = now() + (${this.leaseMs} * interval '1 millisecond')
        WHERE id = ${id} AND turn_id = ${turnId}
        RETURNING interrupt_requested`
        .then((rows) => {
          if (rows.length === 0) {
            lost = true
            controller.abort(new Error('lost the turn claim'))
          } else if (rows[0]?.interrupt_requested) {
            controller.abort(new Error('the turn was interrupted'))
          }
        })
        .catch(() => {
          // A database blip: keep running; the lease covers several misses.
        })
    }, this.renewMs)

    let result: SDKResultMessage | undefined
    let failure: string | undefined
    let forwarded: PluginsForRun | undefined
    let packages: PackagesForRun | undefined
    let pluginCheck: ((message: SDKMessage) => Promise<void>) | undefined
    /** Whether this turn wrote headless-browser folders, removed when it ends. */
    let browserDirs = false
    try {
      // The credentials first, and into `secrets` at once: whatever fails
      // after this point is redacted before it reaches the event log. Every
      // candidate's, since the turn may fall back to any of them.
      const candidates = await this.deps.credentials.candidates()
      secrets = candidates.map((c) => c.credential.secret)
      forwarded = this.deps.remotePlugins ? await this.deps.remotePlugins() : undefined
      const remotePlugins = forwarded?.plugins ?? []
      // Plugin header values (and their bare tokens) are redacted from the
      // event log like the credential. Claude Code never holds them (the
      // forwarder adds them), but a plugin could echo one in a tool result.
      secrets.push(...(forwarded?.secrets ?? []))
      const memory = forwarded?.hindsight ? this.memoryHooks(forwarded.hindsight, secrets, userText, { session, turnId }) : undefined
      const ownPlugin = this.deps.ownPlugin
      const pluginTiers = harnessTierOf({ remotePlugins, tierOf, ...(ownPlugin !== undefined ? { ownPlugin } : {}) })
      eventTierOf = (name, input) => browserTierOf(name) ?? pluginTiers(name, input)
      // A plugin left out of this turn is said so in the session, not only in the log.
      const unavailable = (message: string) =>
        this.events.append(id, [
          scrubForLog(event({ type: 'error', sessionId: id, code: 'plugin_unavailable', message }), secrets),
        ])
      for (const problem of forwarded?.problems ?? []) {
        this.deps.stderr?.(`${problem}\n`)
        await unavailable(problem)
      }
      packages = this.deps.packagePlugins ? await this.deps.packagePlugins() : undefined
      for (const problem of packages?.problems ?? []) {
        this.deps.stderr?.(`${problem}\n`)
        await unavailable(problem)
      }
      const pluginPaths = [...(this.deps.pluginPaths ?? []), ...(packages?.paths ?? [])]
      pluginCheck = async (message: SDKMessage) => {
        if (message.type !== 'system' || message.subtype !== 'init') return
        // The SDK skips a plugin it cannot load; the init message lists what it did load
        // (https://code.claude.com/docs/en/agent-sdk/plugins, "Verifying plugin installation").
        const listed = (message as { plugins?: { path: string }[] }).plugins ?? []
        const loaded = new Set(listed.map((p) => path.resolve(p.path)))
        if (ownPlugin !== undefined && !loaded.has(path.resolve(ownPlugin))) {
          await unavailable("ScadBuddy's own plugin was not loaded by Claude Code: its skills and subagents are unavailable")
        }
        for (const dir of packages?.paths ?? []) {
          if (!loaded.has(path.resolve(dir))) {
            await unavailable(`plugin package ${path.basename(path.dirname(dir))} was not loaded by Claude Code`)
          }
        }
        for (const plugin of remotePlugins) {
          const status = message.mcp_servers.find((s) => s.name === plugin.name)?.status
          if (status !== 'connected') {
            await unavailable(
              `plugin ${plugin.name} is not available in this turn: its MCP server is ${status ?? 'missing'}`,
            )
          }
        }
      }
      const [cwd, resume, model, browserSetting, httpSetting] = await Promise.all([
        ensureSessionDir(this.deps.paths, id),
        this.store.exists(id),
        this.deps.settings?.get<string>(SETTING_MODEL),
        this.deps.headlessBrowser ? this.deps.settings?.get<unknown>(SETTING_HEADLESS_BROWSER) : undefined,
        this.deps.httpRequest ? this.deps.settings?.get<unknown>(SETTING_HTTP_REQUEST) : undefined,
      ])
      // The http_request tool (#827): on unless the setting is `false`. Its
      // saved bodies live in the session's own directory, and it never sees
      // the turn's secrets except to refuse a request that carries one.
      const http =
        this.deps.httpRequest && httpRequestEnabled(httpSetting)
          ? httpRequestServer({
              saveDir: path.join(cwd, 'http'),
              secrets: () => secrets,
              audit: this.deps.audit,
              actor: session.owner,
              sessionId: id,
              turnId,
              signal: controller.signal,
              ...(this.deps.httpRequest.resolve ? { resolve: this.deps.httpRequest.resolve } : {}),
              ...(this.deps.httpRequest.limits ? { limits: this.deps.httpRequest.limits } : {}),
            })
          : undefined
      const hb = this.deps.headlessBrowser
      const gate = this.approvals.gate({
        sessionId: id,
        turnId,
        requestedBy: session.owner,
        ...(principal.tiers ? { requestedTiers: principal.tiers } : {}),
        secrets: () => secrets,
        signal: controller.signal,
      })
      const sandbox =
        this.deps.headlessBrowser?.sandbox && browserSetting === true
          ? await this.deps.headlessBrowser.sandbox()
          : false
      const browser =
        hb && browserSetting === true
          ? {
              ...(sandbox ? { sandbox: true } : {}),
              sessionId: id,
              backendUrl: hb.backendUrl,
              ...(hb.publicUrl ? { publicUrl: hb.publicUrl } : {}),
              ...(hb.uiOrigins ? { uiOrigins: hb.uiOrigins } : {}),
              ...(hb.browserAllowedOrigins
                ? {
                    browserAllowedOrigins: hb.browserAllowedOrigins,
                    // Approved once per origin per session, in Postgres, so a
                    // later turn (on any replica) does not ask again.
                    approvedOrigins: await loadApprovedOrigins(sql, id),
                    rememberOrigin: (origin: string, approvalId: string | undefined) =>
                      rememberApprovedOrigin(sql, id, origin, approvalId),
                  }
                : {}),
              dir: sessionBrowserDir(this.deps.paths, id),
              tmpDir: sessionBrowserTmpDir(id),
              ...(hb.executablePath ? { executablePath: hb.executablePath } : {}),
            }
          : undefined
      browserDirs = browser !== undefined
      const run: Omit<HarnessRun, 'credential'> = {
        paths: this.deps.paths,
        prompt,
        cwd,
        sessionStore: this.store,
        includePartialMessages: true,
        maxTurns: session.maxTurns,
        maxBudgetUsd: Math.max(session.budgetUsd - session.costUsd, 0.000001),
        signal: controller.signal,
        tierOf,
        approvalGate: auditor ? auditor.gate(gate, (toolUseId) => this.approvals.idForToolUse(id, toolUseId)) : gate,
        // AskUserQuestion (#940): only the user in the panel answers, so only
        // a session the browser user owns is given the tool. Any other
        // owner's turn would wait on someone who is not asked.
        ...(asksUser
          ? { questionGate: this.questions.gate({ sessionId: id, turnId, secrets: () => secrets, signal: controller.signal }) }
          : {}),
        // The data/instruction boundary (#258, safety/untrusted.ts): only the
        // user's messages are instructions; tool results are data. Then which
        // browser each browser_* tool drives.
        systemPromptAppend: `${UNTRUSTED_CONTENT_POLICY}\n\n${browserToolsGuide(browser !== undefined)}`,
        ...(browser ? { headlessBrowser: browser } : {}),
        // First turn: the SDK session gets OUR id; later turns resume it.
        ...(resume ? { resume: id } : { sessionId: id }),
        ...(typeof model === 'string' && model ? { model } : {}),
        ...(this.deps.mcpServers || browser || http
          ? {
              mcpServers: {
                ...(this.deps.mcpServers ? this.deps.mcpServers(session, principal) : {}),
                ...(http ? { [HTTP_SERVER]: http } : {}),
                // The one way past the backend's agent-actor gate: a human
                // approves one exact outward request (harness/headlessGrants.ts).
                ...(browser
                  ? {
                      [GRANT_SERVER]: headlessGrantServer({
                        sql,
                        sessionId: id,
                        turnId,
                        hash: (tool, input) => this.approvals.hash(tool, input),
                      }),
                    }
                  : {}),
              },
            }
          : {}),
        ...(pluginPaths.length ? { pluginPaths } : {}),
        ...(ownPlugin !== undefined ? { ownPlugin } : {}),
        ...(remotePlugins.length ? { remotePlugins } : {}),
        ...(memory ? { memoryHooks: memory.hooks } : {}),
        ...(this.deps.stderr ? { stderr: this.deps.stderr } : {}),
      }
      const turn = runWithFallback(run, {
        candidates,
        report: this.deps.credentials.reporter({ sessionId: id, turnId }),
        run: this.run,
        ...(this.deps.probe ? { probe: this.deps.probe } : {}),
        // A resumed query's total includes what the session spent before (fallback.ts `Spend`).
        ...(resume ? { priorCostUsd: session.costUsd } : {}),
      })
      for await (const message of turn) {
        if (message.type === 'result') {
          result = message
          local.settling = true
        }
        await pluginCheck?.(message)
        const events = mapper.map(message)
        if (events.length) await this.events.append(id, events.map((e) => scrubForLog(e, secrets)))
        if (auditor) for (const e of events) await auditor.observe(e)
      }
    } catch (err) {
      // For an error result the SDK yields the result and then throws
      // ("Claude Code returned an error result", test/run.test.ts); the
      // result is what counts then.
      if (!result && !controller.signal.aborted) failure = redact(describe(err), secrets)
    } finally {
      forwarded?.release()
      packages?.release()
      local.settling = true
      clearInterval(renew)
      await renewing
      await auditor?.finish(
        controller.signal.aborted ? abortMessage(controller.signal) : (failure ?? 'the turn ended first'),
      )
      // The query has ended, and with it the playwright server and Chromium:
      // its screenshots and profile go now, not when the volume fills.
      if (browserDirs) {
        await removeSessionBrowserDirs(this.deps.paths, id).catch((err: unknown) =>
          this.deps.stderr?.(`cannot remove the headless-browser folders of session ${id}: ${String(err)}\n`),
        )
      }
    }
    // The loop has ended only after the SDK's last transcript append (measured:
    // `last-prompt` and `cost-state` entries arrive after the `result`
    // message), so releasing the claim here means the next turn, on any
    // replica, resumes from a complete transcript.
    if (lost) return { kind: 'lost_claim' }
    const stopped = controller.signal.aborted ? abortMessage(controller.signal) : undefined
    return this.finish(session, turnId, stopped, result, failure, secrets)
  }

  /**
   * The turn's memory hooks, for the `hindsight` plugin this turn loaded (so
   * only an enabled plugin whose endpoint passed its egress check this turn).
   * Recall is against the user's words (`recallQuery`), not the page context
   * appended after them, unless a fixed query is configured. Undefined when
   * memory is off or the plugin's URL is not a bank endpoint (logged).
   *
   * Each recall and retain becomes an audit row and a `memory` panel event
   * (#818; hooks make no SDK message, so TurnAuditor never sees them). A
   * retain reports after the turn ended; both writes work then too, since
   * they need only the session row. A failure is logged by the hooks.
   */
  private memoryHooks(
    plugin: CheckedPlugin,
    secrets: readonly string[],
    userText: string,
    turn: { session: SessionRecord; turnId: string },
  ): MemoryHooks | undefined {
    const config = this.deps.memory
    if (config === false) return undefined
    const log = (line: string) => this.deps.stderr?.(line)
    let client: HindsightClient
    try {
      client = new HindsightClient(hindsightTarget(plugin))
    } catch (err) {
      log(`hindsight: automatic memory is off for this turn: ${describe(err)}\n`)
      return undefined
    }
    const fixed = config?.hookConfig?.recallQuery
    return createMemoryHooks({
      ...config,
      hookConfig: { ...config?.hookConfig, recallQuery: fixed && fixed !== '$prompt' ? fixed : userText },
      client,
      secrets,
      log,
      sessionStore: this.store,
      onActivity: (activity) => this.recordMemory(turn.session, turn.turnId, activity, secrets),
    })
  }

  /** One recall or retain: its audit row (never fails, audit/log.ts) and its panel event. */
  private async recordMemory(
    session: SessionRecord,
    turnId: string,
    a: MemoryActivity,
    secrets: readonly string[],
  ): Promise<void> {
    const id = session.id
    const summary = {
      bank: a.bank,
      ...(a.documentId ? { document_id: a.documentId } : {}),
      ...(a.count === undefined ? {} : { results: a.count }),
      ...(a.tool ? { tool: a.tool } : {}),
    }
    const detail = a.reason ? { detail: safeDetail(a.reason, secrets) } : {}
    await Promise.all([
      this.deps.audit?.record({
        kind: 'memory',
        action: a.action,
        surface: 'harness',
        actor: session.owner,
        sessionId: id,
        turnId,
        ...(a.toolUseId ? { toolUseId: a.toolUseId } : {}),
        inputSummary: JSON.stringify(summary),
        outcome: a.outcome === 'ok' ? 'ok' : 'error',
        ...detail,
        startedAt: a.startedAt,
        finishedAt: a.finishedAt,
      }),
      this.events.append(id, [
        scrubForLog(
          event({
            type: 'memory',
            sessionId: id,
            turnId,
            action: a.action,
            bank: a.bank,
            outcome: a.outcome,
            ...(a.count === undefined ? {} : { count: a.count }),
            ...detail,
            ...(a.input === undefined ? {} : { input: memoryText(a.input, secrets) }),
            ...(a.memories === undefined ? {} : { memories: a.memories.map((m) => memoryText(m, secrets)) }),
          }),
          secrets,
        ),
      ]),
    ])
  }

  /**
   * `stopped` is why the turn was aborted (the abort reason: an interrupt, or
   * SHUTTING_DOWN), when it was.
   */
  private async finish(
    session: SessionRecord,
    turnId: string,
    stopped: string | undefined,
    result: SDKResultMessage | undefined,
    failure: string | undefined,
    secrets: readonly string[],
  ): Promise<TurnOutcome> {
    const id = session.id
    let status: SessionStatus
    let outcome: TurnOutcome
    const tail: ServerEvent[] = []
    let costUsd = session.costUsd
    let turns = session.turns
    // An approval still pending here belongs to a call that was waiting when
    // the turn was aborted: only an abort ends a wait (approvals/service.ts).
    // A shutdown keeps it, and the session waiting on it, for after the
    // restart; anything else (an interrupt) cancels it. Measured on SDK
    // 0.3.283 (test/approvals.e2e.test.ts): aborting a query whose canUseTool
    // is pending fails that call ("Tool permission request failed: AbortError:
    // Tool permission stream closed before response received"), and Claude
    // Code may still reach the model and end with a `result` before it exits,
    // so either outcome below can follow. The tool never runs.
    // Approved-but-unused approvals end with the turn in every case,
    // including the one a resumed turn was bound to and did not use.
    // A question never outlives its turn (questions/service.ts), shutdown or not.
    // Only this turn's: if its claim was lost, a newer turn's question is not ours to cancel.
    await this.questions.cancelPending(id, stopped ?? 'the turn ended', { refresh: false, turnId })
    const keepWaiting = stopped === SHUTTING_DOWN && (await this.approvals.hasPending(id))
    if (stopped !== SHUTTING_DOWN) {
      await this.approvals.cancelPending(id, stopped ?? 'the turn ended', { refresh: false })
    } else {
      await this.approvals.revokeUnused(id, 'the turn ended')
    }
    if (result) {
      // `total_cost_usd` of a RESUMED query already includes the earlier
      // turns: measured on SDK 0.3.283 (0.000105 after turn 1, 0.00021 after
      // turn 2 of the same session; test/sessions.e2e.test.ts asserts it). The
      // SDK restores it from the transcript's `cost-state` entry, so if that
      // restore ever fails the total comes back smaller than what is recorded,
      // and it is added instead.
      const total = result.total_cost_usd
      costUsd = total >= session.costUsd ? total : session.costUsd + total
      turns = session.turns + result.num_turns
      status = result.subtype === 'error_during_execution' ? 'failed' : 'idle'
      // Its budget is filled in from the row once the claim is released below.
      tail.push(event({ type: 'session.result', sessionId: id, costUsd, turns }))
      if (result.subtype === 'error_max_budget_usd') {
        // The SDK's own text names this turn's share of the budget as an
        // unrounded float; the session's budget, in cents, is what the user
        // set. Worded below, once the row says what the budget is now.
        tail.push(event({ type: 'error', sessionId: id, code: result.subtype, message: 'the chat used its budget' }))
      } else if (result.subtype !== 'success') {
        const detail = 'errors' in result && result.errors.length ? `: ${result.errors.join('; ')}` : ''
        tail.push(event({ type: 'error', sessionId: id, code: result.subtype, message: `the turn stopped (${result.subtype})${detail}` }))
      }
      outcome = { kind: 'result', subtype: result.subtype, costUsd, turns }
    } else if (stopped !== undefined) {
      status = 'idle'
      tail.push(event({ type: 'error', sessionId: id, code: 'interrupted', message: 'the turn was interrupted' }))
      outcome = { kind: 'interrupted' }
    } else {
      status = 'failed'
      const message = failure ?? 'the turn ended without a result'
      tail.push(event({ type: 'error', sessionId: id, code: 'turn_failed', message }))
      outcome = { kind: 'failed', message }
    }
    if (keepWaiting) status = 'waiting_approval'
    tail.push(event({ type: 'session.status', sessionId: id, status }))

    const [released] = await this.deps.sql<{ budget_usd: number }[]>`
      UPDATE ai_sessions
      SET status = ${status}, cost_usd = ${costUsd}, turns = ${turns},
          turn_id = NULL, lease_until = NULL, interrupt_requested = false, updated_at = now()
      WHERE id = ${id} AND turn_id = ${turnId}
      RETURNING budget_usd`
    if (!released) return { kind: 'lost_claim' }
    // Read from the row, not `session`: the user may have raised it while the turn ran.
    const budgetUsd = released.budget_usd
    const worded = tail.map((e): ServerEvent => {
      if (e.type === 'session.result') return { ...e, budgetUsd }
      if (e.type === 'error' && e.code === 'error_max_budget_usd') {
        return { ...e, message: `this chat used its ${usd(budgetUsd)} budget (${usd(costUsd)} spent)` }
      }
      return e
    })
    await this.events.append(id, worded.map((e) => scrubForLog(e, secrets)))
    // A decision that landed while this turn was finishing saw it still
    // holding the session and took it for parked (approvals/service.ts
    // decide), so nobody resumes for it: void an approval of this turn's
    // that nothing used, and settle the status if nothing is pending now. A
    // decision that lands after the release is an orphan's: decide() resumes
    // for it, and a row already bound to that resumed turn is skipped here
    // (revokeUnused `turnId`). Whichever of the two gets the row first wins;
    // a void is announced, so an approval is never lost silently.
    await this.approvals.revokeUnused(id, 'it was decided as its turn ended', { turnId })
    await this.approvals.refreshStatus(id)
    return outcome
  }

  /**
   * Stops the running turn. Any principal that may see the session may
   * interrupt it (spec §8.6: "interrupt from any watcher"). Resolves false
   * when no turn is running.
   */
  async interrupt(id: string, principal: Owner): Promise<boolean> {
    const session = await this.get(id, principal)
    const local = this.active.get(id)
    if (local) {
      // Accurate, not optimistic: a turn whose result is already in is
      // finishing by itself, so this interrupt stops nothing.
      if (local.settling) return false
      // Its pending approvals and questions are cancelled as it finishes (finish()).
      local.controller.abort(new Error(`interrupted by ${publicLabel(principal)}`))
      return true
    }
    if (!session.turnActive) {
      // No turn: all an interrupt can stop is an approval left pending by a
      // turn that is gone (a restart), which would otherwise resume it.
      return (await this.approvals.cancelPending(id, `interrupted by ${publicLabel(principal)}`)) > 0
    }
    // Running on another replica: its lease renewal sees the flag.
    const rows = await this.deps.sql`
      UPDATE ai_sessions SET interrupt_requested = true
      WHERE id = ${id} AND turn_id IS NOT NULL AND lease_until > now()`
    return rows.count > 0
  }

  /**
   * Moves ownership explicitly (spec §6 "Handoff"). What `to` asks for:
   *
   *   - the actor itself: the browser user takes over any session (the panel's
   *     `session.handoff`, #256); the owner already has it; the target of a
   *     live offer ACCEPTS it. Nobody else can take a session.
   *   - the browser user: the owner hands it to the human at once, who may
   *     take any session over anyway and sees every one.
   *   - any other principal: the owner OFFERS it (PR #715 review). Nothing
   *     moves until that principal accepts, the way `confirm_action` completes
   *     only for the principal that prepared the call (approvals/mcp.ts): so
   *     no principal is made the owner, and the sole sender, of a session it
   *     never asked for. One offer at a time (a new one replaces it); it lasts
   *     `handoffOfferTtlMs`, and is withdrawn or declined with `cancelHandoff`.
   *
   * Any change of owner clears the offer, and cancels the session's pending
   * approvals: an approval asked for the previous owner's turn is not handed
   * on (approvals/service.ts).
   */
  async handoff(id: string, actor: Owner, to: Owner): Promise<SessionRecord> {
    const session = await this.get(id, actor)
    const isOwner = sameOwner(actor, session.owner)
    if (sameOwner(actor, to)) {
      if (actor.kind === 'browser') return this.transfer(session, to)
      if (isOwner) return session
      if (session.offer && sameOwner(actor, session.offer.to)) return this.transfer(session, to, { accepting: true })
      throw new SessionError(
        'forbidden',
        `session ${id} is controlled by ${ownerSeenBy(actor, session.owner).label} and was not offered to you; ` +
          'only its owner can hand it to you',
      )
    }
    if (!isOwner) {
      throw new SessionError(
        'forbidden',
        `session ${id} is controlled by ${ownerSeenBy(actor, session.owner).label}; only its owner can hand it off`,
      )
    }
    if (to.kind === 'browser') return this.transfer(session, to)
    // Whether `to` is a live token is not checked: an answer that differed would
    // tell any write-tier caller which token ids are live (PR #715 review). An
    // offer nobody can accept lapses at its `until`, and the owner can withdraw it.
    return this.offer(session, to)
  }

  /**
   * Accepts the live offer of a session to `actor` (the same as
   * `handoff(id, actor, actor)` for it). Refused when nothing is offered to it.
   */
  async acceptHandoff(id: string, actor: Owner): Promise<SessionRecord> {
    const session = await this.get(id, actor)
    if (!session.offer || !sameOwner(actor, session.offer.to)) {
      throw new SessionError('invalid', `session ${id} is not offered to you`)
    }
    return this.transfer(session, actor, { accepting: true })
  }

  /**
   * Ends the live offer of a session: its owner withdraws it, its target
   * declines it. Resolves false when there was none.
   */
  async cancelHandoff(id: string, actor: Owner): Promise<boolean> {
    const session = await this.get(id, actor)
    if (!session.offer) return false
    const target = sameOwner(actor, session.offer.to)
    if (!target && !sameOwner(actor, session.owner)) {
      throw new SessionError('forbidden', `only the owner of session ${id} or the one it is offered to can cancel the offer`)
    }
    const { to } = session.offer
    const rows = await this.deps.sql`
      UPDATE ai_sessions
      SET pending_owner_kind = NULL, pending_owner_id = NULL, pending_owner_label = NULL,
          pending_owner_until = NULL, updated_at = now()
      WHERE id = ${id} AND pending_owner_kind = ${to.kind} AND pending_owner_id = ${to.id} AND pending_owner_until > now()`
    if (rows.count === 0) return false
    await this.announceOwner(id)
    return true
  }

  private async offer(session: SessionRecord, to: Owner): Promise<SessionRecord> {
    const ttlMs = this.deps.handoffOfferTtlMs ?? HANDOFF_OFFER_TTL_MS
    // Conditional on the owner read above, as a transfer is.
    const rows = await this.deps.sql`
      UPDATE ai_sessions
      SET pending_owner_kind = ${to.kind}, pending_owner_id = ${to.id}, pending_owner_label = ${to.label},
          pending_owner_until = now() + (${ttlMs} * interval '1 millisecond'), updated_at = now()
      WHERE id = ${session.id} AND owner_kind = ${session.owner.kind} AND owner_id = ${session.owner.id}`
    if (rows.count === 0) throw new SessionError('busy', `session ${session.id} changed owner meanwhile; try again`)
    await this.announceOwner(session.id)
    return this.get(session.id, session.owner)
  }

  private async transfer(session: SessionRecord, to: Owner, options: { accepting?: boolean } = {}): Promise<SessionRecord> {
    const { id } = session
    if (sameOwner(session.owner, to) && session.owner.label === to.label && !session.offer) return session
    // Conditional on the owner read above (and, accepting, on the offer still
    // being the live one to `to`), so two concurrent handoffs cannot both apply.
    const offerStill = options.accepting
      ? this.deps.sql`AND pending_owner_kind = ${to.kind} AND pending_owner_id = ${to.id} AND pending_owner_until > now()`
      : this.deps.sql``
    const rows = await this.deps.sql`
      UPDATE ai_sessions
      SET owner_kind = ${to.kind}, owner_id = ${to.id}, owner_label = ${to.label},
          pending_owner_kind = NULL, pending_owner_id = NULL, pending_owner_label = NULL,
          pending_owner_until = NULL, updated_at = now()
      WHERE id = ${id} AND owner_kind = ${session.owner.kind} AND owner_id = ${session.owner.id} ${offerStill}`
    if (rows.count === 0) {
      throw new SessionError(
        options.accepting ? 'invalid' : 'busy',
        options.accepting
          ? `session ${id} is no longer offered to you`
          : `session ${id} changed owner meanwhile; try again`,
      )
    }
    if (sameOwner(session.owner, to)) {
      // Only the offer went (the browser user took back its own session).
      await this.announceOwner(id)
      return this.get(id, to)
    }
    await this.events.append(id, [event({ type: 'session.owner', sessionId: id, owner: to })])
    await this.approvals.cancelPending(id, `the session was handed off to ${publicLabel(to)}`)
    await this.questions.cancelPending(id, `the session was handed off to ${publicLabel(to)}`)
    return this.get(id, to)
  }

  /**
   * Announces a change to a session's offer as `session.owner` on the bus
   * (busEvents.ts), so a watcher of the session or of the list re-reads it. An
   * offer is state on the session row, not a transcript event (the panel's
   * protocol has none for it), so nothing is appended.
   */
  private async announceOwner(id: string): Promise<void> {
    const session = await this.row(id)
    if (session) await this.events.announce(id, [event({ type: 'session.owner', sessionId: id, owner: session.owner })])
  }

  /**
   * Branches a session: the SDK's `forkSession()` copies the transcript into a
   * new session id through this store (sdk.d.ts: "Fork a session into a new
   * branch with fresh UUIDs"; "When provided, read/write session data via this
   * store"). The child is owned by whoever forked it, records its parent, and
   * starts with the parent's conversation events so attach shows its history.
   */
  async fork(
    id: string,
    principal: Owner,
    options: { title?: string; origin?: Origin; rateLimited?: boolean } = {},
  ): Promise<SessionRecord> {
    const parent = await this.get(id, principal)
    if (!(await this.store.exists(id))) {
      throw new SessionError('invalid', `session ${id} has no transcript to fork yet; send it a turn first`)
    }
    const title = options.title?.trim() || `${parent.title || 'session'} (fork)`
    // A fork is a new session and counts against the same limit as a start (PR #715
    // review). Checked first too, so a refused fork leaves no SDK copy behind; the
    // insert's check under the lock is the one that holds.
    await this.withinNewSessionLimit(principal, this.deps.sql)
    const { sessionId: childId } = await sdkForkSession(id, {
      sessionStore: this.store,
      dir: sessionWorkDir(this.deps.paths, id),
      title,
    })
    const child = await this.insert(childId, principal, {
      origin: options.origin ?? parent.origin,
      title,
      tags: parent.tags,
      scope: parent.scope,
      parentId: parent.id,
    }, { rateLimited: options.rateLimited ?? true })
    // The conversation so far, re-addressed to the child. Lifecycle events
    // (status, owner, result) are the parent's own and are not copied.
    const history: ServerEvent[] = []
    for (let after = 0; ; ) {
      const page = await this.events.read(id, after)
      if (page.length === 0) break
      after = page.at(-1)?.seq ?? after
      for (const { event: e } of page) {
        if (
          e.type === 'user.turn' ||
          e.type === 'assistant.text.delta' ||
          e.type === 'assistant.text.done' ||
          e.type === 'tool.call' ||
          e.type === 'tool.result'
        ) {
          history.push({ ...e, sessionId: childId })
        }
      }
    }
    await this.events.append(childId, [
      event({
        type: 'session.started',
        sessionId: childId,
        origin: child.origin,
        owner: child.owner,
        title,
        budgetUsd: child.budgetUsd,
      }),
      ...history,
      event({ type: 'session.status', sessionId: childId, status: 'idle' }),
    ])
    return child
  }

  /**
   * Adds `addUsd` to one session's budget (#790). User-only and owner-only:
   * it spends money, so only the browser user may raise it, and only on a
   * session it owns (take one over first). `agentActor` is set when the
   * request carried the headless browser's agent-actor marker
   * (harness/headlessBrowser.ts AGENT_ACTOR_HEADER), and is refused like any
   * other principal. No tool calls this; the one caller is the HTTP route
   * (routes/sessions.ts). A raise is a `settings` audit row on the session
   * (action `session_budget_usd`); a refused or failed one is recorded by
   * app.ts's `auditWrites` from the route's status, like other refused writes.
   */
  async raiseBudget(
    id: string,
    principal: Owner,
    addUsd: number,
    context: Omit<AuditContext, 'actor'> & { agentActor?: boolean },
  ): Promise<SessionRecord> {
    const startedAt = new Date()
    if (context.agentActor || principal.kind !== 'browser') {
      throw new SessionError('forbidden', "only you can raise a chat's budget, in the ScadBuddy UI")
    }
    const add = cents(addUsd)
    if (!Number.isFinite(add) || add <= 0) throw new SessionError('invalid', 'the raise must be at least $0.01')
    const session = await this.get(id, principal)
    if (!sameOwner(principal, session.owner)) {
      throw new SessionError(
        'forbidden',
        `session ${id} is controlled by ${session.owner.label}; take it over before raising its budget`,
      )
    }
    // One statement, so two raises add up and the owner cannot change in between.
    const [row] = await this.deps.sql.unsafe<Row[]>(
      `UPDATE ai_sessions SET budget_usd = round((budget_usd + $2)::numeric, 2)::double precision, updated_at = now()
       WHERE id = $1 AND owner_kind = $3 AND owner_id = $4 AND round((budget_usd + $2)::numeric, 2) <= $5
       RETURNING ${COLUMNS}`,
      [id, add, principal.kind, principal.id, MAX_SESSION_BUDGET_USD],
    )
    if (!row) {
      const now = (await this.row(id)) ?? session
      throw sameOwner(principal, now.owner)
        ? new SessionError(
            'invalid',
            `a chat's budget can be at most ${usd(MAX_SESSION_BUDGET_USD)}; this one is ${usd(now.budgetUsd)}`,
          )
        : new SessionError('busy', `session ${id} changed owner meanwhile; try again`)
    }
    const raised = record(row)
    await this.deps.audit?.record({
      kind: 'settings',
      action: 'session_budget_usd',
      surface: context.surface,
      actor: principal,
      clientIp: context.clientIp,
      sessionId: id,
      outcome: 'ok',
      detail: `${usd(raised.budgetUsd - add)} + ${usd(add)} = ${usd(raised.budgetUsd)} (${usd(raised.costUsd)} spent)`,
      startedAt,
      finishedAt: new Date(),
    })
    await this.events.append(id, [
      event({ type: 'session.budget', sessionId: id, costUsd: raised.costUsd, budgetUsd: raised.budgetUsd }),
    ])
    return raised
  }

  /** Aborts every turn running in this process (shutdown); each releases its claim as interrupted. */
  abortAll(): void {
    for (const { controller } of this.active.values()) controller.abort(new Error(SHUTTING_DOWN))
  }

  /**
   * Once every turn running now has finished, its last events appended: what a
   * shutdown waits for after abortAll(), before it closes the database those appends
   * use (#802).
   */
  async settled(): Promise<void> {
    await Promise.allSettled([...this.active.values()].map((turn) => turn.done))
  }

  /** From now on no turn starts in this process: sends get `busy` (RESTARTING). */
  drain(): void {
    this.draining = true
  }

  /**
   * A restart's end of the turns running here (main.ts, on SIGTERM): no new
   * turn starts, the running ones get `graceMs` to finish on their own, then
   * the rest are aborted, and each gets up to `abortWaitMs` more to record
   * that it stopped (finish(): the interrupted event, the session's status,
   * the claim released). Without that wait the pool closed under them, and a
   * session was left `running` with no event saying why (2026-09-30, a deploy
   * mid-turn). Resolves once every turn has ended or the waits are up.
   */
  async stopTurns({ graceMs, abortWaitMs }: { graceMs: number; abortWaitMs: number }): Promise<void> {
    this.drain()
    const ended = () => this.settled()
    const within = (ms: number, work: Promise<unknown>) =>
      Promise.race([work, new Promise((resolve) => setTimeout(resolve, ms).unref())])
    await within(graceMs, ended())
    if (this.active.size === 0) return
    this.abortAll()
    await within(abortWaitMs, ended())
  }

  /**
   * Ends the turns whose lease ran out: their process died before finish()
   * (a SIGKILL, an OOM, a pool closed under them), so nothing else would say
   * the turn stopped. A send could already take such a session over (its
   * claim accepts an expired lease), but until then its row said `running`
   * and its log had no end. A live turn renews its lease every `renewMs`, so
   * an expired one is dead. One UPDATE, so two replicas' reapers never both
   * take the same row. The session goes to `waiting_approval` when an
   * approval is still pending (a shutdown keeps those for after the restart,
   * #258), otherwise `idle`; approved-but-unused approvals of the dead turn
   * are revoked, as finish() does on a shutdown. Returns the sessions reaped.
   */
  async reapExpired(): Promise<string[]> {
    const rows = await this.deps.sql<{ id: string; status: SessionStatus; dead_turn: string }[]>`
      WITH dead AS (
        SELECT id, turn_id FROM ai_sessions
        WHERE turn_id IS NOT NULL AND lease_until <= now()
        FOR UPDATE SKIP LOCKED
      )
      UPDATE ai_sessions s
      SET status = CASE
            WHEN s.status = 'done' THEN 'done'
            WHEN EXISTS (SELECT 1 FROM ai_approvals a WHERE a.session_id = s.id AND a.decision IS NULL)
              THEN 'waiting_approval'
            ELSE 'idle'
          END,
          turn_id = NULL, lease_until = NULL, interrupt_requested = false, updated_at = now()
      FROM dead
      WHERE s.id = dead.id
      RETURNING s.id, s.status, dead.turn_id AS dead_turn`
    for (const { id, status, dead_turn } of rows) {
      await this.approvals.revokeUnused(id, 'the turn ended')
      // The dead turn's questions only (questions/service.ts).
      await this.questions.cancelPending(id, 'the turn ended', { refresh: false, turnId: dead_turn })
      await this.events.append(id, [
        event({
          type: 'error',
          sessionId: id,
          code: 'interrupted',
          message: 'the turn was interrupted (the agent service restarted)',
        }),
        event({ type: 'session.status', sessionId: id, status }),
      ])
    }
    return rows.map((r) => r.id)
  }

  /** Runs `reapExpired` now and every `intervalMs` (main.ts), like the approval sweeper. */
  startReaper(
    intervalMs: number,
    options: { ready?: () => Promise<boolean>; onError?: (err: unknown) => void } = {},
  ): () => void {
    const ready = options.ready ?? (() => Promise.resolve(true))
    const sweep = () => {
      ready()
        .then((ok) => (ok ? this.reapExpired() : []))
        .catch(options.onError ?? (() => {}))
    }
    sweep()
    const timer = setInterval(sweep, intervalMs)
    timer.unref()
    return () => clearInterval(timer)
  }
}
