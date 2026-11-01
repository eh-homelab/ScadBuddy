import { randomUUID } from 'node:crypto'
import type { SDKResultMessage } from '@anthropic-ai/claude-agent-sdk'
import { ApplicationFailure } from '@temporalio/common'
import { context as otelContext } from '@opentelemetry/api'
import type { Sql, TransactionSql } from 'postgres'
import type { GateTrace } from '../approvals/service.js'
import { contextFrom } from '../telemetry/trace.js'
import { turnCostEntry } from '../audit/turn.js'
import { redact, WAIT_FOR_USER } from '../gate/entry.js'
import { refusal } from '../gate/refusal.js'
import { INPUT_CHANNEL } from '../gate/store.js'
import { type AttentionSpec, answeredText, BACK_REPLIES, DEFAULT_REPLIES, RECONNECTED_TEXT, timedOutText } from '../harness/attention.js'
import type { ApprovalGate, ApprovalVerdict } from '../harness/permissions.js'
import type { QuestionGate, QuestionRequest, QuestionVerdict, UserQuestion } from '../harness/questions.js'
import type { QuestionService } from '../questions/service.js'
import type { Continuation, OpenInputAnswer, OpenInputArgs, SessionTurnInput, SessionTurnResult, WithdrawInputArgs } from '../temporal/session/models.js'
import type { DecidedCall, DecidedRun } from '../temporal/toolActivities.js'
import { NO_CAPACITY, SESSION_GONE, SHUTTING_DOWN_FAILURE } from '../temporal/session/names.js'
import { attentionGate, type HeldRow, replyOf, TAB_WAIT_CALL } from './attentionGate.js'
import type { SessionBlobs } from './blobs.js'
import { userPrompt } from './images.js'
import { type ClaimedSession, runningDurableTurn, TAB_WAIT_S, type TurnOutcome, waitForTab } from './manager.js'
import { scrubForLog } from './sdkEvents.js'
import { CoreTurn, type TallyInput, tallyTurn, type TurnCoreDeps, type TurnGates, type TurnTally } from './turnCore.js'
import type { WarmEntry } from './warm.js'

// An AgentSession's turn on this replica (unified runtime spec 2026-10-13 §5.2, §5.5): what
// the `session_turn` activity runs (temporal/session/turnActivity.ts). The turn is a classic
// one (turnCore.ts, as SessionManager.runTurn runs it) on the session's warm Claude Code:
//
//   1. the claim: the row runs exactly this turn (`durable_turn_id`, DurableTurns.send's), and
//      this placement of it (`turn_attempt`, the workflow's run id and cycle);
//   2. assemble (TurnRunner), acquire the warm process, push the message;
//   3. the events, as a classic turn writes them (sdkEvents.ts);
//   4. the turn's cost as it goes (FENCE_MS, and each finished request), so a placement that
//      dies before its result still counts against the budget; at its end the cost and turns
//      as a classic turn reckons them, as increments over what it wrote; the closing events
//      (`tail`) are the workflow's `session.finish_turn`.
//
// Placements (§5.2 "Recovery"). A placement the workflow gave up on may still run here (its
// heartbeat stalled, it ran past `turn_max_seconds`). Each of its event appends and transcript
// appends checks, in its own transaction, that the row is still this placement's
// (`turn_attempt`), so none lands once another has claimed the turn. Every FENCE_MS it also reads
// the row, and once another placement holds it, or the turn is no longer the row's, it is cut
// off: its process closed. Its spend is always written: its requests are its own, never the
// next placement's, so the budget counts every replica that ran the turn, once. A new placement
// here for the same session cuts an older one first, so the warm process is free.
//
// A `restarted` continuation (§5.5) is decided from the transcript, not from the workflow's
// word: the first placement marks the main transcript's last entry (`turn_mark`), and a later
// one resumes with RESTARTED_PROMPT only when a user entry landed after it (the message went
// in, and Claude Code wrote it); otherwise it sends the message itself.
//
// Parks (§5.4, "Open" and "Hold"). An outward call (canUseTool) or an AskUserQuestion (the
// question gate) is opened through the session's own workflow, the Update `open_input`
// (TurnControl.openInput), which records the entry and writes its row; the turn then holds,
// its process warm, until the entry's response row is in ai_input_responses, woken by
// `NOTIFY scadbuddy_input` (gate/store.ts, sent as the row commits) and polled every
// INPUT_POLL_MS besides. The row decides the call: an approved one runs in the turn as a
// classic call does; any other end is what the model reads, in the gate's words
// (gate/refusal.ts). Each call parks on an entry of its own, and the holds are independent;
// Claude Code (2.1.296, test/sessionPark.temporal.test.ts) asks for the calls of one
// assistant message one after another, so they park in turn. A call refused after its
// entry opened (the open's answer lost, Claude Code dropping the request) withdraws the
// entry (Update `withdraw_input`), so nobody can approve a call that will never run; a read
// of the row that fails is tried again on the next tick.
//
// Attention (#5003, §5.4). request_user_attention and a browser call's wait for its tab park the
// same way, as the gate's `wait_for_user` entry, with classic's semantics (attentionGate.ts): a
// reply, the tab's return (`reconnected`, which the process whose hub sees the tab sends as the
// Update `tab_reconnected`, manager.ts `resolveTabWaits`), or the entry's timer, whose `stop` and
// `wait` end the turn. A `done` summary waits for nothing and outlives its turn, so it is posted
// as classic's is (questions/service.ts), never through the workflow, whose entries end with
// their turn. A released one is told in the continuation in the words its call would have read.
//
// Release (§5.4 "Release"). A hold lasts `park_hold_seconds` (the input's parkHoldSeconds);
// past it, or at a shutdown, the turn lets its process go. Each held call is answered with a
// refusal that interrupts the turn (`interrupt`, as a Stop does): Claude Code records it as the
// call's error result and asks the model nothing more. An attention request is an MCP tool's
// call, whose result cannot interrupt, so its release stops the turn itself. Closing the
// process instead, with the call still held, makes Claude Code read the closed permission
// stream as a failed call and ask the model again before it dies. Once the turn has ended the warm entry is closed (not
// cut: its mirror lands what Claude Code wrote), its spend is written as a stopped
// placement's, and the activity returns `released` with the entries it held, which stay open
// in the workflow. Once each is decided
// the workflow places the turn again with a `parked` continuation: each approved call is run
// once, outside the model, through the tool layer (deps.runCall, toolActivities.ts
// `decidedCalls`, its Idempotency-Key the request id), and the model is told in one message
// how each call went. That message goes in once: the continuation has its own transcript mark,
// and a later placement of it whose message landed resumes with RESTARTED_PROMPT instead.
//
// A shutdown (`stop`) gives the running turns their grace, then cuts the rest: only those fail
// with the retryable ShuttingDown, which places the turn on another replica; a turn that ended
// on its own in the grace (a reply, a refusal) keeps its end.

