import type { AccessResolver } from '../access/resolver.js'
import type { nestedQueries } from '../builtins/nested.js'
import type { BackgroundTasks } from '../background/service.js'
import { randomUUID } from 'node:crypto'
import {
  type EffortLevel,
  forkSession as sdkForkSession,
  type McpSdkServerConfigWithInstance,
  type SDKMessage,
  type SDKResultMessage,
  type SDKUserMessage,
  type SessionStoreEntry,
} from '@anthropic-ai/claude-agent-sdk'
import type { Context } from '@opentelemetry/api'
import type { Sql } from 'postgres'
import type { Tier } from '../auth/principal.js'
import type { Credential } from '../credentials.js'
import type { PayloadKeys } from '../temporal/payloadCodec.js'
import type { ProbeVerdict } from '../harness/credentialErrors.js'
import type { CredentialSource } from '../harness/fallback.js'
import type { HarnessPaths } from '../harness/options.js'
import type { ApprovalPreparer, TierResolver } from '../harness/permissions.js'
import type { SeamLedger } from '../harness/seamLedger.js'
import type { BuiltInPolicy } from '../plugins/builtInTools.js'
import { DEFAULT_MAX_BUDGET_USD, DEFAULT_MAX_TURNS, type HarnessRun } from '../harness/run.js'
import { type HttpLimits, type SavedMeta, savedBody } from '../harness/httpRequest.js'
import type { Resolver } from '../http/egress.js'
import type { PluginsForRun } from '../plugins/forwarder.js'
import type { MemoryOptions } from '../memory/hindsight.js'
import type { LoadedPackage, PackagesForRun } from '../plugins/packages/install.js'
import type { PackageServers } from '../plugins/packages/mcp.js'
import { isUuid, sessionWorkDir } from '../harness/stateDirs.js'
import { type ApprovalRecord, ApprovalService, type GrantCheck, type ResumeResult } from '../approvals/service.js'
import { QuestionService } from '../questions/service.js'
import { attentionCard, attentionSpec, BACK_REPLIES, DEFAULT_REPLIES, IM_BACK, parseAttention, timedOutText } from '../harness/attention.js'
import type { QuestionGate } from '../harness/questions.js'
import { SERVER_NAME } from '../tools/projections.js'
import type { FlowTurn, TabWait, Tool, WaitForTab } from '../tools/registry.js'
import type { AuditContext, AuditDeps } from '../audit/log.js'
import { turnCostEntry } from '../audit/turn.js'
import type { TurnTrace } from '../telemetry/turn.js'
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
  type SentImage,
  type ServerEvent,
  type SessionMode,
  type SessionStatus,
} from './protocol.js'
import { SessionBlobs, sentImageBlobs, type StoredBlob } from './blobs.js'
import { PgHttpBodies } from './httpBodies.js'
import { SessionFiles } from '../workspace/store.js'
import { SessionEdits } from './edits.js'
import { blobNames, durableReplies, entriesThrough, forkHistory, historyThroughCalls, transcriptCut, unansweredCalls } from './forkPoint.js'
import { scrubForLog, type TitleResolver } from './sdkEvents.js'
import { PostgresSessionStore } from './store.js'
import { userPrompt, type UserImage } from './images.js'
import { type ResourceRef, SessionResources, type TouchedRecord } from './touched.js'
import { type DurableGate, DurableUnavailable } from '../gate/durable.js'
import type { DurableTurns, ForkedAgent } from './durable.js'
import { PendingProjection } from '../gate/projection.js'
import { Escalations } from '../flows/escalations.js'
import { TurnRunner } from './turnRunner.js'
import { type WarmQuery, WarmSessions } from './warm.js'
import { notifyWarmEvict } from './warmEvict.js'
import { CoreTurn, type Refused, tallyTurn, type TurnCoreDeps, wordTail } from './turnCore.js'
import { POOL_BUDGET, POOL_COST, systemAppend } from './turnShared.js'
import { FlowQueue } from '../flows/queue.js'

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
// naming what this replica can tell of the cause, a `turn` audit row, and the
// session back to idle, since its own finish() never ran (#2375). The same
// renewal reads `interrupt_requested`, which is how an interrupt reaches a turn
// running on another replica. On SIGTERM, `stopTurns` drains: no new turn,
// running ones may finish, the rest are aborted and waited for, and the warm
// processes are closed.
//
// Processes (unified runtime spec 2026-10-13 §5.3). A classic session's turns
// run on one warm Claude Code per replica (`warm`, warm.ts), a streaming-input
// query that each turn pushes its prompt into (warmTurn.ts): a turn after the
// first spawns nothing while the process still matches it (its fingerprint,
// the transcript tail, the budget), and respawns from the transcript when not.
// A turn's cost is the difference of its process's cumulative total (§6.6).
// An interrupt is `query.interrupt()`; the process answers with its result and
// stays. A fallback to another credential respawns (§6.7).
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
// that spends money. A fork does not get a budget of its own: it spends from
// its parent's, and so does a fork of that fork (#823). The budget belongs to
// the lineage's root session (`budget_root_id`); every check and every meter
// reads the root's budget against what all of the lineage has spent, and a
// raise on any member raises the root's.

/** ai_settings keys (non-secret, spec §9). */
export const SETTING_SESSION_MAX_TURNS = 'session_max_turns'
export const SETTING_SESSION_BUDGET_USD = 'session_max_budget_usd'
/** Which mode a new session runs in when its creator names none: `durable` unless it says `classic` (plan 5d Ruling 1). */
export const SETTING_SESSION_MODE = 'session_mode'
export { SESSION_MODES, type SessionMode } from './protocol.js'
export { BROWSER_TOOLS_GUIDE, MEMORY_TEXT_MAX, memoryText, POOL_BUDGET, POOL_COST, recordMemoryActivity, SETTING_MODEL, systemAppend } from './turnShared.js'

/** The stored `session_mode` as a mode: `classic` only when it says so (plan 5d Ruling 1, the owner's default). */
export function sessionModeOf(stored: unknown): SessionMode {
  return stored === 'classic' ? 'classic' : 'durable'
}

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

/**
 * #815 §2: how a turn's browser_* call that found no tab waits for it, as a
 * `tab_disconnected` attention request on the turn's question gate: shown on the
 * panel and the badge, resolved `reconnected` when the session's tab is back
 * (bridge/hub.ts `onSessionTab`), or by the user's reply, or by its timer, which
 * lets the agent carry on without the tab (never approving anything). Calls that
 * fail together wait on one request, not one each (#815 §5 would supersede them).
 */
export const TAB_WAIT_S = 300
/**
 * How many tab waits one turn may open. A tab back on another replica answers
 * `reconnected` while this replica still has none, so each retry can fail and
 * wait again; this bounds that, and tab waits are outside the model's own rate
 * limit (questions/service.ts).
 */
export const TAB_WAITS_PER_TURN = 3
const CARRY_ON = 'Carry on without the tab'

export function waitForTab(gate: QuestionGate, turn: AbortSignal, reconnected: (questionId: string) => Promise<unknown>): WaitForTab {
  let open: { wait: Promise<TabWait>; waiters: number; stop: AbortController } | undefined
  // Once the user said to carry on, typed a reply of their own, or nobody came
  // back in time, the rest of the turn does not ask again: each later call that
  // finds no tab fails at once. The latch lasts until the turn ends, never less.
  let gaveUp: string | undefined
  let started = 0
  return ({ tool, toolUseId, signal, isBack }) => {
    // A call already stopped opens nothing: no row, no card, no use of the turn's waits.
    if (signal.aborted) return Promise.resolve({ back: false, message: 'The call stopped before it waited for the tab.' })
    if (gaveUp !== undefined) return Promise.resolve({ back: false, message: gaveUp })
    if (!open && started >= TAB_WAITS_PER_TURN) {
      // Neutral on purpose (#1393): the waits counted here may have ended any
      // way (a reconnect to this replica or another, a withdrawn call), and this
      // replica cannot tell which, so the message names no cause.
      gaveUp =
        `The tab was waited for ${TAB_WAITS_PER_TURN} times this turn and is not attached here now. Carry on without ` +
        'the tab for the rest of this turn.'
      return Promise.resolve({ back: false, message: gaveUp })
    }
    if (!open) started += 1
    // One wait for the turn's calls: a call that stops waiting (its own signal)
    // leaves the others waiting, and the last one to stop withdraws the request.
    open ??= start(tool, toolUseId, isBack)
    const shared = open
    shared.waiters += 1
    return new Promise<TabWait>((resolve, reject) => {
      const withdrawn = () => {
        resolve({ back: false, message: 'The call stopped while it waited for the tab.' })
        shared.waiters -= 1
        if (shared.waiters === 0) {
          // Withdrawn: a call that comes after must open a wait of its own, not join this one.
          if (open === shared) open = undefined
          shared.stop.abort()
        }
      }
      if (signal.aborted) return withdrawn()
      signal.addEventListener('abort', withdrawn, { once: true })
      shared.wait.then(resolve, reject).finally(() => signal.removeEventListener('abort', withdrawn))
    })
  }

  function start(tool: string, toolUseId: string | undefined, isBack: (signal: AbortSignal) => Promise<boolean>): NonNullable<typeof open> {
    const stop = new AbortController()
    const wait = (async (): Promise<TabWait> => {
      const parsed = parseAttention({
        reason: 'tab_disconnected',
        // Not named after `tool`: the turn's other calls share the card, and the one that
        // opened it may stop waiting first (#1341). The row keeps the opener's id.
        message:
          'I need your ScadBuddy tab, but it is not connected. Open ScadBuddy (or reload it) and open ' +
          "this chat in the assistant panel. A change that was waiting is not made on its own when the tab is back. " +
          "Answer I'm back to have me try again; a reply of your own words ends my waits for the tab this turn, and " +
          'without the tab I carry on with what needs no tab.',
        options: [IM_BACK, CARRY_ON],
        timeout_s: TAB_WAIT_S,
      })
      if (!parsed.ok) throw new Error(parsed.error)
      const spec = attentionSpec(parsed.input)
      // A tab wait is never a done summary; this narrows the spec to one that waits.
      if (spec.reason === 'done') throw new Error('a tab wait is not a done summary')
      const card = attentionCard(parsed.input)
      const verdict = await gate({
        tool: `mcp__${SERVER_NAME}__${tool}`,
        questions: [card],
        toolUseId: toolUseId ?? `tab-wait-${randomUUID()}`,
        // Withdrawn by the turn, or once no call waits on it any more.
        signal: AbortSignal.any([turn, stop.signal]),
        attention: {
          ...spec,
          // The tab may have come back between the failed call and the row: the
          // hub saw nothing to resolve then, so look once now that there is one.
          // A check that outlives the wait does nothing. The one that passes that
          // test can still be held up (a lock, the pool) until a later wait has
          // opened, so it resolves only this wait's row, never the session's (#1360).
          // The check gets the same signal, so one that hangs (#1352) is cancelled
          // with the wait rather than held for the life of the process.
          onParked: async (parked, questionId) => {
            if ((await isBack(parked)) && !parked.aborted) await reconnected(questionId)
          },
        },
      })
      if ('reconnected' in verdict && verdict.reconnected) {
        // reconnected() runs on whichever replica saw the tab. isBack reaches a tab on
        // another replica too (bridge/relay.ts, #4780); when it still finds none, nothing
        // here will end a later wait but its timer (#1308), so the rest of the turn does
        // not wait. A failed check does not latch.
        if (!(await isBack(turn).catch(() => true))) {
          gaveUp =
            'The tab reconnected, but it cannot be reached from this agent replica now. Carry on ' +
            'without the tab for the rest of this turn.'
        }
        return { back: true, why: 'reconnected' }
      }
      // Only "I'm back" (or "I'm here") means try again. "Carry on" ends the turn's tab waits; any
      // other reply is the user's own words, which the model must read, so the
      // call is not run and its error carries them. It ends the turn's tab waits
      // too, for the rest of the turn: the user is not asked twice in one turn,
      // and a later call says so without telling the model to act on the reply
      // again (the call that carried it already did).
      if (verdict.answered) {
        const reply = verdict.answers[card.question] ?? ''
        // The model's own tab_disconnected card, which this wait may have joined
        // (questions/service.ts gate), says the same in its own words.
        if (BACK_REPLIES.includes(reply)) return { back: true, why: 'user_back' }
        if (reply !== CARRY_ON && reply !== DEFAULT_REPLIES[1]) {
          gaveUp =
            `The user already replied ${JSON.stringify(reply)} when asked for the tab this turn, so the tab is not ` +
            'asked for again this turn and this call was not run. Carry on without the tab for the rest of this turn.'
          return { back: false, message: `The user replied ${JSON.stringify(reply)} instead; the call was not run. Act on their reply.` }
        }
        gaveUp = `The user replied ${JSON.stringify(reply)}: carry on without the tab for the rest of this turn.`
        return { back: false, message: gaveUp }
      }
      if ('timedOut' in verdict && verdict.timedOut) {
        gaveUp = timedOutText(TAB_WAIT_S)
        return { back: false, message: gaveUp }
      }
      return { back: false, message: verdict.message }
    })()
    const handle = { wait, waiters: 0, stop }
    void wait.finally(() => {
      if (open === handle) open = undefined
    }).catch(() => undefined)
    return handle
  }
}

