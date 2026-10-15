import {
  ActivityFailure,
  ApplicationFailure,
  condition,
  isCancellation,
  proxyActivities,
  setHandler,
  defineQuery,
  defineUpdate,
  workflowInfo,
  TemporalFailure,
} from '@temporalio/workflow'
import type { Duration } from '@temporalio/common'
import { ASK_USER, buildEntry, DEFAULT_TIMEOUT_S, MalformedAnswerInputError } from '../../gate/entry.js'
import { durableRequestId } from '../../gate/ids.js'
import { CANCEL_INPUT_UPDATE, GATE_REFUSED, PENDING_INPUT_QUERY, RESPOND_UPDATE } from '../../gate/names.js'
import type { DenyInput, GrantInput, OpenInput, ResolveInput, Responder } from '../../gate/store.js'
import { type GateEntry, type RefusalCode, RespondRefusal, type RespondRequest, type Role, validateRespond } from '../../gate/validate.js'
import { isUuid } from '../../harness/uuid.js'
import type { InputEntry, InputOutcome, Owner } from '../../sessions/protocol.js'
import { TASK_QUEUE } from '../names.js'
import { FLOW_GRANT_CHECK_ACTIVITY, SESSION_GATE_ACTIVITIES } from './names.js'

// The session workflow's half of the tool-call gate (spec 2026-10-01 §6.6, phase 4 of the
// unified runtime), ported whole from agent-durable's session/workflow.py, whose names it
// keeps: `_watch_gate` -> watch, `_park_or_refuse` -> parkOrRefuse, `_park` -> open,
// `_resolve` -> resolve, `_cancel_parked` -> cancelAll, `_settle_parked` -> settle, the
// `respond` and `cancel_input` Updates and the `pending_input` Query -> install.
//
// What hangs off the Claude agent itself (`self._agent.decide`, `validate_decision`, the
// turn's refusals, who the turn is for) is the GateHost this is given: phase 5's
// AgentSession wires it to the turn.
//
// When a call parks, and how it ends:
//   - a turn tool whose call is not outward (its tier, asked of the agent) runs at once;
//   - an approval is described by the agent (summary and input hash), then, in a flow
//     turn, checked against the run's grant (`flow_grant_check`): inside it, `grant_input`
//     records it and no entry is made; outside it, with `on_outside: 'deny'`, `deny_input`
//     refuses it as the flow; else it parks in the run's queue (a hop at the starter
//     session first when the verdict names one; one timer covers both hops);
//   - an answer's card is built and checked here (gate/entry.ts), and a malformed one is
//     refused to the model;
//   - a parked call ends by `respond`, by its timer (approval -> expired, ask_user ->
//     cancelled, any other -> timed out), by `cancel_input` or by the turn's end, each
//     through `resolve_input` before the host is told.
// The workflow patches (`workflow.patched`) of the Python one are not carried as patches:
// this is a new workflow type, and no older history takes the paths they guard. Each
// patch's new side is the one ported (PERSON_KEEPS_TIMEOUT, SETTLE_PARKED,
// SESSION_FLOW_GRANT, SESSION_FLOW_ESCALATION), so a reader can check each against it.

/** An entry's timer at most (ai_pending_input's CHECK). */
export const ENTRY_MAX_S = 86_400
export const NOBODY_ANSWERED = 'nobody answered in time'
/** How long a flow turn's call waits for the run grant's verdict before it parks. */
const GRANT_CHECK_WAIT = '1m'
const PROJECTS_QUEUE = 'projects'
const SHORT = '30s'
const SYSTEM: Responder = { kind: 'system', id: 'scadbuddy', label: 'ScadBuddy' }

/** The timers of the session's entries (ai_settings), read when a turn starts. */
export type GateSettings = { approvalExpiryS: number; questionExpiryS: number; toolTiers?: Record<string, string>; disabledTools?: string[] }

/** A call the agent has parked: its tool_use id, the tool and the model's input. */
export type GateCall = { id: string; name: string; input: unknown }

/** Who the running turn is for, and the flow run that sent it (#2481). */
export type GateTurn = { author: Owner; flow?: { runId: string; callId: string } | undefined }