export const RESTARTED_PROMPT =
  'The agent service restarted during this turn. Calls that were in flight are marked as interrupted; check before repeating any of them. Continue the task.'
/** What a call that cannot park here reads: an attention request or a tab wait, or a turn with no way to its workflow. */
export const NO_PARKS = 'this session cannot wait for an approval or an answer yet, so the call was not run'
/** How often a held call reads its entry's response row besides the NOTIFY (approvals/service.ts's waiter's 1 s). */
export const INPUT_POLL_MS = 1_000
/** The gate's question tool (gate/entry.ts ASK_USER): AskUserQuestion and the subagents' ask_user park as it. */
const GATE_ASK = 'ask_user'
/** How often a running placement checks that it still holds the turn, and writes what it has spent. */
export const FENCE_MS = 1_000

/** What `session_turn` heartbeats: whether the message has gone into Claude Code. Advisory (logs, history); the transcript decides a restart. */
export type TurnBeat = { pushed: boolean }

/** Why a placement's activity was cancelled: the workflow's interrupt, the worker stopping, or the workflow no longer waiting on it (timed out). */
export type CancelReason = 'interrupt' | 'shutdown' | 'gone'

export type TurnControl = {
  heartbeat: (beat: TurnBeat) => void
  cancelled: AbortSignal
  /** Read when `cancelled` fires. */
  cancelReason: () => CancelReason
  /** The Update `open_input` on the session's workflow (§5.4): how a call parks, given up when `signal` aborts. Absent, every park reads NO_PARKS. */
  openInput?: (args: OpenInputArgs, signal: AbortSignal) => Promise<OpenInputAnswer>
  /** The Update `withdraw_input`: the entry of a call the turn no longer waits on is cancelled. */
  withdrawInput?: (args: WithdrawInputArgs) => Promise<boolean>
  /** The Update `tab_reconnected` for one tab wait: its tab was back by the time its entry opened (#5003). */
  reconnected?: (requestId: string) => Promise<unknown>
}

/** Why a placement was cut off: another holds the row, the turn ended, a newer one here, the shutdown. */
type Cut = 'moved' | 'ended' | 'replaced' | 'shutdown'
const CUT_TEXT: Record<Cut, string> = {
  moved: 'the turn was placed on another worker',
  ended: 'the turn ended without this placement',
  replaced: 'a newer placement of the session took over',
  shutdown: 'the agent service is shutting down',
}

export type TurnHostDeps = {
  /** The replica's turn machinery (SessionManager.turnCore): its warm processes, events, audit. */
  core: TurnCoreDeps
  sql: Sql
  /** The message's images, by the names DurableTurns.send stored them under; and a tool result's, stored under the fence. */
  images: Pick<SessionBlobs, 'get' | 'putIf'>
  /** This replica's session host queue (`agent-sessions.<identity>`), what each result names. */
  hostQueue: string
  /** Tests only: the fence's period (FENCE_MS). */
  fenceMs?: number
  /** LISTEN on a channel (events/pgListener.ts `listenAlso`): a held call wakes on `scadbuddy_input`. Without it, the poll alone. */
  listen?: (channel: string, onNotify: (payload: string) => void) => void
  /** Tests only: how often a held call reads its row (INPUT_POLL_MS). */
  inputPollMs?: number
  /** Runs a released turn's approved call once (toolActivities.ts `decidedCalls`); absent, the model is told it was not run. */
  runCall?: (call: DecidedCall) => Promise<DecidedRun>
  /** Where a `done` summary is posted, as a classic turn posts it (#815 §4); absent, it reads NO_PARKS. */
  questions?: Pick<QuestionService, 'gate' | 'attentionLimit'>
}

/** A placement's holds (§5.4): the calls held now, the hold's length, and the release that ends them all. */
type Holds = {
  held: Set<string>
  holdMs: number
  /** Releases the turn if it holds a call; whether it did. */
  release: () => boolean
  /** Aborts once the turn is released. */
  released: AbortSignal
  /** The calls the release took (held as it came): the entries the workflow waits on. */
  taken: string[]
}

/** A parked continuation's call (§5.4 step 5): as `open_input` named it, and how its entry ended. */
type ParkedCall = Extract<Continuation, { kind: 'parked' }>['calls'][number]

/** The questions of a parked AskUserQuestion, in order (its card, `{questions}`). */
function questionsOf(input: unknown): string[] {
  const questions = (input as { questions?: unknown } | null)?.questions
  return Array.isArray(questions) ? questions.map((q) => (typeof (q as { question?: unknown })?.question === 'string' ? (q as { question: string }).question : '')) : []
}

/** An entry's response row, as resolve_input (or a grant, or the flow's refusal) wrote it. */
/** The entry's response row, with the decision's span (#5041). */
type Response = HeldRow & { decision_traceparent?: string | null }

/** What a parked attention request named: its `wait_for_user` input (TurnHost's attention gate). */
type AttentionInput = { reason?: string; timeout_s?: number; on_timeout?: string }

type Running = { attempt: string; cut: (why: Cut) => void; release: () => boolean; done: Promise<void> }

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/** A question as the panel's card shows it: every string with the turn's secrets redacted (gate/entry.ts `redact`). */
function cardOf(q: UserQuestion, secrets: readonly string[]): UserQuestion {
  return {
    ...q,
    question: redact(q.question, secrets),
    header: redact(q.header, secrets),
    options: q.options.map((o) => ({
      ...o,
      label: redact(o.label, secrets),
      description: redact(o.description, secrets),
      ...(o.preview !== undefined ? { preview: redact(o.preview, secrets) } : {}),
    })),
  }
}

const within = (ms: number, work: Promise<unknown>) => Promise.race([work, new Promise((resolve) => setTimeout(resolve, ms).unref())])