/**
 * #815 §2: a session whose tab is connected again (bridge/hub.ts `onSessionTab`)
 * stops waiting for it. main.ts wires the hub with this.
 */
export function resolveTabWaits(tabs: { onSessionTab: ((sessionId: string) => Promise<unknown>) | undefined }, sessions: SessionManager): void {
  tabs.onSessionTab = (sessionId) => sessions.questions.reconnected(sessionId)
}

/**
 * An attention request's `stop` or `wait` timer ended the turn (#815), not a
 * user's Stop: the session's `interrupted` error says why (#1206).
 */
class AttentionStop extends Error {}

/** The `interrupted` error's message: why, when an attention request's timer ended the turn. */
function interruptedMessage(signal: AbortSignal): string {
  const reason: unknown = signal.reason
  return reason instanceof AttentionStop ? `the turn ended: ${reason.message}` : 'the turn was interrupted'
}

function abortMessage(signal: AbortSignal): string {
  const reason: unknown = signal.reason
  return reason instanceof Error ? reason.message : 'the turn was interrupted'
}

export const DEFAULT_LEASE_MS = 60_000

/** A lease's length for a person: "a minute", "90 seconds". */
export function leaseWords(ms: number): string {
  const seconds = Math.round(ms / 1000)
  if (seconds === 60) return 'a minute'
  if (seconds % 60 === 0) return `${seconds / 60} minutes`
  return seconds === 1 ? 'a second' : `${seconds} seconds`
}
/** A lapsed turn's `turn` row when a new turn took its session before the reaper ran (#3320). */
export const TAKEN_OVER =
  'the turn was stopped: its claim on the session lapsed and a new turn took the session (the replica running it stopped, or could not reach the database)'
/** How long a handoff offer waits for its target (`handoff`). */
export const HANDOFF_OFFER_TTL_MS = 60 * 60_000
export const DEFAULT_RENEW_MS = 1_000
/** A lease renewal this slow is logged (#2375): a minute without one and the reaper ends the turn. */
export const SLOW_RENEW_MS = 5_000

/**
 * Renews a turn's lease every `everyMs`, one renewal at a time (#2375): one still
 * waiting on a stalled pool is not joined by another every second, which would only
 * queue more on the pool it waits for. A renewal that fails, or takes SLOW_RENEW_MS or
 * more, is logged when it ends, and one still waiting at SLOW_RENEW_MS is logged then,
 * once, so a renewal that never ends shows before the reaper says so (#2981 review).
 * The lease covers several, but a minute of them is what ends the turn, and until now
 * nothing said so. `stop` waits for the one in flight.
 */
export function renewLoop(renew: () => Promise<void>, everyMs: number, log: (line: string) => void): { stop(): Promise<void> } {
  let inFlight: Promise<void> | undefined
  let started = 0
  let warned = false
  const timer = setInterval(() => {
    if (inFlight) {
      const waited = Date.now() - started
      if (!warned && waited >= SLOW_RENEW_MS) {
        warned = true
        log(`lease renewal still waiting after ${waited} ms`)
      }
      return
    }
    started = Date.now()
    warned = false
    inFlight = renew()
      .then(
        () => {
          const took = Date.now() - started
          if (took >= SLOW_RENEW_MS) log(`lease renewal took ${took} ms`)
        },
        // One line a renewal: a failure says how long it took (#3321).
        (err: unknown) => log(`lease renewal failed after ${Date.now() - started} ms: ${describe(err)}`),
      )
      .finally(() => {
        inFlight = undefined
      })
  }, everyMs)
  return {
    async stop() {
      clearInterval(timer)
      await inFlight
    },
  }
}

export type SessionErrorCode =
  | 'not_found'
  | 'forbidden'
  | 'busy'
  | 'budget_exhausted'
  | 'closed'
  | 'invalid'
  | 'no_transcript'
  | 'rate_limited'
  /** A durable session's workflow cannot be reached, or this service has no Temporal or key for one. */
  | 'unavailable'
  /** The session is archived (#1885): read-only until it is unarchived. */
  | 'archived'

type SessionErrorStatus = 400 | 403 | 404 | 409 | 429 | 503