/** How a parked call ended, as the entry recorded it. */
export type Resolution =
  | { kind: 'approval'; outcome: 'approved' | 'denied' | 'expired' | 'cancelled' }
  | { kind: 'answer'; outcome: 'answered' | 'cancelled' | 'timed_out'; response?: unknown }

/** What the gate needs of the agent whose calls it gates. */
export interface GateHost {
  /** Lets the call run (`allow`) or refuses it; ignored when the call no longer waits. */
  decide(callId: string, allow: boolean, approver?: string): void
  /** Throws when the call no longer waits: `respond`'s validator refuses it as stale. */
  validateDecision(callId: string): void
  /** What the model is told besides the fixed rejection, for a call that was not run (#2392). */
  refuse(callId: string, message: string): void
  /** The running turn; absent between turns. */
  turn(): GateTurn | undefined
}

export type SessionGateOptions = {
  sessionId: string
  owner: Owner
  creator: Owner
  host: GateHost
  /** A tool's gate kind, from the manifest: `answer` parks for a reply, anything else for approval. */
  kindOf: (tool: string) => 'approval' | 'answer' | undefined
  /** This turn's other tools (plugins, http_request), which are asked their tier before they park. */
  turnTools?: () => ReadonlySet<string>
  /** The queue `open_input`, `resolve_input`, `grant_input`, `deny_input` and `gate_settings` run on; default the workflow's. */
  taskQueue?: string
  /** The queue `gate.describe_call` and `turn_tool_tier` run on; default `agent-tools`. */
  toolsQueue?: string
  settings?: GateSettings
  /** How long a flow turn's call waits for the run grant's verdict before it parks; default one minute. */
  grantCheckWait?: Duration
}

type Parked = {
  callId: string
  entry: GateEntry
  tool: string
  summary: string
  prompt: string
  requestedBy: Owner
  createdAt: number
  expiresAt: number
  attention: { reason: string; on_timeout: string } | null
  /** Whether open_input has written its row: answerable from the moment it is parked, resolved only once its row is there. */
  opened: boolean
  openFailed: boolean
}

type Verdict = { verdict: string; grant_id?: string; timeout_s?: number; on_outside?: string; reason?: string; hop_s?: number; parent_hop?: boolean; parent_session?: string; parent_principal?: string }

type RespondArgs = { request_id?: string; response?: unknown; responder?: Partial<Owner> & { kind?: string }; role?: Role }

type Activities = Record<string, (input?: unknown) => Promise<unknown>>

const respondUpdate = defineUpdate<{ kind: 'approval' | 'answer'; outcome: InputOutcome }, [RespondArgs]>(RESPOND_UPDATE)
const cancelInputUpdate = defineUpdate<'cancelled' | 'none', [{ reason?: string }]>(CANCEL_INPUT_UPDATE)
const pendingInputQuery = defineQuery<(InputEntry & { expiring?: true })[]>(PENDING_INPUT_QUERY)

/** A park that ends with its scope (a cancelled turn) is not a failure. */
function ignoreCancellation(err: unknown): void {
  if (!isCancellation(err)) throw err
}

const iso = (at: number): string => new Date(at).toISOString()

export function refused(code: RefusalCode, message: string): ApplicationFailure {
  return ApplicationFailure.nonRetryable(message, `${GATE_REFUSED}:${code}`)
}