/** Why a released placement stopped: what its process is closed with, and its tally's stop. */
const RELEASED_TEXT = 'a call waited longer than the turn holds its process; the turn goes on once it is decided'
/** What a released call's result says until the continuation tells the model how it ended. */
const RELEASED_CALL = 'This call is still waiting for the user; the turn paused, and you will be told how it ended.'
/** A held call's default hold, when the workflow names none (park_hold_seconds' default). */
const PARK_HOLD_S = 300
/** A tab wait's quick reply that ends the turn's tab waits (sessions/manager.ts waitForTab's CARRY_ON). */
const CARRY_ON = 'Carry on without the tab'

/**
 * Whether a user entry landed in `id`'s main transcript after row `mark`. A sealed row says so in
 * its plaintext `entry_type`; a row from before sealing has none, so its own top-level `type` is
 * read (a substring match would take an assistant entry that quotes one). An unparseable legacy
 * row is no user entry.
 */
export async function userEntryLanded(sql: Sql, id: string, mark: number): Promise<boolean> {
  const rows = await sql<{ typed: boolean; entry: string | null }[]>`
    SELECT entry_type = 'user' AS typed, entry FROM ai_session_entries
    WHERE session_id = ${id} AND subpath = '' AND id > ${mark}
      AND (entry_type = 'user' OR (entry_type IS NULL AND entry LIKE '%"user"%'))
    ORDER BY id`
  return rows.some((row) => {
    if (row.typed) return true
    try {
      const parsed: unknown = JSON.parse(row.entry ?? '')
      return typeof parsed === 'object' && parsed !== null && (parsed as { type?: unknown }).type === 'user'
    } catch {
      return false
    }
  })
}

export class TurnHost {
  readonly #deps: TurnHostDeps
  #stopping = false
  /** The placement running here for each session. */
  readonly #running = new Map<string, Running>()
  /**
   * Each session's latest arrival here: only it may give back the place it reserved (`unreserve`).
   * A token of its own, not the attempt id, which the next turn's first placement in a run shares.
   */
  readonly #arrived = new Map<string, object>()
  /** Held calls by request id: each is woken by its entry's NOTIFY. */
  readonly #waiters = new Map<string, Set<() => void>>()