const STATUS_OF: Record<SessionErrorCode, SessionErrorStatus> = {
  not_found: 404,
  forbidden: 403,
  busy: 409,
  budget_exhausted: 409,
  closed: 409,
  invalid: 400,
  no_transcript: 409,
  rate_limited: 429,
  unavailable: 503,
  archived: 409,
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

/** What a send or a handoff to an archived session is refused with (#1885). */
export function archivedError(id: string): SessionError {
  return new SessionError('archived', `session ${id} is archived, so it is read-only; unarchive it to continue it, or fork it`)
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
  /** `classic` runs in this service's harness, `durable` as a DurableSession workflow; set at insert. */
  mode: SessionMode
  maxTurns: number
  /** The budget this session spends from, shared with its forks and its parent's lineage (#823). */
  budgetUsd: number
  /** What that lineage has spent, every member's turns included: what `budgetUsd` is checked against. */
  costUsd: number
  /** What this session's own turns have spent. */
  ownCostUsd: number
  turns: number
  /** A turn holds the claim (on some replica) right now. */
  turnActive: boolean
  createdAt: string
  updatedAt: string
  /** When its owner archived it (#1885, edits.ts); null while it is not archived. */
  archivedAt: string | null
}

/**
 * A session as its turn claimed it, with how much of `costUsd` is spend
 * Claude Code never priced (#991, the `unpriced_cost_usd` column): a resumed
 * query's `total_cost_usd` does not hold that part.
 */
export type ClaimedSession = SessionRecord & {
  unpricedCostUsd: number
  /** A Workflow agent's chat (startChild): its own model, effort and subagent definition. */
  agentModel: string | null
  agentEffort: EffortLevel | null
  agentType: string | null
  /** The claim's `now()` as Postgres wrote it, microseconds kept: `updatedAt` is a JS Date's milliseconds (#1383). */
  claimedAt: string
}

/**
 * The claim's `updated_at` as `claimedAt`: ISO 8601 in UTC at Postgres's microseconds,
 * whatever the connection's `DateStyle` and `TimeZone` (a plain `::text` follows them).
 */
export const CLAIMED_AT = `to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS claimed_at`

type ClaimRow = Row & { unpriced_cost_usd: number; claimed_at: string; agent_model: string | null; agent_effort: EffortLevel | null; agent_type: string | null }

const claimed = (row: ClaimRow): ClaimedSession => ({
  ...record(row),
  unpricedCostUsd: row.unpriced_cost_usd,
  agentModel: row.agent_model,
  agentEffort: row.agent_effort,
  agentType: row.agent_type,
  claimedAt: row.claimed_at,
})

/**
 * The session as its running durable turn holds it (an AgentSession's `session_turn`,
 * temporal/session/turnHost.ts): undefined unless the row is running exactly that turn
 * (`durable_turn_id`, written by DurableTurns.send's claim), with the sender's tiers.
 */
export async function runningDurableTurn(
  sql: Sql,
  id: string,
  turnId: string,
): Promise<{ session: ClaimedSession; principal: TurnPrincipal } | undefined> {
  if (!isUuid(id)) return undefined
  // A claim's shape, so a Workflow agent's model, effort and type assemble as its classic turns do.
  const [row] = await sql.unsafe<(ClaimRow & { turn_tiers: { tiers?: Tier[] } | null })[]>(
    `SELECT ${COLUMNS}, unpriced_cost_usd, agent_model, agent_effort, agent_type, ${CLAIMED_AT}, turn_tiers FROM ai_sessions
     WHERE id = $1 AND durable_turn_id = $2 AND status IN ('running', 'waiting_approval', 'waiting_input')`,
    [id, turnId],
  )
  if (!row) return undefined
  const tiers = row.turn_tiers?.tiers
  return { session: claimed(row), principal: tiers ? { tiers } : {} }
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
  /** With `prompt`: see SendOptions.images. */
  images?: readonly UserImage[]
  /** With `prompt`: see SendOptions.tiers. */
  tiers?: readonly Tier[]
  /** With `prompt`: see SendOptions.flow. */
  flow?: FlowTurn
  /**
   * The session's mode. Named, it is kept or refused (`unavailable`). Omitted, the
   * `session_mode` setting's applies, and a durable one this service cannot run now
   * becomes classic, with the reason in `modeFallback` (plan 5d Ruling 2).
   */
  mode?: SessionMode
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
   * Images the user sent with the message (#1866, images.ts): the model gets
   * them as image blocks before the text; the `user.turn` event names them and
   * their previews, stored with the session first (#2479).
   */
  images?: readonly UserImage[]
  /**
   * The sender's tiers (spec §8.1), for the turn's in-process tools: an MCP
   * token's, as `/mcp` authenticated it for this send (tools/sessions.ts).
   * Left out, the owner's default applies (auth/principal.ts
   * `harnessPrincipal`: everything for the browser user, `read` otherwise).
   */
  tiers?: readonly Tier[]
  /**
   * The flow run and step that send this turn (#2481), set only by the sessions tools
   * from a flow activity's own context (ToolContext.flowTurn): a durable turn carries
   * it, so the run's grant may cover its calls. A classic turn ignores it.
   */
  flow?: FlowTurn
}

/** Who a turn's in-process tools act for, beyond the session's owner (SendOptions.tiers). */
export type TurnPrincipal = { tiers?: readonly Tier[] }

export type ListFilter = {
  status?: SessionStatus
  origin?: Origin
  limit?: number
  /** Only sessions whose tool calls touched it (#931, touched.ts `ResourceRef`). */
  resource?: ResourceRef
  /**
   * Archived sessions (#1885): left out (`exclude`, the default, as the panel's list
   * leaves them out), the only ones listed (`only`, the archive view), or both (`include`).
   */
  archived?: 'exclude' | 'only' | 'include'
}

/** Reads ai_settings; SettingsStore (credentials.ts) is one. */
export type SettingsReader = { get<T>(key: string): Promise<T | undefined> }

/**
 * Starts one Claude Code process's query (the warm cache's spawn, warm.ts);
 * `runHarness` in production, a scripted stand-in in unit tests. The stream
 * serves every turn of the process: its `prompt` is the process's inbox.
 */
export type QueryRunner = (run: HarnessRun) => AsyncIterable<SDKMessage>

/** A warm process per replica past which the least recently used idle one goes (spec 2026-10-13 §5.3, D3) (main.ts passes the stored `warm_max_per_replica`). */
export const DEFAULT_WARM_MAX_ENTRIES = 8
/** How long a warm process may sit idle (spec §5.3: the same 10 min as temporal/sessionClients.ts) (main.ts passes the stored `warm_idle_seconds`). */
export const DEFAULT_WARM_IDLE_MS = 600_000

const isWarmQuery = (stream: AsyncIterable<SDKMessage>): stream is WarmQuery =>
  typeof (stream as Partial<WarmQuery>).interrupt === 'function' && typeof (stream as Partial<WarmQuery>).close === 'function'

/**
 * The cache's `start` over a QueryRunner: a stream with its own `interrupt`
 * and `close` (the SDK's Query, as runHarness returns it) is used as it is; a
 * plain one (a scripted stand-in) is given an abort signal, which `interrupt`
 * and `close` abort, as the SDK's abort ends a query.
 */
function startedQuery(run: QueryRunner): (spawn: HarnessRun) => WarmQuery {
  return (spawn) => {
    const controller = new AbortController()
    const started = run({ ...spawn, signal: controller.signal })
    if (isWarmQuery(started)) return started
    const iterator = started[Symbol.asyncIterator]()
    return {
      [Symbol.asyncIterator]: () => iterator,
      interrupt: () => {
        controller.abort(new Error('the turn was interrupted'))
        return Promise.resolve(undefined)
      },
      close: () => {
        controller.abort(new Error('the process was closed'))
        void iterator.return?.().catch(() => {})
      },
    }
  }
}

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
  /**
   * Tool access (#2171, ai_access): the http_request and own-plugin switches, and http_request's
   * tier, per turn; with it, the seam also reads each call's access (access/live.ts). Defaults without it.
   */
  access?: Pick<AccessResolver, 'enabled' | 'entry' | 'item' | 'switchedItem'>
  /** The registry tools the in-process server offers (tools/harness.ts), whose access the seam reads per call. */
  tools?: readonly Tool[]
  tierOf?: TierResolver
  /** Each registry call's title for the panel (#782, tools/harness.ts `titleOf`). */
  titleOf?: TitleResolver
  /** #251's registry: the in-process MCP servers a session's queries get. */
  mcpServers?: (
    session: SessionRecord,
    turn: TurnPrincipal,
    /** #815 §2: how a browser_* call that finds no tab waits for it; only in a session the browser user owns. */
    extras?: {
      waitForTab?: WaitForTab
      turnContext?: () => Context
      builtIn?: BuiltInPolicy
      secrets?: () => readonly string[]
      /** The tier the seam applied to each of the turn's calls (#2171). */
      ledger?: Pick<SeamLedger, 'tierOf'>
    },
  ) => Record<string, McpSdkServerConfigWithInstance>
  /**
   * What an outward call parks, for a registry tool that resolves its arguments (#4442,
   * tools/harness.ts `prepareApproval`): bound to the turn's principal, as `mcpServers` is.
   */
  prepareApproval?: (session: SessionRecord, turn: TurnPrincipal) => ApprovalPreparer
  /**
   * Settings → Tools & plugins' overrides of the built-in tools (#1953,
   * plugins/builtInTools.ts `BuiltInTools.policy`): read once per turn, as the
   * remote plugins are.
   */
  builtInTools?: () => Promise<BuiltInPolicy>
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
   * A turn's vetted packages' http MCP servers, registered with the loopback forwarder
   * as remote plugins are (plugins/packages/mcp.ts `forwardPackageServers`, #2320).
   */
  packageServers?: (loaded: readonly LoadedPackage[]) => Promise<PackageServers>
  /**
   * Automatic memory (memory/hindsight.ts): when a turn's remote plugins
   * include an enabled `hindsight` plugin, its prompt is recalled against and
   * its transcript retained to that plugin's bank. These are the hook knobs
   * (hooks.py `MemoryHookConfig` and friends); `false` turns it off.
   */
  memory?: MemoryOptions | undefined
  /**
   * The assistant's `http_request` tool (#827, harness/httpRequest.ts). A
   * session's turns get it when this is set and `builtin:http` is not switched
   * off in `access` (#2171); it is on by default.
   */
  httpRequest?: {
    /** Tests only: a resolver other than the system's. */
    resolve?: Resolver
    /** Tests only: smaller caps. */
    limits?: Partial<HttpLimits>
  }
  /**
   * The assistant's workspace (#2329, workspace/store.ts): where http_request's
   * `save_to_workspace` writes, and what a fork copies. One over `sql` (caps from
   * `settings`) when omitted.
   */
  workspace?: SessionFiles
  /**
   * A session's background tasks (src/background/): a classic turn whose allowed packages
   * name CronCreate and the rest gets the session's in place of Claude Code's own, which
   * end with the turn. Without it those are Claude Code's.
   */
  background?: { tasks: BackgroundTasks; tz: string; commandQueue?: string }
  run?: QueryRunner
  /**
   * The warm process cache's limits (warm.ts; spec 2026-10-13 §5.3),
   * DEFAULT_WARM_MAX_ENTRIES and DEFAULT_WARM_IDLE_MS when omitted, and where
   * it says why each process went.
   */
  warm?: { maxEntries?: number; idleMs?: number; log?: (message: string) => void }
  /** The session WebFetch's own model calls (builtins/nested.ts); a test's stand-in. */
  nestedQueries?: typeof nestedQueries
  /** Per-token approval grants (spec §6); nobody but the browser user may approve without one. */
  approvalGrants?: GrantCheck
  /**
   * The tiers a principal holds now, when that can be known without its
   * credential (a bearer token's row; auth/tokens.ts `liveTokenTiers`), or
   * undefined when it cannot. A resumed approval's turn gets no more than
   * these (`resumeApproved`).
   */
  currentTiers?: (owner: Owner) => Promise<readonly Tier[] | undefined>
  /** The data keys the transcript is sealed under (D7); without them no transcript line is written. */
  payloadKeys?: PayloadKeys
  /** HMAC key for approval input hashes (approvals/service.ts `approvalHashKey`); per process when omitted. */
  approvalHashKey?: Buffer
  /**
   * The audit log (#258, audit/log.ts): every tool call a turn makes
   * (audit/turn.ts), and every approval decision.
   */
  audit?: AuditDeps
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
  mode: SessionMode
  max_turns: number
  budget_usd: number
  cost_usd: number
  pool_cost_usd: number
  turns: number
  turn_active: boolean
  created_at: Date
  updated_at: Date
  archived_at: Date | null
}

/** A pending handoff offer, read as none once it has expired (the columns stay until the next change clears them). */
const LIVE_OFFER = 'pending_owner_until > now()'

const COLUMNS = `id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_kind END AS offer_kind,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_id END AS offer_id,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_label END AS offer_label,
  CASE WHEN ${LIVE_OFFER} THEN pending_owner_until END AS offer_until,
  status, title, tags, scope, parent_id, mode, max_turns, ${POOL_BUDGET} AS budget_usd, cost_usd,
  ${POOL_COST} AS pool_cost_usd, turns,
  (turn_id IS NOT NULL AND lease_until > now()) AS turn_active, created_at, updated_at, archived_at`

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
    mode: row.mode,
    maxTurns: row.max_turns,
    budgetUsd: row.budget_usd,
    costUsd: row.pool_cost_usd,
    ownCostUsd: row.cost_usd,
    turns: row.turns,
    turnActive: row.turn_active,
    createdAt: row.created_at.toISOString(),
    updatedAt: row.updated_at.toISOString(),
    archivedAt: row.archived_at?.toISOString() ?? null,
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
  const archived = filter.archived ?? 'exclude'
  if (archived !== 'include') where.push(archived === 'only' ? 'archived_at IS NOT NULL' : 'archived_at IS NULL')
  if (filter.resource) {
    // Served by ai_session_resources_model / _resource (20261001T1824Z_session_resources.sql).
    let match: string
    if (filter.resource.type === 'model') {
      params.push(filter.resource.id)
      match = `r.model_slug = $${params.length}`
    } else {
      params.push(filter.resource.type, filter.resource.id)
      match = `r.resource_type = $${params.length - 1} AND r.resource_id = $${params.length}`
    }
    where.push(`EXISTS (SELECT 1 FROM ai_session_resources r WHERE r.session_id = ai_sessions.id AND ${match})`)
  }
  params.push(Math.min(Math.max(filter.limit ?? 100, 1), 500))
  const text = `SELECT ${COLUMNS} FROM ai_sessions ${where.length ? `WHERE ${where.join(' AND ')}` : ''}
       ORDER BY updated_at DESC LIMIT $${params.length}`
  return { text, params }
}

/** A turn running in this process. */
type LocalTurn = {
  controller: AbortController
  /** The turn this is, so the reaper attributes only its own dead turn to it. */
  turnId: string
  /**
   * Set once the turn's claim is gone (a renewal found none, or the reaper took it):
   * the turn then ends without finish(), whose session-wide approval cancel would
   * reach a newer turn's (#2981 review).
   */
  lost: boolean
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
  /** Durable sessions' parked calls as projected in ai_pending_input (spec §6.6). */
  readonly projection: PendingProjection
  /** A flow run's calls waiting at the session that started it (#2482, flows/escalations.ts). */
  readonly escalations: Escalations
  /** A flow run's one queue (#2482, spec 2026-10-11 §4.5). */
  readonly queue: FlowQueue
  /** Renaming a session and marking it done (#795, edits.ts). */
  readonly edits: SessionEdits
  /**
   * A durable session's gate (its workflow's `pending_input`, `respond`, `cancel_input`,
   * `interrupt`), when Temporal is configured; main.ts sets it.
   */
  durable: DurableGate | undefined
  /**
   * How a durable session's turns run (plan 5c PR 3), when Temporal and the secret key
   * are both configured; main.ts sets it. Without it a durable session is refused.
   */
  durableTurns: DurableTurns | undefined
  /**
   * This replica's warm Claude Code processes (warm.ts, spec 2026-10-13 §5.3):
   * one per classic session, serving its turns until it no longer matches
   * them, goes idle, or is evicted (a handoff, done, an archive, a credential,
   * plugin or package change on any replica: warmEvict.ts; main.ts LISTENs).
   */
  readonly warm: WarmSessions
  private readonly deps: SessionManagerDeps
  private readonly leaseMs: number
  private readonly renewMs: number
  /** Turns running in THIS process, by session id. */
  private readonly active = new Map<string, LocalTurn>()
  /** Set by `drain`: a restart is coming, so no new turn starts here. */
  private draining = false
  /** What sessions touched (#931), read by `resources`. */
  private readonly touched: SessionResources
  /** The images tool results carried (#782), read by `blob`. */
  private readonly blobs: SessionBlobs
  private readonly files: SessionFiles
  /** Assembles each turn's query options (turnRunner.ts). */
  private readonly runner: TurnRunner
  /**
   * What a turn on this replica's warm processes needs (turnCore.ts): a classic turn's, and
   * an AgentSession's `session_turn` (temporal/session/turnHost.ts), so both run alike.
   */
  readonly turnCore: TurnCoreDeps

  constructor(deps: SessionManagerDeps) {
    this.deps = deps
    this.store = new PostgresSessionStore(deps.sql, deps.payloadKeys ? { keys: deps.payloadKeys } : {})
    this.touched = new SessionResources(deps.sql)
    this.blobs = new SessionBlobs(deps.sql)
    this.files = deps.workspace ?? new SessionFiles(deps.sql, { settings: deps.settings })
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
      workspace: this.files,
      resume: (approval, by) => this.resumeApproved(approval, by),
    })
    this.questions = new QuestionService({
      sql: deps.sql,
      events: this.events,
      ...(deps.approvalPollMs === undefined ? {} : { pollMs: deps.approvalPollMs }),
      ...(deps.audit ? { audit: deps.audit } : {}),
      ...(deps.settings ? { settings: deps.settings } : {}),
    })
    this.projection = new PendingProjection(deps.sql)
    this.warm = new WarmSessions({
      sql: deps.sql,
      store: this.store,
      maxEntries: deps.warm?.maxEntries ?? DEFAULT_WARM_MAX_ENTRIES,
      idleMs: deps.warm?.idleMs ?? DEFAULT_WARM_IDLE_MS,
      ...(deps.run ? { start: startedQuery(deps.run) } : {}),
      ...(deps.warm?.log ? { log: deps.warm.log } : {}),
    })
    this.queue = new FlowQueue(deps.sql)
    this.escalations = new Escalations({
      sql: deps.sql,
      durable: () => this.durable,
      ...(deps.currentTiers ? { currentTiers: deps.currentTiers } : {}),
      // A delivery turn (#2482): as the owner, with the tiers its owner holds now.
      send: async (id, owner, text, tiers) => {
        await this.send(id, owner, text, { tiers })
      },
    })
    this.edits = new SessionEdits({
      sql: deps.sql,
      events: this.events,
      get: (id, principal) => this.get(id, principal),
      ...(deps.audit ? { audit: deps.audit } : {}),
      endDurable: (id) => this.endDurable(id),
      warm: this.warm,
      endBackground: (id, why) => this.deps.background?.tasks.endSession(id, why) ?? Promise.resolve(0),
    })
    this.runner = new TurnRunner({ ...deps, store: this.store, events: this.events, files: this.files })
    this.turnCore = {
      runner: this.runner,
      warm: this.warm,
      events: this.events,
      blobs: this.blobs,
      credentials: deps.credentials,
      ...(deps.probe ? { probe: deps.probe } : {}),
      ...(deps.titleOf ? { titleOf: deps.titleOf } : {}),
      ...(deps.audit ? { audit: deps.audit } : {}),
      ...(deps.stderr ? { stderr: deps.stderr } : {}),
    }
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

  /**
   * Drops each idle warm process that a fresh assembly of its session would
   * not reuse (warm.ts `revalidate`): the periodic backstop for a NOTIFY that
   * never arrived (sessions/runtimeSync.ts). The session's owner and the
   * process's own tiers and retry bound stand in for the turn's, as they are
   * all fingerprint inputs a turn does not change.
   */
  revalidateWarm(): Promise<void> {
    return this.warm.revalidate(async (entry) => {
      // A claim's shape, so a Workflow agent's model, effort and type assemble as its turns do.
      const [row] = await this.deps.sql.unsafe<ClaimRow[]>(
        `SELECT ${COLUMNS}, 0::float8 AS unpriced_cost_usd, agent_model, agent_effort, agent_type, ${CLAIMED_AT} FROM ai_sessions WHERE id = $1`,
        [entry.sessionId],
      )
      if (!row) return undefined
      const parts = entry.turn.fingerprintParts
      const fresh = await this.runner.assemble(claimed(row), parts.tiers ? { tiers: parts.tiers as Tier[] } : {}, {
        // A rate-limited credential is still the one a healthy process runs on.
        includeCooling: true,
      })
      fresh.fingerprintParts.transientRetries = parts.transientRetries
      return fresh
    })
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

  /**
   * One of the images the session's tool results carried (#782, blobs.ts), by
   * its name; not_found for a session the principal may not see, a name that is
   * not a blob's, or one that is not this session's.
   */
  async blob(id: string, name: string, principal: Owner): Promise<StoredBlob> {
    await this.get(id, principal)
    const blob = await this.blobs.get(id, name)
    if (!blob) throw new SessionError('not_found', `no image ${name} in session ${id}`)
    return blob
  }

  /**
   * A response body the session's http_request saved (#1292), by the id its
   * audit row names; not_found for a session the principal may not see, or a
   * body that is not (or no longer) in the session's directory on this replica.
   */
  /** `id` is the saved response's id as `savedBody` checked it: a UUID, safe in a header or a path. */
  async httpBody(id: string, saved: string, principal: Owner): Promise<{ id: string; meta: SavedMeta; bytes: Buffer }> {
    await this.get(id, principal)
    const body = await savedBody(new PgHttpBodies(this.deps.sql, id), saved)
    if (!body) throw new SessionError('not_found', `no saved response ${saved} in session ${id}`)
    return { id: saved, ...body }
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
        // The switcher's nesting, last activity and spend (#795).
        parentId: s.parentId,
        updatedAt: s.updatedAt,
        costUsd: s.costUsd,
        budgetUsd: s.budgetUsd,
        mode: s.mode,
        // #1284 — the switcher's "Offered to" badge and Cancel offer; an expired offer reads as
        // none. Its target is named only as `principal` may see it, as the session routes do.
        offer: s.offer ? { to: ownerSeenBy(principal, s.offer.to), until: s.offer.until } : null,
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
    fields: {
      origin: Origin
      title: string
      tags: string[]
      scope: Record<string, unknown>
      parentId: string | null
      mode: SessionMode
    },
    options: { rateLimited?: boolean; budgetOf?: SessionRecord } = {},
  ): Promise<SessionRecord> {
    const { maxTurns, budgetUsd: fresh } = await this.limits()
    // A session spending from another's budget (a fork, #823) keeps that budget as its own
    // budget_usd only as a fallback; the root's is the one that counts (POOL_BUDGET).
    const from = options.budgetOf
    const budgetUsd = from ? from.budgetUsd : fresh
    const insert = async (sql: Sql) => {
      await sql`
        INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,
                                 status, title, tags, scope, parent_id, mode, max_turns, budget_usd, budget_root_id)
        VALUES (${id}, ${fields.origin}, ${principal.kind}, ${principal.id}, ${principal.label},
                ${principal.kind}, ${principal.id}, 'idle', ${fields.title}, ${sql.array(fields.tags)},
                ${sql.json(fields.scope as never)}, ${fields.parentId}, ${fields.mode}, ${maxTurns}, ${budgetUsd},
                ${from ? sql`(SELECT coalesce(budget_root_id, id) FROM ai_sessions WHERE id = ${from.id})` : null})`
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

  /** The `session_mode` setting (sessionModeOf). */
  private async modeSetting(): Promise<SessionMode> {
    return sessionModeOf(await this.deps.settings?.get<unknown>(SETTING_SESSION_MODE))
  }

  /** Why a new durable session could not run now, or undefined when it could (plan 5d Ruling 2b). */
  async durableUnready(): Promise<string | undefined> {
    if (!this.durableTurns) {
      return 'durable sessions need Temporal (SCADBUDDY_TEMPORAL_ADDRESS) and the secret key; this agent service has not got them'
    }
    // As the new session would run: the flag picks AgentSession or DurableSession at its first send.
    return this.durableTurns.unready(await this.durableTurns.newRuntime())
  }

  /**
   * The mode a new session runs in (plan 5d Ruling 2). A durable session this service
   * cannot run now (no Temporal, no key, no worker polling) is refused when the creator
   * asked for it, and becomes classic, with the reason, when it came from the setting.
   */
  private async startMode(asked: SessionMode | undefined): Promise<{ mode: SessionMode; fallback?: string }> {
    const mode = asked ?? (await this.modeSetting())
    if (mode === 'classic') return { mode }
    const why = await this.durableUnready()
    if (why === undefined) return { mode }
    if (asked) throw new SessionError('unavailable', why)
    return { mode: 'classic', fallback: why }
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

  async start(
    principal: Owner,
    options: StartOptions,
  ): Promise<{ session: SessionRecord; turn?: Turn; modeFallback?: string }> {
    const prompt = options.prompt?.trim()
    const id = randomUUID()
    const title = options.title?.trim() || (prompt ? titleFrom(prompt) : '')
    const { mode, fallback } = await this.startMode(options.mode)
    const session = await this.insert(id, principal, {
      origin: options.origin,
      title,
      tags: options.tags ?? [],
      scope: options.scope ?? {},
      parentId: null,
      mode,
    }, { rateLimited: true })
    await this.events.append(id, [
      event({
        type: 'session.started',
        sessionId: id,
        origin: session.origin,
        owner: session.owner,
        title,
        budgetUsd: session.budgetUsd,
        mode,
        ...(fallback ? { modeFallback: fallback } : {}),
      }),
      event({ type: 'session.status', sessionId: id, status: 'idle' }),
    ])
    const said = fallback ? { modeFallback: fallback } : {}
    if (!prompt) return { session, ...said }
    const turn = await this.send(id, principal, prompt, {
      ...(options.context ? { context: options.context } : {}),
      ...(options.images?.length ? { images: options.images } : {}),
      ...(options.tiers ? { tiers: options.tiers } : {}),
      ...(options.flow ? { flow: options.flow } : {}),
    })
    return { session: await this.get(id, principal), turn, ...said }
  }

  /**
   * A new chat that runs one agent() of a session's Workflow (background/workflowAgents.ts):
   * owned by `principal`, its parent recorded, in the parent's mode, and spending from the
   * parent's budget (its lineage, as a fork does, #823), so a workflow never makes budget.
   * It starts empty: the agent's task is its first turn.
   */
  async startChild(
    parentId: string,
    principal: Owner,
    options: { title: string; tags?: string[]; agent?: { model?: string | undefined; effort?: string | undefined; type?: string | undefined } },
  ): Promise<SessionRecord> {
    const parent = await this.get(parentId, principal)
    const id = randomUUID()
    const session = await this.insert(
      id,
      principal,
      { origin: parent.origin, title: options.title, tags: options.tags ?? [], scope: parent.scope, parentId, mode: parent.mode },
      { budgetOf: parent },
    )
    const agent = options.agent
    if (agent && (agent.model || agent.effort || agent.type)) {
      await this.deps.sql`
        UPDATE ai_sessions SET agent_model = ${agent.model ?? null}, agent_effort = ${agent.effort ?? null}, agent_type = ${agent.type ?? null}
         WHERE id = ${id}`
    }
    await this.events.append(id, [
      event({ type: 'session.started', sessionId: id, origin: session.origin, owner: session.owner, title: session.title, budgetUsd: session.budgetUsd, mode: session.mode }),
      event({ type: 'session.status', sessionId: id, status: 'idle' }),
    ])
    return session
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
    // Never unarchived by a send (#1885): unarchiving is the owner's explicit PATCH.
    if (before.archivedAt !== null) throw archivedError(id)
    const turnId = randomUUID()
    if (before.mode === 'durable') {
      const turn = await this.durableOrRefuse().send(before, {
        turnId,
        prompt,
        text: options.context ? `${prompt}\n\n${options.context}` : prompt,
        author: principal,
        images: options.images ?? [],
        systemAppend: systemAppend(),
        ...(options.tiers ? { tiers: options.tiers } : {}),
        ...(options.flow ? { flow: options.flow } : {}),
      })
      if (!turn) throw await this.whyNotClaimed(id, principal, before)
      return turn
    }
    const claim = await this.claimSession(
      id,
      `UPDATE ai_sessions
       SET status = 'running', turn_id = $2, lease_until = now() + ($5 * interval '1 millisecond'),
           interrupt_requested = false, updated_at = now()
       FROM prev
       WHERE id = $1 AND owner_kind = $3 AND owner_id = $4 AND status <> 'done' AND archived_at IS NULL
         AND ${POOL_COST} < ${POOL_BUDGET} AND (turn_id IS NULL OR lease_until <= now())
       RETURNING ${COLUMNS}, unpriced_cost_usd, agent_model, agent_effort, agent_type, ${CLAIMED_AT}, prev.prev_turn_id`,
      [id, turnId, principal.kind, principal.id, this.leaseMs],
    )
    if (!claim) throw await this.whyNotClaimed(id, principal, before)
    // The images and their previews are stored once the claim proves the sender owns the
    // session and may start a turn, so the turn's event can name them (#2479). A store
    // that fails gives the claim back and refuses the message. (A durable send does the
    // same under its own claim, durable.ts.)
    let sent: SentImage[] | undefined
    if (options.images?.length) {
      try {
        sent = await this.storeSentImages(id, options.images)
      } catch (err) {
        await this.releaseClaim(id, turnId)
        throw err
      }
    }
    return this.startTurn(claimed(claim), turnId, prompt, principal, {
      ...(options.context ? { context: options.context } : {}),
      ...(options.images?.length && sent ? { images: options.images, sent } : {}),
      ...(options.tiers ? { tiers: options.tiers } : {}),
    })
  }

  /**
   * Claims session `id` with `query`, an UPDATE that takes it when it is free or its
   * lease has run out (`$1` its id; it joins `FROM prev` and returns `prev.prev_turn_id`),
   * and reads in the same statement the turn it took over, if any (#3320). An expired lease is either reaped,
   * and reapExpired writes that turn's `turn` row, or taken here, and this writes it:
   * each lapsed turn gets exactly one, whichever replica ran it. The CTE locks the row
   * before the UPDATE runs (joined, so it is read first: one read only in RETURNING
   * would run after the UPDATE and find nothing to lock), so a reaper's UPDATE and this
   * one are ordered and only one sees the turn. One statement, not a transaction, so a
   * claim costs the pool one round trip (#2981 review).
   */
  private async claimSession(
    id: string,
    query: string,
    params: (string | number)[],
  ): Promise<ClaimRow | undefined> {
    // FOR UPDATE is what keeps a lapsed turn to one `turn` row, not a tuning: without it
    // the reaper's UPDATE and this one could both read the turn and both write its row.
    const [row] = await this.deps.sql.unsafe<(ClaimRow & { prev_turn_id: string | null })[]>(
      `WITH prev AS (SELECT turn_id::text AS prev_turn_id FROM ai_sessions WHERE id = $1 FOR UPDATE)
       ${query}`,
      params,
    )
    const claim: ClaimRow | undefined = row
    const taken = row?.prev_turn_id ?? undefined
    if (claim && taken) {
      // Not awaited: the send it is on would wait for it. record() settles on its own,
      // and reports a write that never lands (audit/log.ts).
      void this.deps.audit?.record({
        kind: 'turn',
        action: 'lost_claim',
        surface: 'harness',
        actor: { kind: claim.owner_kind, id: claim.owner_id, label: claim.owner_label },
        sessionId: id,
        turnId: taken,
        outcome: 'error',
        detail: TAKEN_OVER,
      })
    }
    return claim
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
    const claim = await this.claimSession(
      approval.sessionId,
      `UPDATE ai_sessions
       SET status = 'running', turn_id = $2, lease_until = now() + ($3 * interval '1 millisecond'),
           interrupt_requested = false, updated_at = now()
       FROM prev
       WHERE id = $1 AND status <> 'done' AND archived_at IS NULL AND ${POOL_COST} < ${POOL_BUDGET}
         AND (turn_id IS NULL OR lease_until <= now())
       RETURNING ${COLUMNS}, unpriced_cost_usd, agent_model, agent_effort, agent_type, ${CLAIMED_AT}, prev.prev_turn_id`,
      [approval.sessionId, turnId, this.leaseMs],
    )
    if (!claim) {
      return { resumed: false, reason: 'it is running another turn, is done or archived, or has spent its budget' }
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
      const tiers = await this.resumeTiers(approval, record(claim))
      await this.startTurn(claimed(claim), turnId, prompt, by, { keepResumeTurn: turnId, ...(tiers ? { tiers } : {}) })
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

  /** The durable dispatch, or `unavailable` when this service has none (no Temporal or no key). */
  private durableOrRefuse(): DurableTurns {
    if (this.durableTurns) return this.durableTurns
    throw new SessionError(
      'unavailable',
      'this session is durable, and this agent service has no Temporal or secret key to run it; try again once it has',
    )
  }

  /** Tells a durable session's workflow it was marked done (#1056), or refuses `unavailable`. */
  private async endDurable(id: string): Promise<void> {
    if (!this.durable) {
      throw new SessionError('unavailable', 'this session is durable, and this agent service has no Temporal to end it; try again once it has')
    }
    try {
      await this.durable.end(id)
    } catch (err) {
      if (err instanceof DurableUnavailable) throw new SessionError('unavailable', err.message)
      throw err
    }
  }

  /** Gives back a claim no turn ran on (a resume that could not start). */
  private async releaseClaim(id: string, turnId: string): Promise<void> {
    const released = await this.deps.sql`
      UPDATE ai_sessions SET status = 'idle', turn_id = NULL, lease_until = NULL, updated_at = now()
      WHERE id = ${id} AND turn_id = ${turnId}`
    if (released.count > 0) await this.events.append(id, [event({ type: 'session.status', sessionId: id, status: 'idle' })])
  }

  /**
   * Stores a turn's images and their previews with the session (blobs.ts), as a
   * tool result's are, and answers what the `user.turn` event names them by
   * (#2479): the panel loads both by name, from any tab.
   */
  private async storeSentImages(id: string, images: readonly UserImage[]): Promise<SentImage[]> {
    const { blobs, sent } = sentImageBlobs(images)
    try {
      await this.blobs.put(id, blobs)
    } catch (err) {
      this.deps.stderr?.(`session ${id}: could not store a turn's images: ${err instanceof Error ? err.message : String(err)}\n`)
      throw new SessionError('unavailable', `the images for session ${id} could not be stored; send it again in a moment`)
    }
    return sent
  }

  private async startTurn(
    session: ClaimedSession,
    turnId: string,
    prompt: string,
    author: Owner,
    options: {
      keepResumeTurn?: string
      context?: string
      images?: readonly UserImage[]
      /** What the `user.turn` event names `images` by, stored already (`storeSentImages`). */
      sent?: readonly SentImage[]
      tiers?: readonly Tier[]
    } = {},
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
      event({
        type: 'user.turn',
        sessionId: id,
        turnId,
        text: prompt,
        author,
        ...(options.sent?.length ? { images: [...options.sent] } : {}),
      }),
      event({ type: 'session.status', sessionId: id, status: 'running' }),
    ])
    const controller = new AbortController()
    const local: LocalTurn = { controller, turnId, settling: false, lost: false }
    this.active.set(id, local)
    const text = options.context ? `${prompt}\n\n${options.context}` : prompt
    const query = options.images?.length ? userPrompt(text, options.images) : text
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
    if (now.archivedAt !== null) return archivedError(id)
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
    session: ClaimedSession,
    turnId: string,
    prompt: string | AsyncIterable<SDKUserMessage>,
    local: LocalTurn,
    /** The user's words without the page context: what memory is recalled against. */
    userText: string,
    principal: TurnPrincipal,
  ): Promise<TurnOutcome> {
    const { controller } = local
    const id = session.id
    const sql = this.deps.sql
    // The turn on the session's warm process (turnCore.ts), under this row's lease.
    const core = new CoreTurn(this.turnCore, {
      session,
      turnId,
      principal,
      userText,
      signal: controller.signal,
      interrupt: () => controller.abort(new Error('the turn was interrupted')),
      onSettling: () => {
        local.settling = true
      },
      gates: ({ turn, secrets, trace, shown }) => {
        const approvalGate = this.approvals.gate({
          sessionId: id,
          turnId,
          requestedBy: session.owner,
          ...(principal.tiers ? { requestedTiers: principal.tiers } : {}),
          secrets,
          signal: controller.signal,
          trace,
          shown,
        })
        // AskUserQuestion (#940): the run has a question gate only in a session
        // the browser user owns (TurnRunner), and this turn's answers it.
        const questionGate = turn.run.questionGate
          ? this.questions.gate({
              sessionId: id,
              turnId,
              secrets,
              signal: controller.signal,
              // An attention request's `stop`/`wait` timer (#815) ends the turn as an interrupt does.
              stopTurn: (why) => controller.abort(new AttentionStop(why)),
              // The claim set updated_at = now(): the turn's start by the database's clock.
              turnStartedAt: session.claimedAt,
            })
          : undefined
        return {
          approvalGate,
          approvalFor: (toolUseId) => this.approvals.idForToolUse(id, toolUseId),
          ...(questionGate
            ? {
                questionGate,
                waitForTab: waitForTab(questionGate, controller.signal, (questionId) => this.questions.reconnected(id, { questionId })),
              }
            : {}),
        }
      },
    })
    let result: SDKResultMessage | undefined
    let outcome: TurnOutcome = { kind: 'failed', message: 'the turn could not finish' }
    try {
      // Lease renewal, and the interrupt flag from other replicas. A database blip
      // keeps the turn running: the lease covers several misses (renewLoop logs them).
      const renewal = renewLoop(
        async () => {
          const rows = await sql<{ interrupt_requested: boolean }[]>`
            UPDATE ai_sessions SET lease_until = now() + (${this.leaseMs} * interval '1 millisecond')
            WHERE id = ${id} AND turn_id = ${turnId}
            RETURNING interrupt_requested`
          if (rows.length === 0) {
            local.lost = true
            controller.abort(new Error('lost the turn claim'))
          } else if (rows[0]?.interrupt_requested) {
            controller.abort(new Error('the turn was interrupted'))
          }
        },
        this.renewMs,
        (line) => this.deps.stderr?.(`session ${id} turn ${turnId}: ${line}\n`),
      )
      let run: Awaited<ReturnType<CoreTurn['run']>>
      try {
        run = await core.run(prompt)
      } finally {
        await renewal.stop()
      }
      result = run.result
      // The entry was settled on the result, after the SDK's last transcript
      // append of the turn (warm.ts `settle` waits for the mirror; measured:
      // `last-prompt` and `cost-state` entries arrive after the `result`
      // message), so releasing the claim here means the next turn, on any
      // replica, resumes from a complete transcript.
      const stopped = controller.signal.aborted ? abortMessage(controller.signal) : undefined
      const turnUsd = run.spentUsd
      // Only a turn that did not end on its own can be cut off mid-request;
      // one that did has every request priced in its result. WebFetch's own
      // model calls are in no result, so they are added the same way.
      const cutUsd = (stopped !== undefined || !result ? run.cutUsd : 0) + run.nestedUsd
      if (local.lost) {
        outcome = { kind: 'lost_claim' }
        // Another turn owns the session now, wherever it runs: this process is not kept for it.
        // Before the charge, which needs the database a lost claim may have lost too (#3849 review).
        for (const entry of run.served) void this.warm.abandon(entry, 'lost the turn claim')
        await this.chargeLostTurn(session.id, turnId, result, turnUsd, cutUsd, core.traced)
        return outcome
      }
      // A turn stopped after its refusal was reported is still the caller's.
      outcome = await this.finish(
        session,
        turnId,
        stopped,
        stopped === undefined ? undefined : interruptedMessage(controller.signal),
        result,
        result && stopped === undefined ? run.refused : undefined,
        run.failure,
        run.secrets,
        turnUsd,
        cutUsd,
        core.traced,
      )
      // A claim that went after the lost check above: its process is not kept either (#2981 review).
      if (outcome.kind === 'lost_claim') for (const entry of run.served) void this.warm.abandon(entry, 'lost the turn claim')
      return outcome
    } catch (err) {
      core.traced.fail(err)
      throw err
    } finally {
      core.end(outcome, result)
    }
  }

  /**
   * `stopped` is why the turn was aborted (the abort reason: an interrupt, or
   * SHUTTING_DOWN), when it was.
   */
  private async finish(
    session: ClaimedSession,
    turnId: string,
    stopped: string | undefined,
    /** The `interrupted` error's message when `stopped`: an attention timer's says so (#1206). */
    interrupted: string | undefined,
    result: SDKResultMessage | undefined,
    /** The failed model request `result` reports, when it reports one (fallback.ts `onRefused`). */
    refused: Refused | undefined,
    failure: string | undefined,
    secrets: readonly string[],
    /**
     * What the turn's attempts spent as their results priced it (warmTurn.ts
     * `spentUsd`): each the difference of its process's cumulative
     * `total_cost_usd` before and after (spec §6.6; warm.ts `turnCostUsd`), so
     * a resumed process's carried history and the session's unpriced spend
     * (#991), which no total holds, are neither counted again nor lost.
     */
    turnUsd: number,
    /**
     * What the turn spent that no result prices: the request it was cut off in (#991,
     * unpricedSpend.ts), and its WebFetch's own model calls, in every outcome (#2684).
     */
    cutUsd: number,
    /** The turn's trace: a lost turn's spend that could not be recorded goes on it. */
    trace: TurnTrace,
  ): Promise<TurnOutcome> {
    const id = session.id
    // An approval still pending here belongs to a call that was waiting when
    // the turn was aborted: only an abort ends a wait (approvals/service.ts).
    // A shutdown keeps it, and the session waiting on it, for after the
    // restart; anything else (an interrupt) cancels it. Measured on SDK
    // 0.3.283 and 0.3.287 (test/approvals.e2e.test.ts): aborting a query whose canUseTool
    // is pending fails that call ("Tool permission request failed: AbortError:
    // Tool permission stream closed before response received"), and Claude
    // Code may still reach the model and end with a `result` before it exits;
    // so the harness interrupts first (harness/run.ts stopFirst, #1168), which
    // ends the turn with no model call. The tool never runs.
    // Approved-but-unused approvals end with the turn in every case,
    // including the one a resumed turn was bound to and did not use.
    // A question never outlives its turn (questions/service.ts), shutdown or not;
    // a posted `done` summary does, and cancelPending leaves it pending.
    // Only this turn's: if its claim was lost, a newer turn's question is not ours to cancel.
    await this.questions.cancelPending(id, stopped ?? 'the turn ended', { refresh: false, turnId })
    // The approvals are the session's, not this turn's: settled only while this turn still
    // holds the claim. One the reaper or a send took after runTurn's lost check (#2981
    // review) leaves them to the turn that has it; the release below would fail anyway.
    // A claim taken between this read and the cancel below can still cost that newer
    // turn its approvals: a window of one query, accepted. Holding the row's lock across
    // the cancel would serialise it with every claim, and the cancel runs on its own
    // connection, so a lock taken here could wait on itself.
    const [held] = await this.deps.sql`SELECT 1 FROM ai_sessions WHERE id = ${id} AND turn_id = ${turnId}`
    if (!held) {
      await this.chargeLostTurn(id, turnId, result, turnUsd, cutUsd, trace)
      return { kind: 'lost_claim' }
    }
    const keepWaiting = stopped === SHUTTING_DOWN && (await this.approvals.hasPending(id))
    if (stopped !== SHUTTING_DOWN) {
      await this.approvals.cancelPending(id, stopped ?? 'the turn ended', { refresh: false })
    } else {
      await this.approvals.revokeUnused(id, 'the turn ended')
    }
    const tally = tallyTurn(session, { stopped, interrupted, result, refused, failure, secrets, turnUsd, cutUsd })
    let status: SessionStatus = tally.status
    const outcome = tally.outcome
    const { costUsd, turns } = tally
    const tail = tally.tail.slice(0, -1)
    if (keepWaiting) status = 'waiting_approval'
    tail.push(event({ type: 'session.status', sessionId: id, status }))

    // Added, not set: a turn that lost this session's claim earlier may have added its
    // spend since this one read the row (chargeLostTurn).
    const [released] = await this.deps.sql<{ id: string }[]>`
      UPDATE ai_sessions
      SET status = ${status}, cost_usd = cost_usd + ${costUsd - session.ownCostUsd}, turns = ${turns},
          unpriced_cost_usd = unpriced_cost_usd + ${cutUsd},
          turn_id = NULL, lease_until = NULL, interrupt_requested = false, updated_at = now()
      WHERE id = ${id} AND turn_id = ${turnId}
      RETURNING id`
    if (!released) {
      await this.chargeLostTurn(id, turnId, result, turnUsd, cutUsd, trace)
      return { kind: 'lost_claim' }
    }
    // #1922: what the turn cost, as its own audit row (audit/turn.ts turnCostEntry).
    await this.deps.audit?.record(
      turnCostEntry(
        { sessionId: id, turnId, actor: session.owner },
        outcome,
        { costUsd: costUsd - session.ownCostUsd, priced: result !== undefined, estimatedUsd: cutUsd },
        secrets,
      ),
    )
    // Read again, not from `session`: the user may have raised the budget while the turn
    // ran, and a fork or the parent sharing it may have spent from it (#823). The meter
    // shows what the whole lineage has spent, which is what the budget is checked against.
    const after = await this.row(id)
    const budgetUsd = after?.budgetUsd ?? session.budgetUsd
    const spent = after?.costUsd ?? costUsd
    const worded = wordTail(tail, { spentUsd: spent, budgetUsd })
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
    if (session.mode === 'durable') {
      return this.durableOrRefuse().interrupt(session, `interrupted by ${publicLabel(principal)}`)
    }
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
    if (session.archivedAt !== null) throw archivedError(id)
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
    if (session.archivedAt !== null) throw archivedError(id)
    if (!session.offer || !sameOwner(actor, session.offer.to)) {
      throw new SessionError('invalid', `session ${id} is not offered to you`)
    }
    return this.transfer(session, actor, { accepting: true })
  }

  /**
   * Ends the live offer of a session: its owner withdraws it, its target
   * declines it. Resolves false when there was none. The browser user is only
   * ever an owner here, since a handoff to it transfers at once and is never
   * offered: declining by the target serves MCP principals (`sessions_cancel_handoff`).
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
    // Refused before anything changes when this service cannot reach a durable session's workflow.
    const durable = session.mode === 'durable' && !sameOwner(session.owner, to) ? this.durableOrRefuse() : undefined
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
    // A durable session's parked calls were asked for the previous owner's turn: ended
    // only once the owner change above applied, with no lock held, best-effort (the
    // interrupt Signal when cancel_input goes unanswered).
    await durable?.handoff(session, `the session was handed off to ${publicLabel(to)}`, to)
    await this.events.append(id, [event({ type: 'session.owner', sessionId: id, owner: to })])
    await this.approvals.cancelPending(id, `the session was handed off to ${publicLabel(to)}`)
    await this.questions.cancelPending(id, `the session was handed off to ${publicLabel(to)}`)
    // Its warm process was spawned for the previous owner (the owner is in the
    // fingerprint, and its question gate is the browser user's): gone here and
    // on every other replica (spec 2026-10-13 §5.3, warmEvict.ts).
    void this.warm.evict(id, 'handed off')
    await notifyWarmEvict(this.deps.sql, id)
    return this.get(id, to)
  }

  /**
   * Announces a change to a session's offer as `session.owner` on the bus
   * (busEvents.ts), so a watcher of the session or of the list re-reads it. An
   * offer is state on the session row, not a transcript event (the panel reads it
   * from `sessions.snapshot`, #1284), so nothing is appended.
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
   *
   * Only the browser user's "continue in a new chat" (`freshBudget`, the HTTP
   * route) gives the child a budget of its own. Like `raiseBudget`, that is
   * enforced here: `freshBudget` from any other principal is ignored (#1447). Any other fork, `sessions_fork`
   * above all, spends from the parent's budget: nothing is copied or moved, the
   * child joins the parent's lineage (`budget_root_id`), and a turn in either
   * debits the one budget. Forking therefore never creates budget, and cannot
   * stand in for the user-only `raiseBudget` (#823).
   *
   * `upTo` (#793) is a reply's panel message id: the child keeps the conversation
   * through that reply and nothing after it (forkPoint.ts). With `audit`, the fork
   * is a `resource` row on the child (action `session_fork`).
   */
  async fork(
    id: string,
    principal: Owner,
    options: {
      title?: string
      origin?: Origin
      rateLimited?: boolean
      freshBudget?: boolean
      upTo?: string
      audit?: Omit<AuditContext, 'actor'>
    } = {},
  ): Promise<SessionRecord> {
    const startedAt = new Date()
    const fresh = options.freshBudget === true && principal.kind === 'browser'
    const parent = await this.get(id, principal)
    // A durable parent's conversation is its workflow's (#2688), read before anything is written.
    const durable = parent.mode === 'durable' ? await this.durableForkPoint(parent, options.upTo) : undefined
    let classic: { transcript: SessionStoreEntry[]; upToMessageId: string | undefined } | undefined
    let history: ServerEvent[] | undefined
    if (durable) {
      history = durable.history
    } else {
      const transcript = await this.store.load({ projectKey: '', sessionId: id })
      if (!transcript) {
        throw new SessionError('no_transcript', `session ${id} has no transcript to fork yet; send it a turn first`)
      }
      const upToMessageId = options.upTo === undefined ? undefined : transcriptCut(transcript, options.upTo)
      history = await forkHistory(this.events, id, options.upTo)
      if (!history || (options.upTo !== undefined && upToMessageId === undefined)) {
        throw new SessionError('invalid', `session ${id} has no finished reply ${options.upTo} to fork from`)
      }
      classic = { transcript, upToMessageId }
    }
    // A fork of a spent lineage could not run a turn; say why now rather than at its first send.
    if (!fresh && parent.costUsd >= parent.budgetUsd) {
      throw new SessionError(
        'budget_exhausted',
        `session ${id} has spent its budget (${usd(parent.costUsd)} of ${usd(parent.budgetUsd)}); ` +
          'only you can continue it in a new chat or raise its budget, in the ScadBuddy UI',
        { costUsd: parent.costUsd, budgetUsd: parent.budgetUsd },
      )
    }
    const title = options.title?.trim() || `${parent.title || 'session'} (fork)`
    // A fork is a new session and counts against the same limit as a start (PR #715
    // review). Checked first too, so a refused fork leaves no SDK copy behind; the
    // insert's check under the lock is the one that holds.
    await this.withinNewSessionLimit(principal, this.deps.sql)
    const fields = { origin: options.origin ?? parent.origin, title, tags: parent.tags, scope: parent.scope, parentId: parent.id }
    const insertOptions = { rateLimited: options.rateLimited ?? true, ...(fresh ? {} : { budgetOf: parent }) }
    let child: SessionRecord
    if (durable) {
      // A new durable session whose workflow starts with a copy of the parent's
      // conversation as it stood at the fork point, under the parent's Claude session
      // (only ever resumed inside each workflow's own segments, never shared).
      child = await this.insert(randomUUID(), principal, { ...fields, mode: 'durable' }, insertOptions)
      try {
        await durable.turns.startFork(child, durable.agent)
      } catch (err) {
        // Nothing else of the child is written yet: it goes, rather than run empty.
        await this.deps.sql`DELETE FROM ai_sessions WHERE id = ${child.id}`
        throw err
      }
    } else {
      const { sessionId: forkedId } = await sdkForkSession(id, {
        sessionStore: this.store,
        dir: sessionWorkDir(this.deps.paths, id),
        title,
        ...(classic?.upToMessageId === undefined ? {} : { upToMessageId: classic.upToMessageId }),
      })
      // A classic fork copies the SDK transcript, which only a classic session has.
      child = await this.insert(forkedId, principal, { ...fields, mode: 'classic' }, insertOptions)
    }
    const childId = child.id
    // The images the copied events name (#782, #2150), before the events that name them: the
    // parent's others (a later turn's, past `upTo`) stay the parent's, so a fork chain does not
    // copy every image of every ancestor.
    await this.blobs.copy(id, childId, blobNames(history))
    // Its workspace files (#2329), so the fork carries on with what the parent had.
    await this.files.copy(id, childId)
    await this.events.append(childId, [
      event({
        type: 'session.started',
        sessionId: childId,
        origin: child.origin,
        owner: child.owner,
        title,
        budgetUsd: child.budgetUsd,
        mode: child.mode,
      }),
      // The conversation, re-addressed to the child.
      ...history.map((e) => ({ ...e, sessionId: childId })),
      event({ type: 'session.status', sessionId: childId, status: 'idle' }),
    ])
    // Recorded once the child is complete (#2159): a fork that failed after the insert has only
    // the failures-only mount's `error` row (app.ts), never an `ok` one beside it.
    if (options.audit) {
      await this.deps.audit?.record({
        kind: 'resource',
        action: 'session_fork',
        surface: options.audit.surface,
        actor: principal,
        clientIp: options.audit.clientIp,
        sessionId: childId,
        outcome: 'ok',
        detail: `forked from ${id}${options.upTo === undefined ? '' : ` up to ${options.upTo}`}`,
        startedAt,
        finishedAt: new Date(),
      })
    }
    return child
  }

  /**
   * Where a durable session forks (#2688): its workflow's committed conversation through
   * `upTo`'s reply, or all of it. A parent whose turn is running forks at its last
   * finished reply, as the conversation past it is not committed yet. One whose committed
   * state ends at a paused call (#4173: a stopped turn's last segment never committed)
   * forks there when its workflow holds the call's result: the child takes it with the
   * checkpoint and delivers it first, and its history runs through the call. Without the
   * result (a worker from before it was sent) it forks at its last finished reply before
   * the call, with its history through that reply. A cut never leaves a call unanswered.
   */
  private async durableForkPoint(
    parent: SessionRecord,
    upTo: string | undefined,
  ): Promise<{ turns: DurableTurns; agent: ForkedAgent; history: ServerEvent[] }> {
    const id = parent.id
    const turns = this.durableOrRefuse()
    const source = await turns.forkSource(id)
    const all = source ? await forkHistory(this.events, id) : undefined
    if (!source || !all) {
      throw new SessionError('no_transcript', `session ${id} has no conversation to fork yet; send it a turn first`)
    }
    const replies = durableReplies(source.entries, all)
    const through = (uuid: string | undefined) => (uuid === undefined ? undefined : entriesThrough(source.entries, uuid))
    // At the checkpoint, a call whose result the workflow holds is answered: the child delivers it.
    const answered = (uuid: string | undefined, owed: Record<string, unknown> = {}) => {
      const prefix = through(uuid)
      return prefix && unansweredCalls(prefix).every((call) => call in owed) ? prefix : undefined
    }
    const paused = unansweredCalls(through(source.checkpoint) ?? [])
    // The child's history through the paused calls; undefined when the log lacks one of them,
    // and then the parent forks before them, as without their results.
    const pausedHistory = historyThroughCalls(all, paused)
    // The conversation through the checkpoint, when the workflow holds every result it lacks.
    const atCheckpoint = answered(source.checkpoint, source.owed.pending)
    let at = upTo
    if (at === undefined && (source.busy || !pausedHistory || !atCheckpoint)) {
      at = [...replies.keys()].reverse().find((reply) => answered(replies.get(reply)))
      if (at === undefined) {
        let reason = `session ${id} has no finished reply before the call it is waiting on; fork it once that call is answered`
        if (source.busy) reason = `session ${id} has no finished reply to fork yet; wait for its turn to answer`
        else if (!pausedHistory && atCheckpoint) {
          // The result is held, but the log has no record of the call to end the child's history at.
          reason = `session ${id} has no finished reply before the call it is waiting on, and its log has no record of that call to fork at; fork it once the session has resumed past that call`
        }
        throw new SessionError('no_transcript', reason)
      }
    }
    const entries = at === undefined ? atCheckpoint : answered(replies.get(at))
    if (!entries && through(at === undefined ? source.checkpoint : replies.get(at))) {
      throw new SessionError('invalid', `session ${id}'s conversation through reply ${at} leaves a call unanswered; fork from an earlier reply`)
    }
    const history = at === undefined ? pausedHistory : await forkHistory(this.events, id, at)
    if (!entries || !history) {
      throw new SessionError('invalid', `session ${id} has no finished reply ${upTo} to fork from`)
    }
    const checkpoint = entries.at(-1)!.uuid!
    // At the parent's checkpoint the child takes what the parent still owes there (#4173):
    // a turn stopped at a paused call left its result for the next segment, which the
    // child's first segment must deliver. A reply before it owes nothing; the calls that
    // already ran never run again either way. An `upTo` whose reply is the checkpoint
    // takes it too: it is the same state, and a prefix with no unanswered call owes no
    // result, only `fork_next` (a stopped turn's: the next segment runs in a copy).
    const owed = checkpoint === source.checkpoint ? source.owed : { pending: {}, fork_next: false, recent_call_ids: source.owed.recent_call_ids }
    return {
      turns,
      agent: { session_id: source.claudeSessionId, checkpoint, conversation: entries.map((e) => JSON.stringify(e)), ...owed },
      history,
    }
  }

  /**
   * Adds `addUsd` to the budget a session spends from (#790): its own, or for a
   * fork its lineage's, which the parent and every other fork of it share
   * (#823). User-only and owner-only:
   * it spends money, so only the browser user may raise it, and only on a
   * session it owns (take one over first). No tool calls this; the one caller is the HTTP route
   * (routes/sessions.ts). A raise is a `settings` audit row on the session
   * (action `session_budget_usd`); a refused or failed one is recorded by
   * app.ts's `auditWrites` from the route's status, like other refused writes.
   */
  async raiseBudget(
    id: string,
    principal: Owner,
    addUsd: number,
    context: Omit<AuditContext, 'actor'>,
  ): Promise<SessionRecord> {
    const startedAt = new Date()
    if (principal.kind !== 'browser') {
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
    // One statement on the lineage's root, so two raises add up and the owner cannot change
    // in between; the owner checked is this session's, which need not be the root's.
    const [hit] = await this.deps.sql.unsafe<{ id: string; budget_usd: number }[]>(
      `UPDATE ai_sessions r SET budget_usd = round((r.budget_usd + $2)::numeric, 2)::double precision, updated_at = now()
         FROM ai_sessions s
        WHERE s.id = $1 AND s.owner_kind = $3 AND s.owner_id = $4 AND r.id = coalesce(s.budget_root_id, s.id)
          AND round((r.budget_usd + $2)::numeric, 2) <= $5
       RETURNING s.id, r.budget_usd`,
      [id, add, principal.kind, principal.id, MAX_SESSION_BUDGET_USD],
    )
    const row = hit ? await this.row(id) : undefined
    if (!hit || !row) {
      const now = (await this.row(id)) ?? session
      throw sameOwner(principal, now.owner)
        ? new SessionError(
            'invalid',
            `a chat's budget can be at most ${usd(MAX_SESSION_BUDGET_USD)}; this one is ${usd(now.budgetUsd)}`,
          )
        : new SessionError('busy', `session ${id} changed owner meanwhile; try again`)
    }
    const raised = row
    // The before and after this raise made, from its own UPDATE: the read above may
    // already show a later raise (#1650).
    const after = hit.budget_usd
    await this.deps.audit?.record({
      kind: 'settings',
      action: 'session_budget_usd',
      surface: context.surface,
      actor: principal,
      clientIp: context.clientIp,
      sessionId: id,
      outcome: 'ok',
      detail: `${usd(after - add)} + ${usd(add)} = ${usd(after)} (${usd(raised.costUsd)} spent)`,
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
  async stopTurns({ graceMs, abortWaitMs, others }: { graceMs: number; abortWaitMs: number; others?: Promise<void> }): Promise<void> {
    this.drain()
    const ended = () => this.settled()
    const within = (ms: number, work: Promise<unknown>) =>
      Promise.race([work, new Promise((resolve) => setTimeout(resolve, ms).unref())])
    await within(graceMs, ended())
    if (this.active.size > 0) {
      this.abortAll()
      await within(abortWaitMs, ended())
    }
    // The other turns on these warm processes (an AgentSession's, TurnHost.stop), which keep
    // their own grace and wait: their processes are not closed under them.
    await others
    // The warm processes go with the pod (spec 2026-10-13 §5.3 "shutdown"): the
    // idle ones, and a stopped turn's that is still winding down past its wait.
    await this.warm.close()
  }

  /**
   * What a turn that lost its claim spent, added to its session all the same (#2981
   * review): the requests were billed whoever owns the session now. Counted as finish()
   * counts it, without the claim, and added to the row rather than set, since the turn
   * that took it over may be writing its own.
   *
   * Deliberately no `turnCostEntry` row and no `session.result` event (#3849 review): a
   * lapsed turn has exactly one `turn` row, the reaper's or the claim's (#3320), so its
   * spend shows in the session's total only; and the session's events are the newer
   * turn's now, whose own `session.result` carries the total with this in it. A failed
   * write is logged, not thrown: the claim is often lost because the database was.
   */
  private async chargeLostTurn(
    id: string,
    turnId: string,
    result: SDKResultMessage | undefined,
    turnUsd: number,
    cutUsd: number,
    trace: TurnTrace,
  ): Promise<void> {
    const usd = (result ? turnUsd : 0) + cutUsd
    if (usd <= 0) return
    try {
      await this.deps.sql`
        UPDATE ai_sessions SET cost_usd = cost_usd + ${usd}, unpriced_cost_usd = unpriced_cost_usd + ${cutUsd}
        WHERE id = ${id}`
    } catch (err) {
      this.deps.stderr?.(`session ${id} turn ${turnId}: its spend of ${usd} USD was not recorded: ${describe(err)}\n`)
      trace.unrecordedSpend(usd)
    }
  }

  /**
   * Ends the turns whose lease ran out. Usually their process died before
   * finish() (a SIGKILL, an OOM, a pool closed under them), so nothing else
   * would say the turn stopped. A send could already take such a session over
   * (its claim accepts an expired lease), but until then its row said `running`
   * and its log had no end. A live turn renews its lease every `renewMs`, so an
   * expired one has lost its claim, whether or not it is still running: one
   * still running on this replica (its renewals stalled, #2375) is stopped
   * here too, as lost, so it never runs finish() over a newer turn. One UPDATE, so two replicas' reapers never both
   * take the same row. The session goes to `waiting_approval` when an
   * approval is still pending (a shutdown keeps those for after the restart,
   * #258), otherwise `idle`; approved-but-unused approvals of the dead turn
   * are revoked, as finish() does on a shutdown. Returns the sessions reaped.
   */
  async reapExpired(): Promise<string[]> {
    const rows = await this.deps.sql<
      { id: string; status: SessionStatus; dead_turn: string; owner_kind: string; owner_id: string; owner_label: string }[]
    >`
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
      RETURNING s.id, s.status, dead.turn_id AS dead_turn, s.owner_kind, s.owner_id, s.owner_label`
    for (const { id, status, dead_turn, owner_kind, owner_id, owner_label } of rows) {
      // Its cause, as far as this replica can tell (#2375). Only a turn on this one is
      // known to be alive: its renewals stopped landing. Any other's replica stopped,
      // or stopped reaching the database; which, this one cannot see.
      const local = this.active.get(id)
      const here = local?.turnId === dead_turn ? local : undefined
      const message = here
        ? `the turn was stopped: it could not renew its claim on the session for ${leaseWords(this.leaseMs)}, though its replica is running`
        : 'the turn was stopped: its claim on the session lapsed (the replica running it stopped, or could not reach the database)'
      if (here) {
        // Lost before aborted: its runTurn then skips finish(), which would cancel the
        // approvals of a turn sent since. The two audits split the rows: the local
        // turn's auditor.finish closes only its open `tool_call` rows, with this
        // message; the reaper writes the one `turn` row, below. So the lost exit in
        // runTurn writes no `turn` row of its own: a turn whose session a new turn took
        // before any reap gets its row from that claim (claimSession, #3320).
        here.lost = true
        here.controller.abort(new Error(message))
      }
      await this.approvals.revokeUnused(id, 'the turn ended')
      // The dead turn's questions only (questions/service.ts).
      await this.questions.cancelPending(id, 'the turn ended', { refresh: false, turnId: dead_turn })
      await this.events.append(id, [
        event({
          type: 'error',
          sessionId: id,
          code: 'interrupted',
          message,
        }),
        event({ type: 'session.status', sessionId: id, status }),
      ])
      // Its `turn` row, as any other ending has (#2375); its own finish never ran. No
      // cost on it: the reaper never saw the requests it made. A turn on this replica
      // adds its own spend to the session when it stops (chargeLostTurn).
      await this.deps.audit?.record({
        kind: 'turn',
        action: 'lost_claim',
        surface: 'harness',
        actor: { kind: owner_kind, id: owner_id, label: owner_label },
        sessionId: id,
        turnId: dead_turn,
        outcome: 'error',
        detail: message,
      })
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