/** What the model is told when its call was not approved, in the words a classic turn uses (approvals/service.ts `refusal`). */
export function refusal(tool: string, requestId: string, outcome: string, reason?: string | null): string {
  const what = `${tool} (approval ${requestId})`
  if (outcome === 'denied') return `The user denied ${what}; it was not run. Do not retry it unless the user asks you to.`
  if (outcome === 'flow_denied') {
    return `The flow that sent this turn refused ${what}: it is outside what the run may do (${reason || 'no reason given'}), so it was not run. Do not retry it.`
  }
  if (outcome === 'expired') return `Nobody decided on ${what} before it expired, so it was not run. Tell the user it needs their approval.`
  if (outcome === 'cancelled') return `The approval for ${what} was cancelled (${reason || 'no reason given'}), so it was not run.`
  if (outcome === 'withdrawn') return `The approval for ${what} was withdrawn before it ran (${reason || 'no reason given'}), so it was not run.`
  return `${what} was not approved, so it was not run.`
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

export class SessionGate {
  readonly #opts: SessionGateOptions
  readonly #parked = new Map<string, Parked>()
  readonly #resolutions = new Map<string, { promise: Promise<Resolution>; resolve: (r: Resolution) => void }>()
  readonly #seen = new Set<string>()
  #settings: GateSettings
  readonly #own: Activities
  readonly #tools: Activities
  readonly #describe: Activities
  readonly #projects: Activities
  readonly #grants: Activities
  readonly #onRespond: Activities
  readonly #onTimer: Activities

  constructor(opts: SessionGateOptions) {
    this.#opts = opts
    this.#settings = opts.settings ?? { approvalExpiryS: 600, questionExpiryS: 3600 }
    const queue = opts.taskQueue ? { taskQueue: opts.taskQueue } : {}
    const toolsQueue = opts.toolsQueue ?? TASK_QUEUE
    const nonRetryable = ['UnknownSession', 'ToolError']
    this.#own = proxyActivities<Activities>({ ...queue, startToCloseTimeout: SHORT })
    this.#grants = proxyActivities<Activities>({ ...queue, startToCloseTimeout: SHORT, retry: { maximumAttempts: 5 } })
    this.#describe = proxyActivities<Activities>({ taskQueue: toolsQueue, startToCloseTimeout: SHORT, retry: { maximumAttempts: 5, nonRetryableErrorTypes: nonRetryable } })
    this.#tools = proxyActivities<Activities>({ taskQueue: toolsQueue, startToCloseTimeout: SHORT, retry: { maximumAttempts: 3, nonRetryableErrorTypes: nonRetryable } })
    this.#projects = proxyActivities<Activities>({
      taskQueue: PROJECTS_QUEUE,
      // No projects worker to answer (down, or a deployment without flows): the call parks
      // after this, rather than waiting on it for ever.
      scheduleToCloseTimeout: opts.grantCheckWait ?? GRANT_CHECK_WAIT,
      startToCloseTimeout: SHORT,
      retry: { maximumAttempts: 3 },
    })
    // On the respond path a person waits on the Update's answer, so the activity is
    // bounded at 30 s; on the timer path nobody waits, so it retries with no deadline
    // (backoff capped at 5 minutes) until Postgres takes the write (gate/activities.py).
    this.#onRespond = proxyActivities<Activities>({ ...queue, scheduleToCloseTimeout: SHORT })
    this.#onTimer = proxyActivities<Activities>({ ...queue, startToCloseTimeout: SHORT, retry: { maximumInterval: '5m' } })
  }

  get settings(): GateSettings {
    return this.#settings
  }

  /** Reads Settings as a turn starts (`gate_settings`). */
  async loadSettings(): Promise<GateSettings> {
    this.#settings = (await this.#own[SESSION_GATE_ACTIVITIES.settings]!()) as GateSettings
    return this.#settings
  }

  /** The Update, Query and validator of the gate: `respond`, `cancel_input`, `pending_input`. */
  install(): void {
    setHandler(respondUpdate, (args) => this.#respond(args), { validator: (args) => this.#validateRespond(args) })
    setHandler(cancelInputUpdate, async (args) => {
      const reason = String(args?.reason || 'cancelled').slice(0, 200)
      return (await this.cancelAll(reason, true)) > 0 ? 'cancelled' : 'none'
    })
    setHandler(pendingInputQuery, () => this.pending())
  }

  /** Parks every call of `calls()` once as it appears, for as long as the scope it runs in lives (`_watch_gate`). */
  async watch(calls: () => readonly GateCall[]): Promise<never> {
    for (;;) {
      await condition(() => calls().some((c) => !this.#seen.has(c.id)))
      for (const call of calls()) {
        if (!this.#seen.has(call.id)) {
          this.#seen.add(call.id)
          void this.parkOrRefuse(call).catch(ignoreCancellation)
        }
      }
    }
  }

  /**
   * A call that could not be described or opened is refused to the model, never left
   * undecided: the turn would wait on it until an interrupt (`_park_or_refuse`).
   */
  async parkOrRefuse(call: GateCall): Promise<void> {
    try {
      await this.open(call)
    } catch (err) {
      if (isCancellation(err)) throw err
      if (!(err instanceof TemporalFailure)) throw err
      // Parked: its entry is ended by the gate (cancel, interrupt, turn end).
      if ([...this.#parked.values()].some((p) => p.callId === call.id)) return
      this.#opts.host.decide(call.id, false, 'scadbuddy')
    }
  }

  /**
   * Parks a call (`_park`): the request id of its entry once the entry is open, with its
   * timer running in the scope it was opened in. A call that needs no entry is settled at
   * once and its resolution says how: a read turn tool and one inside the run's grant are
   * `approved`, one the flow denies is `denied`, a malformed answer is `cancelled`.
   */
  async open(call: GateCall): Promise<string> {
    const { sessionId, owner, creator, host } = this.#opts
    const rid = durableRequestId(sessionId, workflowInfo().runId, call.id)
    const kind: 'approval' | 'answer' = this.#opts.kindOf(call.name) === 'answer' ? 'answer' : 'approval'
    const turn = host.turn()
    const author = turn?.author ?? owner
    const slot = this.#slot(rid)
    const settled = (allow: boolean, approver: string, resolution: Resolution): string => {
      host.decide(call.id, allow, approver)
      slot.resolve(resolution)
      return rid
    }
    if (this.#opts.turnTools?.().has(call.name) && !(await this.#outward(call))) {
      // A read or write call goes straight through, as the permission seam lets it; its activity checks the tier again.
      return settled(true, 'scadbuddy', { kind, outcome: 'approved' } as Resolution)
    }
    let summary = ''
    let inputHash: string | null = null
    if (kind === 'approval') {
      const described = (await this.#describe[SESSION_GATE_ACTIVITIES.describe]!({ tool: call.name, input: call.input })) as { summary: unknown; input_hash: unknown }
      summary = String(described.summary)
      inputHash = String(described.input_hash)
    }
    let grantTimeout = 0
    let outside: Verdict | undefined
    const flow = turn?.flow
    if (kind === 'approval' && flow && inputHash !== null) {
      const verdict = await this.#grantVerdict(rid, call, flow)
      if (verdict?.verdict === 'inside') {
        const grantId = String(verdict.grant_id || '')
        if (
          grantId &&
          ((await this.#grants[SESSION_GATE_ACTIVITIES.grant]!({
            requestId: rid,
            sessionId,
            tool: call.name,
            summary,
            inputHash,
            grantId,
            requestedBy: author,
          } satisfies GrantInput)) as boolean)
        ) {
          // Inside the run's grant (spec 2026-10-11 §3.3): no entry, nobody asked; the
          // tool's activity checks the grant again as it runs.
          return settled(true, `grant:${grantId}`, { kind: 'approval', outcome: 'approved' })
        }
      } else if (verdict) {
        grantTimeout = Math.trunc(Number(verdict.timeout_s || 0))
        outside = verdict
      }
    }
    // A flow turn's call is in its run's queue (#2482): the person's, never a grant's.
    const escalation = kind === 'approval' && flow !== undefined
    if (escalation && outside?.on_outside === 'deny' && inputHash !== null) {
      const reason = String(outside.reason || '') || null
      await this.#grants[SESSION_GATE_ACTIVITIES.deny]!({
        requestId: rid,
        sessionId,
        runId: flow.runId,
        tool: call.name,
        summary,
        inputHash,
        reason,
        requestedBy: author,
      } satisfies DenyInput)
      // The script said so (spec §4.3): refused at once, never parked.
      host.refuse(call.id, refusal(call.name, rid, 'flow_denied', reason))
      return settled(false, `flow:${flow.runId}`, { kind: 'approval', outcome: 'denied' })
    }
    let built
    try {
      built = buildEntry({
        requestId: rid,
        kind,
        tool: call.name,
        toolInput: call.input,
        inputHash,
        requestedBy: { kind: author.kind, id: author.id },
        sessionOwner: { kind: owner.kind, id: owner.id },
        sessionCreator: { kind: creator.kind, id: creator.id },
        inRun: escalation,
      })
    } catch (err) {
      // Refused to the model, never parked; the error is not kept.
      if (err instanceof MalformedAnswerInputError) return settled(false, 'scadbuddy', { kind: 'answer', outcome: 'cancelled' })
      throw err
    }
    let hop: Partial<OpenInput> = {}
    let timeout: number
    if (kind === 'approval') {
      // A flow turn's call outside the grant waits as the run's own calls do.
      timeout = grantTimeout || this.#settings.approvalExpiryS
      if (escalation) {
        hop = { flowRunId: flow.runId, hop: 'person' }
        if (outside) hop.outsideReason = String(outside.reason || '') || null
        let hopS = Math.trunc(Number(outside?.hop_s || 0))
        const parent = outside?.parent_session
        const principal = outside?.parent_principal
        if (outside?.parent_hop && hopS > 0 && typeof parent === 'string' && isUuid(parent) && typeof principal === 'string' && principal) {
          // The starter session first, for its hop; then the person. One timer covers
          // both (spec §4.6): the agent's sweep moves it up. Each hop has the run's
          // timeout: a long parent hop is cut so the person keeps theirs, rather than the
          // whole being cut to ENTRY_MAX_S and the person left a second (#2862).
          hopS = Math.max(1, Math.min(hopS, ENTRY_MAX_S - timeout))
          hop = { ...hop, hop: 'parent', hopUntil: iso(Date.now() + hopS * 1000), parentSession: parent, parentPrincipal: principal }
          timeout = Math.min(hopS + timeout, ENTRY_MAX_S)
        }
      }
    } else if (call.name === ASK_USER) {
      timeout = this.#settings.questionExpiryS
    } else {
      timeout = built.timeoutS || DEFAULT_TIMEOUT_S
    }
    const now = Date.now()
    const parked: Parked = {
      callId: call.id,
      entry: built.entry,
      tool: call.name,
      summary,
      prompt: built.prompt,
      requestedBy: author,
      createdAt: now,
      expiresAt: now + timeout * 1000,
      attention: built.attention,
      opened: false,
      openFailed: false,
    }
    // Parked before its row is opened, so a respond that reads the row as soon as it is
    // written finds the call here; every resolution waits for the open (resolve).
    this.#parked.set(rid, parked)
    try {
      await this.#own[SESSION_GATE_ACTIVITIES.open]!({
        requestId: rid,
        sessionId,
        workflowId: workflowInfo().workflowId,
        workflowRunId: workflowInfo().runId,
        kind,
        tool: call.name,
        toolUseId: call.id,
        summary,
        inputHash,
        prompt: built.prompt,
        requestedBy: author,
        responders: kind === 'approval' ? ['browser', 'grant'] : ['browser'],
        expiresAt: iso(parked.expiresAt),
        questions: built.questions,
        attention: built.attention,
        ...hop,
      } satisfies OpenInput)
    } catch (err) {
      parked.openFailed = true
      this.#parked.delete(rid)
      slot.resolve({ kind, outcome: 'cancelled' } as Resolution)
      throw err
    }
    parked.opened = true
    void this.#expire(parked, timeout)
    return rid
  }

  /** How the entry ended, once it has. */
  resolution(requestId: string): Promise<Resolution> {
    return this.#slot(requestId).promise
  }

  #slot(requestId: string): { promise: Promise<Resolution>; resolve: (r: Resolution) => void } {
    let slot = this.#resolutions.get(requestId)
    if (!slot) {
      slot = deferred<Resolution>()
      this.#resolutions.set(requestId, slot)
    }
    return slot
  }

  /** Every pending entry cancelled with `reason`; how many. Only cancel_input, an Update with a caller to tell, gives up after 30 s (`bounded`). */
  async cancelAll(reason: string, bounded = false): Promise<number> {
    const pending = [...this.#parked.values()].filter((p) => p.entry.state === 'pending')
    for (const parked of pending) await this.#resolve(parked, 'cancelled', SYSTEM, undefined, reason, !bounded)
    return pending.length
  }

  /**
   * Lets every resolve_input in flight finish before the parks are cancelled (#2123): one
   * whose timer fired is in a park task, and cancelling it there would leave its row
   * pending. One that fails (a respond's bounded attempt) is cancelled after.
   */
  async settle(reason: string): Promise<void> {
    for (;;) {
      await condition(() => [...this.#parked.values()].every((p) => p.entry.state !== 'resolving'))
      if ((await this.cancelAll(reason)) === 0) return
    }
  }

  /** The entries a client may answer, as the projection lists them (the `pending_input` Query). */
  pending(): (InputEntry & { expiring?: true })[] {
    const out: (InputEntry & { expiring?: true })[] = []
    for (const p of this.#parked.values()) {
      // Listed once its row is written, as the projection lists it; a respond is taken a moment sooner (open).
      if (p.entry.state === 'resolved' || !p.opened) continue
      const entry: InputEntry & { expiring?: true } = {
        id: p.entry.id,
        kind: p.entry.kind,
        session_id: this.#opts.sessionId,
        tool: p.tool,
        summary: p.summary,
        input_hash: p.entry.inputHash,
        prompt: p.prompt,
        requested_by: p.entry.kind === 'approval' ? p.requestedBy : null,
        responders: p.entry.kind === 'approval' ? ['browser', 'grant'] : ['browser'],
        created_at: iso(p.createdAt),
        expires_at: iso(p.expiresAt),
      }
      if (p.attention) entry.attention = p.attention
      if (p.entry.state === 'resolving') entry.expiring = true
      out.push(entry)
    }
    return out
  }

  async #expire(parked: Parked, timeoutS: number): Promise<void> {
    try {
      if (await condition(() => parked.entry.state !== 'pending', timeoutS * 1000)) return
      if (parked.entry.state !== 'pending') return
      if (parked.entry.kind === 'approval') await this.#resolve(parked, 'expired', SYSTEM, undefined, undefined, true)
      else if (parked.tool === ASK_USER) await this.#resolve(parked, 'cancelled', SYSTEM, undefined, NOBODY_ANSWERED, true)
      else await this.#resolve(parked, 'timed_out', SYSTEM, undefined, undefined, true)
    } catch (err) {
      // A cancelled scope or a failed activity ends the timer, as Python's park task did.
      if (!(err instanceof TemporalFailure)) throw err
    }
  }

  async #grantVerdict(rid: string, call: GateCall, flow: { runId: string; callId: string }): Promise<Verdict | undefined> {
    // The run's grant on a flow turn's call (`flow_grant_check` on the backend's `projects`
    // queue, spec 2026-10-11 §3.4). Undefined when it could not answer, which parks the
    // call (fail toward asking).
    try {
      return (await this.#projects[FLOW_GRANT_CHECK_ACTIVITY]!({
        run_id: flow.runId,
        request_id: rid,
        fn: 'tool',
        tool: call.name,
        args: call.input,
        session_id: this.#opts.sessionId,
        flow_call_id: flow.callId,
      })) as Verdict
    } catch (err) {
      if (err instanceof ActivityFailure) return undefined
      throw err
    }
  }

  async #outward(call: GateCall): Promise<boolean> {
    // Whether a turn tool's call is outward, by its input (turn_tool_tier). One the agent
    // cannot tier (its plugin did not answer, or is gone) parks: fail toward asking.
    try {
      const tiered = (await this.#tools[SESSION_GATE_ACTIVITIES.turnToolTier]!({ tool: call.name, input: call.input })) as { tier?: string } | null
      return tiered?.tier === 'outward'
    } catch (err) {
      if (err instanceof ActivityFailure) return true
      throw err
    }
  }

  async #resolve(parked: Parked, outcome: InputOutcome, responder: Responder, response: unknown, reason: string | null | undefined, timer: boolean, rawResponse?: unknown): Promise<boolean> {
    // A response is written only after its entry's row: a call is parked (and so
    // answerable) before open_input has written it. One whose open failed was never
    // asked; its call is refused by parkOrRefuse.
    if (!(parked.opened || parked.openFailed)) {
      await condition(() => parked.opened || parked.openFailed)
      // Another resolution may have run while this one waited.
      if (parked.openFailed || parked.entry.state !== 'pending') return false
    }
    parked.entry = { ...parked.entry, state: 'resolving' }
    let written: boolean
    try {
      written = (await (timer ? this.#onTimer : this.#onRespond)[SESSION_GATE_ACTIVITIES.resolve]!({
        requestId: parked.entry.id,
        outcome,
        responder,
        response,
        ...(rawResponse === undefined ? {} : { rawResponse }),
        reason,
      } satisfies ResolveInput)) as boolean
    } catch (err) {
      parked.entry = { ...parked.entry, state: 'pending' }
      throw err
    }
    parked.entry = { ...parked.entry, state: 'resolved' }
    this.#parked.delete(parked.entry.id)
    // An approval runs only with its approved row; an answer's call reads whatever was
    // recorded, so it is let through to report it.
    const go = parked.entry.kind === 'answer' || (written && outcome === 'approved')
    // The plugin gives the model a fixed rejection; the runner adds why (#2392).
    if (!go) this.#opts.host.refuse(parked.callId, refusal(parked.tool, parked.entry.id, outcome === 'approved' ? 'withdrawn' : outcome, reason))
    // The turn may have ended while resolve_input ran (an interrupt): the call then
    // waits no more, and its row is already right.
    this.#opts.host.decide(parked.callId, go, responder.id)
    this.#resolutions.get(parked.entry.id)?.resolve(
      parked.entry.kind === 'approval'
        ? { kind: 'approval', outcome: outcome as 'approved' | 'denied' | 'expired' | 'cancelled' }
        : { kind: 'answer', outcome: outcome as 'answered' | 'cancelled' | 'timed_out', ...(response !== undefined ? { response } : {}) },
    )
    return written
  }

  #request(args: RespondArgs): RespondRequest {
    const responder = args.responder ?? {}
    return {
      requestId: String(args.request_id ?? ''),
      response: args.response,
      responder: { kind: String(responder.kind ?? '') as Owner['kind'], id: String(responder.id ?? '') },
      role: (args.role ?? '') as Role,
    }
  }

  async #respond(args: RespondArgs): Promise<{ kind: 'approval' | 'answer'; outcome: InputOutcome }> {
    const request = this.#request(args)
    const parked = this.#parked.get(request.requestId)
    if (!parked) throw refused('stale', `no pending input ${request.requestId}`)
    // Read from its row before open_input's result reached here: wait for it (open).
    if (!(parked.opened || parked.openFailed)) await condition(() => parked.opened || parked.openFailed)
    if (parked.openFailed) throw refused('stale', `no pending input ${request.requestId}`)
    let valid
    try {
      // The validator passed, but another handler may have moved the entry since.
      valid = validateRespond(parked.entry, request)
    } catch (err) {
      if (err instanceof RespondRefusal) throw refused(err.code, err.message)
      throw err
    }
    const responder = args.responder ?? {}
    const who: Responder = { kind: String(responder.kind ?? ''), id: String(responder.id ?? ''), label: String(responder.label ?? '') }
    if ('decision' in valid) {
      const outcome = valid.decision === 'approve' ? 'approved' : 'denied'
      await this.#resolve(parked, outcome, who, { decision: valid.decision }, null, false, args.response)
      return { kind: parked.entry.kind, outcome }
    }
    await this.#resolve(parked, 'answered', who, { answers: [...valid.answers] }, null, false, args.response)
    return { kind: parked.entry.kind, outcome: 'answered' }
  }

  #validateRespond(args: RespondArgs): void {
    const request = this.#request(args)
    const parked = this.#parked.get(request.requestId)
    try {
      validateRespond(parked?.entry, request)
    } catch (err) {
      if (err instanceof RespondRefusal) throw refused(err.code, err.message)
      throw err
    }
    try {
      this.#opts.host.validateDecision(parked!.callId)
    } catch {
      throw refused('stale', `${request.requestId} is no longer waiting`)
    }
  }
}