  constructor(deps: TurnHostDeps) {
    this.#deps = deps
    // The only listener of this channel on the connection: listenAlso keeps one per channel, so a second would replace this one.
    deps.listen?.(INPUT_CHANNEL, (requestId) => {
      for (const wake of this.#waiters.get(requestId) ?? []) wake()
    })
  }

  /** The service is shutting down: no turn is cut yet (`stop` does that past the grace). */
  drain(): void {
    this.#stopping = true
  }

  get stopping(): boolean {
    return this.#stopping
  }

  /** How many placements run here now. */
  get active(): number {
    return this.#running.size
  }

  /**
   * SIGTERM (main.ts): the running turns get `graceMs` to end on their own; the rest are cut
   * (each then fails ShuttingDown, and its workflow places it elsewhere) and get `abortWaitMs`
   * to write what they spent.
   */
  async stop({ graceMs, abortWaitMs }: { graceMs: number; abortWaitMs: number }): Promise<void> {
    this.drain()
    // A turn holding a call would not end in the grace: it is released (§5.4), and goes on elsewhere once decided.
    for (const r of this.#running.values()) r.release()
    const ended = () => Promise.all([...this.#running.values()].map((r) => r.done))
    await within(graceMs, ended())
    if (this.#running.size === 0) return
    for (const r of this.#running.values()) r.cut('shutdown')
    await within(abortWaitMs, ended())
  }

  async run(input: SessionTurnInput, control: TurnControl): Promise<SessionTurnResult> {
    const id = input.sessionId
    const attempt = input.attempt ?? randomUUID()
    // A replica shutting down takes no new placement, and evicts nothing for one: the turn moves.
    if (this.#stopping) throw ApplicationFailure.retryable(CUT_TEXT.shutdown, SHUTTING_DOWN_FAILURE, { pushed: false })
    // Its place here again (§5.2): a home kept across turns may have let its reservation lapse
    // (`warm_idle_seconds`), and the cache filled since. Refused, before anything is claimed or
    // spawned, when every process here runs a turn: the workflow moves the turn (`movesWorker`),
    // and the replica never goes past `warm_max_per_replica`. Taking the place may evict another
    // session's idle process, as acquire_worker's does; a turn that then ends before its spawn
    // (SessionGone, a refusal, a move) gives the place back (`unreserve`, below).
    if (this.#deps.core.warm.reserve(id) === 'full') {
      throw ApplicationFailure.retryable(`every warm session on ${this.#deps.hostQueue} is running a turn`, NO_CAPACITY)
    }
    const arrival = {}
    this.#arrived.set(id, arrival)
    // An older placement of the session still here (cut at its time cap, its activity not yet told): it goes first.
    const before = this.#running.get(id)
    if (before) {
      before.cut('replaced')
      await within(10_000, before.done)
    }
    let finished!: () => void
    const done = new Promise<void>((resolve) => {
      finished = resolve
    })
    let cut: Cut | undefined
    const entries: WarmEntry[] = []
    const controller = new AbortController()
    const cutOff = (why: Cut) => {
      if (cut !== undefined) return
      cut = why
      controller.abort(new Error(CUT_TEXT[why]))
      // A shutdown leaves the process to end as a stopped turn's does; any other cut is a placement nobody waits on any more.
      if (why !== 'shutdown') for (const entry of entries) void this.#deps.core.warm.cut(entry, CUT_TEXT[why])
    }
    const releasing = new AbortController()
    const holds: Holds = {
      held: new Set(),
      holdMs: (input.parkHoldSeconds ?? PARK_HOLD_S) * 1000,
      released: releasing.signal,
      taken: [],
      release: () => {
        if (releasing.signal.aborted || cut !== undefined || holds.held.size === 0) return false
        holds.taken = [...holds.held]
        // Each held call is answered (an interrupting refusal), which ends the turn; the process is closed after.
        releasing.abort(new Error(RELEASED_TEXT))
        return true
      },
    }
    const self: Running = { attempt, cut: cutOff, release: holds.release, done }
    this.#running.set(id, self)
    try {
      return await this.#run(input, attempt, control, controller, () => cut, cutOff, entries, holds)
    } catch (err) {
      // Its place, unless a spawn took it: the workflow places the turn again, elsewhere or here.
      // Not when a newer placement of the session arrived since (it cut this one): the place is its.
      if (this.#arrived.get(id) === arrival) this.#deps.core.warm.unreserve(id)
      throw err
    } finally {
      if (this.#arrived.get(id) === arrival) this.#arrived.delete(id)
      if (this.#running.get(id) === self) this.#running.delete(id)
      finished()
    }
  }

  async #run(
    input: SessionTurnInput,
    attempt: string,
    control: TurnControl,
    controller: AbortController,
    cutBy: () => Cut | undefined,
    cutOff: (why: Cut) => void,
    entries: WarmEntry[],
    parks: Holds,
  ): Promise<SessionTurnResult> {
    const { sessionId: id, turnId } = input
    const sql = this.#deps.sql
    const parked = input.continuation?.kind === 'parked' ? input.continuation.calls : undefined
    // A parked continuation marks the transcript for itself: its outcome message goes in once.
    const mark = await this.#claim(id, turnId, attempt, parked?.length ? `${turnId}/${parked[0]!.requestId}` : turnId)
    const claim = mark === undefined ? undefined : await runningDurableTurn(sql, id, turnId)
    if (mark === undefined || !claim) throw ApplicationFailure.nonRetryable(`session ${id} is not running turn ${turnId}`, SESSION_GONE)
    const { session, principal } = claim
    // §5.5: resumed with the restart message only once the turn's prompt is in the transcript;
    // a parked continuation's, once its outcome message is.
    const landed = (input.continuation?.kind === 'restarted' && input.message) || parked?.length ? await this.#landed(id, mark) : false
    const restarted = (input.continuation?.kind === 'restarted' && (!input.message || landed)) || (parked !== undefined && landed)
    const decided = parked && !restarted ? await this.#decided(id, parked, controller.signal, () => control.heartbeat({ pushed: false })) : undefined
    // An attention request's `stop` or `wait` timer ended while the turn was released: the turn
    // ends there, as it would have held, and the model is asked nothing more (harness/attention.ts).
    if (decided?.stop !== undefined) {
      const said = `the turn ended: ${decided.stop}`
      return this.#result(tallyTurn(session, { stopped: said, interrupted: said, result: undefined, refused: undefined, failure: undefined, secrets: [], turnUsd: 0, cutUsd: 0 }), [])
    }
    const text = restarted ? RESTARTED_PROMPT : decided ? decided.text : (input.message?.text ?? '')
    if (!text) throw ApplicationFailure.nonRetryable(`turn ${turnId} of session ${id} has no message`, SESSION_GONE)
    const images = restarted || parked ? [] : await this.#images(id, input.message?.images ?? [])
    const prompt = images.length ? userPrompt(text, images) : text

    const onCancel = () => {
      const why = control.cancelReason()
      if (why === 'interrupt') controller.abort(new Error('the turn was interrupted'))
      else cutOff(why === 'shutdown' ? 'shutdown' : 'ended')
    }
    control.cancelled.addEventListener('abort', onCancel, { once: true })
    if (control.cancelled.aborted) onCancel()
    /** Spoken for: nothing more of this placement is written once it is cut off (a shutdown's still is). */
    const fenced = () => {
      const why = cutBy()
      return why !== undefined && why !== 'shutdown'
    }
    const spend = new Spend(sql, id)
    /**
     * This placement still holds its turn: checked, row locked, in each write's own transaction.
     * The turn as well as the attempt: an attempt id is the run's and the cycle's, so the next
     * turn's first placement in the same run carries this one's, and the turn ended leaves it.
     */
    const holds = async (tx: TransactionSql, lock: 'share' | 'update') => {
      const rows = lock === 'share'
        ? await tx`SELECT 1 FROM ai_sessions WHERE id = ${id} AND turn_attempt = ${attempt} AND durable_turn_id = ${turnId} FOR SHARE`
        : await tx`SELECT 1 FROM ai_sessions WHERE id = ${id} AND turn_attempt = ${attempt} AND durable_turn_id = ${turnId} FOR UPDATE`
      return rows.length > 0
    }
    const holdsOrLast = async (tx: TransactionSql) => {
      const rows = await tx`
        SELECT 1 FROM ai_sessions WHERE id = ${id} AND turn_attempt = ${attempt}
          AND (durable_turn_id = ${turnId} OR durable_turn_id IS NULL) FOR SHARE`
      return rows.length > 0
    }
    // A process of the session's left here by an earlier placement writes under this one now.
    // `durable_turn_id IS NULL` lets it write between turns: what keeps a stale placement from
    // writing after a later turn on another replica has ended is that each placement's
    // `turn_attempt` (run id and cycle) is its own. A change to how attempts are minted must keep that.
    this.#deps.core.warm.refence(id, holdsOrLast)
    const events = this.#deps.core.events
    // The turn's spans are children of the sender's trace, as a classic turn's are (turnCore.ts).
    const sender = contextFrom(input.message?.traceparent, otelContext.active())
    const core: CoreTurn = otelContext.with(sender, () => new CoreTurn(this.#deps.core, {
      session,
      turnId,
      principal,
      userText: input.message?.words ?? input.message?.text ?? text,
      signal: controller.signal,
      interrupt: () => controller.abort(new Error('the turn was interrupted')),
      gates: (ctx) =>
        this.#gates({
          session,
          turnId,
          control,
          stop: controller.signal,
          holds: parks,
          secrets: ctx.secrets,
          shown: ctx.shown,
          trace: ctx.trace,
          asks: ctx.turn.run.questionGate !== undefined,
          // An attention request's timer, or its release, ends the turn as a Stop does: an MCP tool's result cannot.
          endTurn: (why) => {
            if (!controller.signal.aborted) controller.abort(new Error(why))
          },
        }),
      // The prompt goes in right after the acquire.
      onAcquired: ({ entry }) => {
        entries.push(entry)
        // Its transcript appends only while this placement holds the turn, or is the session's
        // last placement and no turn runs: what Claude Code writes as it exits between turns
        // (`last-prompt`, `cost-state`, warm.ts DRAIN_MS) lands as a classic process's does,
        // and never once another turn has the session.
        this.#deps.core.warm.fence(entry, (tx) => holdsOrLast(tx))
        if (fenced()) void this.#deps.core.warm.cut(entry, 'the placement was cut off')
        control.heartbeat({ pushed: true })
      },
      onMessage: (message) => {
        if (message.type === 'stream_event' && message.event.type === 'message_stop') spend.flush(core.runningUsd())
      },
      mayWrite: () => !fenced(),
      // A tool result's images, and its events, only while this placement holds the turn, checked with the write.
      storeImages: (images) => this.#deps.images.putIf(id, images, (tx) => holds(tx, 'share')),
      appendEvents: async (batch) => {
        const written = await sql.begin(async (tx) => ((await holds(tx, 'update')) ? await events.append(id, batch, tx) : undefined))
        if (written) events.committed(id, batch, written)
        return written !== undefined
      },
    }))
    // The fence: still the row's placement and turn, and what it has spent so far written. A
    // Stop's flag (DurableTurns.interrupt) ends the turn here, on this tick: the workflow's
    // Signal is the backstop.
    const fence = setInterval(() => {
      void this.#holds(id, turnId, attempt).then(
        (holds) => {
          if (holds === 'interrupted') {
            if (!controller.signal.aborted) controller.abort(new Error('the turn was interrupted'))
            spend.flush(core.runningUsd())
          } else if (holds !== 'yes') cutOff(holds)
          else spend.flush(core.runningUsd())
        },
        () => {},
      )
    }, this.#deps.fenceMs ?? FENCE_MS)
    fence.unref()
    let outcome: TurnOutcome = { kind: 'failed', message: 'the turn could not finish' }
    let result: SDKResultMessage | undefined
    try {
      const run = await core.run(prompt)
      clearInterval(fence)
      result = run.result
      const released = parks.released.aborted
      const stopped = released
        ? RELEASED_TEXT
        : controller.signal.aborted
          ? controller.signal.reason instanceof Error
            ? controller.signal.reason.message
            : 'the turn was interrupted'
          : undefined
      const end: TallyInput = {
        stopped,
        interrupted: stopped,
        result,
        refused: result && stopped === undefined ? run.refused : undefined,
        failure: run.failure,
        secrets: run.secrets,
        turnUsd: run.spentUsd,
        // WebFetch's own model calls are in no result, so they are added in every outcome (#2684).
        cutUsd: (stopped !== undefined || !result ? run.cutUsd : 0) + run.nestedUsd,
      }
      const tally = tallyTurn(session, end)
      outcome = tally.outcome
      await this.#write(session, turnId, tally, end, spend, core.runningUsd())
      const why = cutBy()
      // Released, and not since moved or ended: the process is closed (not cut: its mirror lands
      // what Claude Code wrote), and gone once its appends have; its plugin sessions go after, unwaited.
      if (released && (why === undefined || why === 'shutdown')) {
        for (const entry of entries) void this.#deps.core.warm.abandon(entry, RELEASED_TEXT, 'close')
        await this.#deps.core.warm.landed(id)
        return { status: 'released', hostQueue: this.#deps.hostQueue, parked: parks.taken }
      }
      if (why === 'shutdown') throw ApplicationFailure.retryable(CUT_TEXT.shutdown, SHUTTING_DOWN_FAILURE, { pushed: entries.length > 0 })
      if (why !== undefined) throw ApplicationFailure.nonRetryable(`session ${id}'s placement ${attempt} was cut off: ${CUT_TEXT[why]}`, SESSION_GONE)
      // A process the turn let go (it failed, or was recycled) lands its last appends before the
      // turn ends, as a classic turn's does: the next turn's claim would fence them out, and its
      // spawn would then start the session anew over Claude Code's own file of it.
      await this.#deps.core.warm.landed(id)
      return this.#result(tally, run.secrets)
    } catch (err) {
      core.traced.fail(err)
      throw err
    } finally {
      clearInterval(fence)
      control.cancelled.removeEventListener('abort', onCancel)
      core.end(outcome, result)
    }
  }

  /** The turn's parks (§5.4): each opened through the workflow, then held on its row. */
  #gates(turn: {
    session: ClaimedSession
    turnId: string
    control: TurnControl
    stop: AbortSignal
    holds: Holds
    secrets: () => readonly string[]
    shown: (toolUseId: string, toolName: string, input: Record<string, unknown>) => Promise<void>
    trace: GateTrace
    asks: boolean
    endTurn: (why: string) => void
  }): TurnGates {
    const { session, turnId, control, stop, holds, secrets, shown, trace, asks, endTurn } = turn
    const open = control.openInput
    if (!open) {
      return {
        approvalGate: () => Promise.resolve({ approved: false, message: NO_PARKS }),
        questionGate: () => Promise.resolve({ answered: false, message: NO_PARKS }),
        waitForTab: () => Promise.resolve({ back: false as const, message: NO_PARKS }),
      }
    }
    /** Each parked call's request id, by its tool_use id: the audit row's approval. */
    const parkedAs = new Map<string, string>()
    /** The call's entry, if it opened one, cancelled: the turn will not run it (best-effort; the turn's end cancels it anyway). */
    const withdraw = (callId: string, why: string) => {
      void control.withdrawInput?.({ turn_id: turnId, tool_use_id: callId, reason: why }).catch(() => {})
    }
    const park = async (
      call: OpenInputArgs['call'],
      signal: AbortSignal,
      options: { holdMs?: number; opened?: (requestId: string) => void } = {},
    ): Promise<{ requestId: string; row?: Response; early?: OpenInputAnswer['resolution'] }> => {
      let opened: OpenInputAnswer
      try {
        opened = await open({ turn_id: turnId, call }, signal)
      } catch (err) {
        // The workflow may have opened it and the answer was lost.
        if (!stop.aborted) withdraw(call.id, 'the call could not wait for its answer')
        throw err
      }
      parkedAs.set(call.id, opened.request_id)
      // Settled at once: a grant's or the flow's row is there already; a read call or a malformed question has none.
      if (opened.resolution) return { requestId: opened.request_id, row: await this.#row(opened.request_id), early: opened.resolution }
      const rid = opened.request_id
      holds.held.add(rid)
      options.opened?.(rid)
      // Past the hold, the turn lets its process go (§5.4 "Release"); the entry stays open.
      const timer = setTimeout(() => holds.release(), options.holdMs ?? holds.holdMs)
      timer.unref()
      try {
        return { requestId: rid, row: await this.#hold(rid, AbortSignal.any([signal, holds.released])) }
      } catch (err) {
        // Released: the entry stays open, and the gate answers with the interrupting refusal.
        if (!holds.released.aborted && !stop.aborted) withdraw(call.id, 'the call stopped waiting')
        throw err
      } finally {
        clearTimeout(timer)
        holds.held.delete(rid)
      }
    }
    const approvalGate: ApprovalGate = async (request): Promise<ApprovalVerdict> => {
      if (stop.aborted) return { approved: false, message: 'The turn is stopping; nothing was run.' }
      // A copy taken now is what runs: the entry's hash is of this input.
      const input = structuredClone(request.input)
      if (request.agentId === undefined) await shown(request.toolUseId, request.toolName, request.input)
      // The turn's trace, as classic's gate keeps it (approvals/service.ts `gate`): the call's span
      // and the turn's open segment end once the entry is open, and its span context goes on the
      // entry for the decision to link to (#5041).
      const traced = trace.park(request.toolUseId, request.toolName)
      let entered = false
      let parked
      try {
        parked = await park({ id: request.toolUseId, name: request.toolName, input }, AbortSignal.any([stop, request.signal]), {
          opened: (rid) => {
            entered = true
            // Ended at once, as classic's park is (approvals/service.ts): a decision that lands
            // before the write below returns then finds the spans ended, never the resume's (#5045).
            traced.parked(rid)
            void this.#traceEntry(rid, traced.traceparent)
          },
        })
      } catch (err) {
        if (!entered) traced.abandoned()
        // The audit row names the entry the call waited on, as classic's does (approvals/service.ts).
        const waited = parkedAs.get(request.toolUseId)
        const source = waited === undefined ? {} : { approvalId: waited }
        if (holds.released.aborted) return { approved: false, message: RELEASED_CALL, interrupt: true, ...source }
        if (stop.aborted || request.signal.aborted) return { approved: false, message: 'The turn is stopping; nothing was run.', ...source }
        return { approved: false, message: `${request.toolName} could not be put to the user for approval (${describe(err)}), so it was not run.` }
      }
      const { requestId, row, early } = parked
      const outcome = row?.outcome ?? early?.outcome
      const decision = outcome === 'approved' || outcome === 'denied' || outcome === 'expired' ? outcome : 'cancelled'
      // Settled at once (a grant, the flow's deny, a read call): nobody was asked, as classic's consumed approval.
      if (entered) traced.decided({ id: requestId, decision, decisionTraceparent: row?.decision_traceparent ?? null }, outcome === 'approved')
      else traced.abandoned()
      if (outcome === 'approved') return { approved: true, input, approvalId: requestId, decision: 'approved' }
      const said = row?.responder?.kind === 'flow' && outcome === 'denied' ? 'flow_denied' : (outcome ?? 'cancelled')
      return { approved: false, message: refusal(request.toolName, requestId, said, row?.reason), approvalId: requestId, decision }
    }
    /** A `done` summary is classic's (see the header); every other attention request waits through the workflow (attentionGate.ts). */
    const waits = attentionGate({
      session,
      turnId,
      stop,
      secrets,
      park,
      hold: (requestId, signal) => this.#hold(requestId, signal),
      holdMs: holds.holdMs,
      released: holds.released,
      release: () => {
        endTurn(RELEASED_TEXT)
        return { answered: false, message: RELEASED_CALL, interrupt: true }
      },
      endTurn,
      ...(this.#deps.questions ? { questions: this.#deps.questions } : {}),
    })
    const attend = (request: QuestionRequest, attention: AttentionSpec): Promise<QuestionVerdict> => {
      if (attention.reason !== 'done') return waits(request, attention)
      const classic = this.#deps.questions
      if (!classic) return Promise.resolve({ answered: false, message: NO_PARKS })
      return classic.gate({ sessionId: session.id, turnId, secrets, signal: stop, turnStartedAt: session.claimedAt })(request)
    }
    const questionGate: QuestionGate = async (request): Promise<QuestionVerdict> => {
      if (request.attention) return attend(request, request.attention)
      if (stop.aborted) return { answered: false, message: 'The user did not answer: the turn is stopping.' }
      // The card the panel shows, with the turn's secrets redacted as the classic QuestionService does.
      const hidden = secrets()
      const card = request.questions.map((q) => cardOf(q, hidden))
      let parked
      try {
        parked = await park({ id: request.toolUseId, name: GATE_ASK, input: { questions: card } }, AbortSignal.any([stop, request.signal]))
      } catch (err) {
        if (holds.released.aborted) return { answered: false, message: RELEASED_CALL, interrupt: true }
        if (stop.aborted || request.signal.aborted) return { answered: false, message: 'The user did not answer: the turn is stopping.' }
        return { answered: false, message: `The user did not answer: the question could not be asked (${describe(err)}).` }
      }
      const { row, early } = parked
      if (!row) {
        return { answered: false, message: early ? 'The question was not asked: its input is not the shape AskUserQuestion takes.' : 'The user did not answer: the request is no longer recorded.' }
      }
      const answers = Array.isArray(row.response?.answers) ? row.response.answers.filter((a): a is string => typeof a === 'string') : []
      if (row.outcome !== 'answered' || answers.length === 0) return { answered: false, message: `The user did not answer: ${row.reason ?? 'the request was cancelled'}.` }
      // One answer per question, in order: keyed by the model's own words, not the card's.
      return { answered: true, answers: Object.fromEntries(request.questions.map((q, i) => [q.question, answers[i] ?? ''])) }
    }
    return {
      approvalGate,
      approvalFor: (toolUseId) => Promise.resolve(parkedAs.get(toolUseId)),
      // A browser call's wait for its tab, as a classic turn's (sessions/manager.ts): only where the user is asked.
      ...(asks
        ? {
            questionGate,
            waitForTab: waitForTab(questionGate, stop, (requestId) => control.reconnected?.(requestId) ?? Promise.resolve()),
          }
        : {}),
    }
  }

  /**
   * The entry's response row, once it is there: woken by its NOTIFY, read every INPUT_POLL_MS
   * besides; a read that fails (a database blip) is tried again on the next tick, since the hold
   * may last the entry's whole timer. Rejects only when `signal` aborts.
   */
  async #hold(requestId: string, signal: AbortSignal): Promise<Response> {
    for (;;) {
      if (signal.aborted) throw signal.reason instanceof Error ? signal.reason : new Error('the call stopped waiting')
      const row = await this.#row(requestId).catch((err: unknown) => {
        this.#deps.core.stderr?.(`held call ${requestId}: could not read its answer, trying again: ${describe(err)}\n`)
        return undefined
      })
      if (row) return row
      await this.#pause(requestId, signal)
    }
  }

  async #row(requestId: string): Promise<Response | undefined> {
    const [row] = await this.#deps.sql<Response[]>`
      SELECT outcome, response, reason, responder, decision_traceparent FROM ai_input_responses WHERE request_id = ${requestId}`
    return row
  }

  /**
   * The parked call's span context on its entry (#5041): the decision links to it, as classic's
   * links to `ai_approvals.traceparent`. Best effort: an entry the write misses, or one decided
   * before it lands, is decided all the same, its decision a root.
   */
  async #traceEntry(requestId: string, traceparent: string | undefined): Promise<void> {
    if (!traceparent) return
    await this.#deps.sql`UPDATE ai_pending_input SET traceparent = ${traceparent} WHERE request_id = ${requestId}`.catch((err: unknown) => {
      this.#deps.core.stderr?.(`parked call ${requestId}: its span context was not recorded: ${describe(err)}\n`)
    })
  }

  #pause(requestId: string, signal: AbortSignal): Promise<void> {
    return new Promise((resolve) => {
      const set = this.#waiters.get(requestId) ?? new Set()
      this.#waiters.set(requestId, set)
      const done = () => {
        clearTimeout(timer)
        set.delete(done)
        if (set.size === 0 && this.#waiters.get(requestId) === set) this.#waiters.delete(requestId)
        signal.removeEventListener('abort', done)
        resolve()
      }
      const timer = setTimeout(done, this.#deps.inputPollMs ?? INPUT_POLL_MS)
      set.add(done)
      signal.addEventListener('abort', done, { once: true })
    })
  }

  /**
   * Takes the turn for this placement: `turn_attempt` is it from now on, and the mark (the main
   * transcript's last entry before the first placement of `markKey`: the turn, or a parked
   * continuation of it) is set once. The mark, or undefined when the row does not run this turn.
   */
  async #claim(id: string, turnId: string, attempt: string, markKey: string): Promise<number | undefined> {
    const [row] = await this.#deps.sql<{ turn_mark: string }[]>`
      UPDATE ai_sessions SET
        turn_attempt = ${attempt},
        turn_mark = CASE WHEN turn_mark_turn = ${markKey} THEN turn_mark ELSE (
          SELECT coalesce(max(e.id), 0) FROM ai_session_entries e WHERE e.session_id = ai_sessions.id::text AND e.subpath = ''
        ) END,
        turn_mark_turn = ${markKey}
      WHERE id = ${id} AND durable_turn_id = ${turnId} AND status IN ('running', 'waiting_approval', 'waiting_input')
      RETURNING turn_mark::text`
    return row ? Number(row.turn_mark) : undefined
  }

  /**
   * A parked continuation's calls, each decided while the turn was released (§5.4 step 5): an
   * approved call run once (deps.runCall), the others as they ended. What the model is told,
   * in one message: its own process marked each call's outcome unknown, and this settles it.
   */
  async #decided(id: string, calls: readonly ParkedCall[], signal: AbortSignal, heartbeat: () => void): Promise<{ text: string; stop?: string }> {
    const said: string[] = []
    for (const call of calls) {
      if (call.tool === WAIT_FOR_USER) {
        const attended = await this.#attended(call)
        // The turn ends there, as an interrupt ends it: a later parked call is neither run nor told, as a Stop runs none.
        if (attended.stop !== undefined) return { text: '', stop: attended.stop }
        said.push(attended.text)
      } else said.push(await this.#outcome(id, call, signal, heartbeat))
    }
    return { text: `${said.join('\n\n')}\n\nContinue the task.` }
  }

  /**
   * How a released attention request or tab wait ended, in the words its call would have
   * returned (harness/attention.ts, sessions/manager.ts waitForTab); `stop` when its `stop` or
   * `wait` timer ended it, which ends the turn.
   */
  async #attended(call: ParkedCall): Promise<{ text: string; stop?: string }> {
    const { requestId, toolUseId, resolution } = call
    const input = (call.input ?? {}) as AttentionInput
    const row = await this.#row(requestId)
    const reply = replyOf(row)
    const reconnected = resolution.outcome === 'cancelled' && row?.response?.reconnected === true
    const timedOut = resolution.outcome === 'timed_out'
    const onTimeout = input.on_timeout ?? 'proceed'
    if (toolUseId.startsWith(TAB_WAIT_CALL)) {
      const paused = `A browser call (id ${toolUseId.slice(TAB_WAIT_CALL.length)}) was paused waiting for the ScadBuddy tab, and it was not run.`
      const recheck = 'The page may have reloaded or changed while the tab was away: re-check it (browser_status, then browser_snapshot) and call the tool again if it is still what you want.'
      if (reconnected) return { text: `${paused} The session has a connected tab again. ${recheck}` }
      if (reply !== undefined && BACK_REPLIES.includes(reply)) return { text: `${paused} The user said they are back (a tab may not be attached yet). ${recheck}` }
      if (reply === CARRY_ON || reply === DEFAULT_REPLIES[1]) return { text: `${paused} The user replied ${JSON.stringify(reply)}: carry on without the tab for the rest of this turn.` }
      if (reply !== undefined) return { text: `${paused} The user replied ${JSON.stringify(reply)} instead. Act on their reply.` }
      if (timedOut) return { text: `${paused} ${timedOutText(input.timeout_s ?? TAB_WAIT_S)}` }
      return { text: `${paused} ${row?.reason ?? 'The wait was cancelled'}.` }
    }
    const paused = `Your request for the user's attention (id ${toolUseId}) was paused waiting for their reply.`
    if (reply !== undefined) return { text: `${paused} ${answeredText(reply)}` }
    if (reconnected) return { text: `${paused} ${RECONNECTED_TEXT}` }
    if (timedOut && onTimeout === 'proceed') return { text: `${paused} ${timedOutText(input.timeout_s ?? 0)}` }
    if (timedOut) return { text: '', stop: `nobody replied to the attention request in ${input.timeout_s ?? 0} s (on_timeout: ${onTimeout})` }
    return { text: `${paused} The user did not answer: ${row?.reason ?? 'the request was cancelled'}.` }
  }

  async #outcome(id: string, call: ParkedCall, signal: AbortSignal, heartbeat: () => void): Promise<string> {
    const { requestId, toolUseId, tool, resolution } = call
    if (resolution.kind === 'answer') {
      const asked = `Your question (id ${toolUseId}) was paused waiting for the user's answer.`
      const row = await this.#row(requestId)
      const answers = Array.isArray(row?.response?.answers) ? row.response.answers.filter((a): a is string => typeof a === 'string') : []
      if (resolution.outcome !== 'answered' || answers.length === 0) {
        return `${asked} The user did not answer: ${row?.reason ?? (resolution.outcome === 'timed_out' ? 'nobody answered in time' : 'the request was cancelled')}.`
      }
      const questions = questionsOf(call.input)
      return `${asked} The user answered:\n${answers.map((a, i) => (questions[i] ? `${questions[i]}: ${a}` : a)).join('\n')}`
    }
    const paused = `Your call \`${tool}\` (id ${toolUseId}) was paused waiting for approval.`
    if (resolution.outcome !== 'approved') {
      const row = await this.#row(requestId)
      const said = row?.responder?.kind === 'flow' && resolution.outcome === 'denied' ? 'flow_denied' : resolution.outcome
      return `${paused} ${refusal(tool, requestId, said, row?.reason)}`
    }
    const run: DecidedRun = this.#deps.runCall
      ? await this.#deps.runCall({ sessionId: id, requestId, toolUseId, tool, input: call.input, signal, heartbeat })
      : { ran: false, text: 'this replica cannot run a call outside the conversation' }
    if (!run.ran) return `${paused} It was approved, but it was not run: ${run.text}`
    return run.ok
      ? `${paused} It was approved and ScadBuddy ran it once. Result:\n${run.text}`
      : `${paused} It was approved and ScadBuddy ran it once, and it failed:\n${run.text}`
  }

  /** Whether a user entry (the turn's prompt, or a tool result after it) landed in the main transcript after the mark. */
  async #landed(id: string, mark: number): Promise<boolean> {
    return userEntryLanded(this.#deps.sql, id, mark)
  }

  /**
   * Whether the row still runs this turn on this placement: `moved` when another placement holds
   * it, `ended` when the turn is over, `interrupted` when a Stop has asked it to end (§5.6).
   */
  async #holds(id: string, turnId: string, attempt: string): Promise<'yes' | 'moved' | 'ended' | 'interrupted'> {
    const [row] = await this.#deps.sql<{ turn_attempt: string | null; durable_turn_id: string | null; interrupt_requested: boolean }[]>`
      SELECT turn_attempt, durable_turn_id, interrupt_requested FROM ai_sessions WHERE id = ${id}`
    if (!row) return 'ended'
    if (row.turn_attempt !== attempt) return 'moved'
    if (row.durable_turn_id !== turnId) return 'ended'
    return row.interrupt_requested ? 'interrupted' : 'yes'
  }

  async #images(id: string, refs: readonly { name: string }[]) {
    const images = []
    for (const ref of refs) {
      const blob = await this.#deps.images.get(id, ref.name)
      if (blob) images.push(blob)
    }
    return images
  }

  /**
   * The turn's spend and turns on the row (manager.ts `finish`'s columns), as increments over
   * what it wrote as it ran, and its audit row. Whether or not the row still runs this turn or
   * this placement: a placement the workflow ended (its StartToClose) or replaced spent what it
   * spent, on requests no other placement made, and the budget counts it.
   */
  async #write(session: ClaimedSession, turnId: string, tally: TurnTally, end: TallyInput, spend: Spend, runningUsd: number): Promise<void> {
    // With a result, Claude Code's price; without one (cut, failed), what its requests cost at the
    // table's prices, the finished ones too, which no result will ever price.
    const spent = end.result ? tally.costUsd - session.ownCostUsd : Math.max(tally.costUsd - session.ownCostUsd, runningUsd)
    const estimated = end.result ? end.cutUsd : spent
    const wrote = await spend.settled()
    const [written] = await this.#deps.sql<{ id: string }[]>`
      UPDATE ai_sessions
      SET cost_usd = cost_usd + ${spent - wrote}, turns = turns + ${tally.turns - session.turns},
          unpriced_cost_usd = unpriced_cost_usd + ${estimated - wrote}, updated_at = now()
      WHERE id = ${session.id}
      RETURNING id`
    if (!written) return
    // #1922: what the turn cost, as its own audit row (audit/turn.ts turnCostEntry).
    await this.#deps.core.audit?.record(
      turnCostEntry(
        { sessionId: session.id, turnId, actor: session.owner },
        tally.outcome,
        { costUsd: spent, priced: end.result !== undefined, estimatedUsd: estimated },
        end.secrets,
      ),
    )
  }

  /** The end as the workflow writes it: classic's closing events (`tail`, scrubbed) and error code (turnCore.ts `tallyTurn`). */
  #result(tally: TurnTally, secrets: readonly string[]): SessionTurnResult {
    const hostQueue = this.#deps.hostQueue
    const tail = tally.tail.map((e) => scrubForLog(e, secrets))
    const o = tally.outcome
    if (o.kind === 'interrupted') return { status: 'interrupted', hostQueue, tail }
    if (o.kind === 'result' && o.subtype === 'success') return { status: 'done', hostQueue, tail }
    if (o.kind === 'result' && o.subtype === 'error_max_budget_usd') return { status: 'budget_exhausted', hostQueue, tail }
    const said = tail.find((e) => e.type === 'error')
    const code = said?.type === 'error' ? said.code : undefined
    const error = said?.type === 'error' ? said.message : o.kind === 'failed' ? o.message : `the turn stopped (${o.subtype})`
    return { status: tally.status === 'failed' ? 'failed' : 'done', hostQueue, error, tail, ...(code !== undefined ? { code } : {}) }
  }
}

/**
 * What a placement has spent so far, on the row as it goes: each write adds what the turn's
 * requests cost since the last (CoreTurn.runningUsd, the table's prices), to `cost_usd` and to
 * `unpriced_cost_usd` (Claude Code has priced none of it yet), whether or not the row is still
 * this placement's. The end (`TurnHost#write`) replaces it with the turn's own reckoning.
 */
class Spend {
  readonly sql: Sql
  readonly id: string
  #written = 0
  #chain: Promise<void> = Promise.resolve()
  /** Set by `settled`: the end's write takes over, so a flush from a fence tick still in flight writes nothing. */
  #closed = false

  constructor(sql: Sql, id: string) {
    this.sql = sql
    this.id = id
  }

  flush(runningUsd: number): void {
    this.#chain = this.#chain.then(async () => {
      const delta = runningUsd - this.#written
      if (this.#closed || delta <= 0) return
      const [row] = await this.sql<{ id: string }[]>`
        UPDATE ai_sessions SET cost_usd = cost_usd + ${delta}, unpriced_cost_usd = unpriced_cost_usd + ${delta}, updated_at = now()
        WHERE id = ${this.id}
        RETURNING id`
      if (row) this.#written += delta
    }).catch(() => {})
  }

  /** What was written, once the writes in flight have landed; no flush writes after it. */
  async settled(): Promise<number> {
    await this.#chain
    this.#closed = true
    return this.#written
  }
}
