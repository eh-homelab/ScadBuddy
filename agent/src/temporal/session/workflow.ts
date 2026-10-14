import {
  ActivityCancellationType,
  ActivityFailure,
  allHandlersFinished,
  ApplicationFailure,
  CancellationScope,
  condition,
  continueAsNew,
  defineQuery,
  defineSignal,
  defineUpdate,
  isCancellation,
  proxyActivities,
  proxyLocalActivities,
  setHandler,
  TimeoutFailure,
} from '@temporalio/workflow'
import { ASK_USER } from '../../gate/entry.js'
import { END_SIGNAL, INTERRUPT_SIGNAL } from '../../gate/names.js'
import type { Owner } from '../../sessions/protocol.js'
import type { RuntimeSettings } from '../../sessions/runtimeSettings.js'
import { TASK_QUEUE } from '../names.js'
import { type GateHost, type GateTurn, SessionGate } from './gate.js'
import {
  BUDGET_EXHAUSTED,
  type Continuation,
  type FinishTurnInput,
  INTERRUPTED,
  MESSAGE_MAX,
  type Message,
  type SendAnswer,
  type SessionStart,
  type SessionTurnInput,
  type SessionTurnResult,
  type TurnOutcome,
  TURN_MAX_SECONDS,
  type TurnStart,
} from './models.js'
import {
  ACQUIRE_WORKER_ACTIVITY,
  NO_CAPACITY,
  RELEASE_WORKER_ACTIVITY,
  SEND_MESSAGE_UPDATE,
  SESSION_FINISH_ACTIVITY,
  SESSION_SETTINGS_ACTIVITY,
  SESSION_TURN_ACTIVITY,
  SHUTTING_DOWN_FAILURE,
  TURN_QUERY,
} from './names.js'

// AgentSession (spec 2026-10-13 §5.1): the workflow that runs a session's turns as the
// `session_turn` activity, the unified runtime's counterpart of agent-durable's
// DurableSession (session/workflow.py), whose handlers it keeps by name and shape, so
// DurableTurns and DurableGate drive either:
//
//   send_message (Update, `busy` / `ended` refused)  <- _validate_send, send_message
//   respond, cancel_input (Updates), pending_input   <- SessionGate.install
//   interrupt, end (Signals)                         <- interrupt, end
//   turn (Query)                                     <- turn
//
// Not here: `fork_point` (forking an AgentSession session is refused until phase 6),
// `segment_context` (no segment runner) and Task 5.3's `open_input`/`owner_changed`.
//
// The loop: wait for a message or `end`; read the runtime settings; run `session_turn`;
// write the turn's end (`session.finish_turn`); continue as new, so every turn runs the
// build current then (D4: the workflow is AUTO_UPGRADE, and a change to this function
// goes behind `patched()` with test/fixtures/agent_session_histories/ replaying).
//
// Affinity (§5.2), Go's Worker Sessions on worker-specific queues: `home` is the session host
// queue (`agent-sessions.<identity>`) of the replica `acquire_worker` reserved a place on; each
// turn runs there, and a turn whose home has gone (a timeout, ShuttingDown, NoCapacity) is placed
// again: acquire on the shared `agent-tools`, then the turn on the new home, resumed from its
// transcript (`restarted`), at most PLACEMENTS times. An interrupt never moves it. On `end` the
// workflow releases its place on `home`. Parks (§5.4) are Task 5.3's: a turn that ends
// `released` is failed here.

const SHORT = '30s'
/** How many times one turn is placed on a worker (§5.2 "Recovery"). */
const PLACEMENTS = 3
const NO_PARKS = 'the turn parked a call, which this build cannot wait for yet'

const sendUpdate = defineUpdate<SendAnswer, [Message]>(SEND_MESSAGE_UPDATE)
const interruptSignal = defineSignal<[{ reason?: string }?]>(INTERRUPT_SIGNAL)
const endSignal = defineSignal<[unknown?]>(END_SIGNAL)
const turnQuery = defineQuery<string | null>(TURN_QUERY)

type Activities = Record<string, (input?: unknown) => Promise<unknown>>

const local = proxyLocalActivities<{ [SESSION_SETTINGS_ACTIVITY]: () => Promise<RuntimeSettings> }>({ startToCloseTimeout: '10s' })
const finish = proxyActivities<Activities>({ taskQueue: TASK_QUEUE, startToCloseTimeout: SHORT, retry: { maximumInterval: '1m' } })

const acquire = proxyActivities<Activities>({
  taskQueue: TASK_QUEUE,
  scheduleToCloseTimeout: '60s',
  retry: { initialInterval: '1s', maximumInterval: '10s', maximumAttempts: 10 },
})

/** Whether the turn ran past `turn_max_seconds` on its home. */
function overTime(err: unknown): boolean {
  const cause = err instanceof ActivityFailure ? err.cause : err
  return cause instanceof TimeoutFailure && cause.timeoutType === 'START_TO_CLOSE'
}

/** A failure that says the turn's worker is gone or cannot take it: place the turn elsewhere (§5.2). */
export function movesWorker(err: unknown): boolean {
  const cause = err instanceof ActivityFailure ? err.cause : err
  // A home that never started the turn, or stopped answering in it. A turn past its
  // `turn_max_seconds` (StartToClose) is over, not lost: placing it again could triple its spend.
  if (cause instanceof TimeoutFailure) return cause.timeoutType === 'SCHEDULE_TO_START' || cause.timeoutType === 'HEARTBEAT'
  return cause instanceof ApplicationFailure && (cause.type === SHUTTING_DOWN_FAILURE || cause.type === NO_CAPACITY)
}

/** Whether the turn's message had gone into Claude Code before its worker was lost (turnActivity.ts `TurnBeat`). */
function pushedBefore(err: unknown): boolean {
  const cause = err instanceof ActivityFailure ? err.cause : err
  const beat = cause instanceof TimeoutFailure ? cause.lastHeartbeatDetails : cause instanceof ApplicationFailure ? cause.details?.[0] : undefined
  return typeof beat === 'object' && beat !== null && (beat as { pushed?: unknown }).pushed === true
}

/**
 * The continuation of a turn placed again after `err`: `restarted`, so the next worker resumes
 * the transcript with the restart message, once the message had gone in; else the same one, so
 * the message is sent. A parked continuation keeps its calls (Task 5.5 runs them first).
 */
export function mergeRestarted(continuation: Continuation | undefined, err: unknown): Continuation | undefined {
  if (continuation?.kind === 'parked') return continuation
  if (continuation?.kind === 'restarted' || pushedBefore(err)) return { kind: 'restarted' }
  return continuation
}

/** How a turn ended, as `session.finish_turn` writes it. */
type TurnEnd = { outcome: TurnOutcome; message?: string; code?: string }

const busy = (): ApplicationFailure => ApplicationFailure.nonRetryable('the session is running a turn', 'busy')

function failureText(err: unknown): string {
  const cause = err instanceof ActivityFailure ? err.cause : err
  const text = cause instanceof Error ? `${err instanceof Error ? err.message : ''}: ${cause.message}` : String(err)
  return text.slice(0, MESSAGE_MAX)
}

export async function AgentSession(start: SessionStart): Promise<void> {
  let message: Message | undefined
  let turn: TurnStart | undefined = start.turn ?? undefined
  // From a message's acceptance until its turn's end is written.
  let inTurn = turn !== undefined
  let interrupted: string | undefined = start.interrupted ?? undefined
  let ended = false
  let home: string | undefined = start.host_queue ?? undefined
  let running: CancellationScope | undefined

  const host: GateHost = {
    // The turn's calls are decided in its activity (spec §5.4); the gate only holds their entries.
    decide: () => undefined,
    validateDecision: () => undefined,
    refuse: (callId, text) => {
      if (turn) turn.refusals = { ...turn.refusals, [callId]: text }
    },
    turn: (): GateTurn | undefined => (turn ? { author: turn.author, flow: turn.flow ? { runId: turn.flow.run_id, callId: turn.flow.call_id } : undefined } : undefined),
  }
  const gate = new SessionGate({
    sessionId: start.session_id,
    owner: start.owner,
    creator: start.creator,
    host,
    kindOf: (tool: string) => (tool === ASK_USER || tool === 'wait_for_user' ? 'answer' : 'approval'),
    taskQueue: TASK_QUEUE,
  })
  gate.install()

  setHandler(
    sendUpdate,
    async (m: Message): Promise<SendAnswer> => {
      message = m
      inTurn = true
      return { accepted: true, turn_id: m.turn_id }
    },
    {
      validator: (_m: Message) => {
        if (inTurn || message !== undefined) throw busy()
        if (ended) throw ApplicationFailure.nonRetryable('the session was marked done', 'ended')
      },
    },
  )
  setHandler(endSignal, () => {
    ended = true
  })
  setHandler(interruptSignal, () => {
    if (!inTurn) return
    interrupted = INTERRUPTED
    // Before the activity starts the flag alone stops the turn; once it runs, the cancel does.
    running?.cancel()
  })
  setHandler(turnQuery, () => turn?.turn_id ?? message?.turn_id ?? null)

  /**
   * The turn on the session's home, acquired first when there is none; placed again when its
   * worker is lost (`movesWorker`), never on that worker's queue (the affinity guide).
   */
  async function placeTurn(input: SessionTurnInput, settings: RuntimeSettings): Promise<SessionTurnResult> {
    let continuation = input.continuation
    let last: unknown
    for (let cycle = 0; cycle < PLACEMENTS; cycle++) {
      if (!home) home = ((await acquire[ACQUIRE_WORKER_ACTIVITY]!({ sessionId: input.sessionId })) as { hostQueue: string }).hostQueue
      const onHome = proxyActivities<Activities>({
        taskQueue: home,
        scheduleToStartTimeout: '5s',
        heartbeatTimeout: '20s',
        startToCloseTimeout: `${settings.turn_max_seconds}s`,
        retry: { maximumAttempts: 1 },
        cancellationType: ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
      })
      const { continuation: _previous, ...rest } = input
      try {
        return (await onHome[SESSION_TURN_ACTIVITY]!({ ...rest, ...(continuation ? { continuation } : {}) })) as SessionTurnResult
      } catch (err) {
        // An interrupt cancels the turn; it never moves the session (BudgetExhausted, CredentialRefused, SessionGone: the turn's end).
        if (isCancellation(err) || interrupted !== undefined || !movesWorker(err)) throw err
        last = err
        home = undefined
        continuation = mergeRestarted(continuation, err)
      }
    }
    return { status: 'failed', hostQueue: '', error: `the turn could not be placed on a worker: ${failureText(last)}` }
  }

  /** `release_worker` on home (§5.2 "CompleteSession"): best-effort, a home that has gone took its place with it. */
  async function releaseHome(): Promise<void> {
    if (!home) return
    const onHome = proxyActivities<Activities>({ taskQueue: home, scheduleToStartTimeout: '5s', startToCloseTimeout: SHORT, retry: { maximumAttempts: 1 } })
    try {
      await onHome[RELEASE_WORKER_ACTIVITY]!({ sessionId: start.session_id })
    } catch (err) {
      if (isCancellation(err)) throw err
    }
    home = undefined
  }

  async function runTurn(current: TurnStart, continuation: Continuation | undefined): Promise<TurnEnd> {
    // Taken first: a Stop that lands while the settings are read ends the turn before it runs,
    // and its message is spent with it, never run as the next turn (STOPPED_BEFORE_RUN).
    const sent = message
    message = undefined
    const settings = await local[SESSION_SETTINGS_ACTIVITY]()
    if (interrupted !== undefined) return { outcome: 'interrupted' }
    const input: SessionTurnInput = {
      sessionId: start.session_id,
      turnId: current.turn_id,
      ...(sent ? { message: sent } : {}),
      ...(continuation ? { continuation } : {}),
    }
    running = new CancellationScope()
    let outcome: TurnEnd
    try {
      const result = await running.run(() => placeTurn(input, settings))
      if (result.hostQueue) home = result.hostQueue
      // The classic turn's error code, when it wrote one (turnCore.ts `tallyTurn`), is the session's too.
      const said = result.code !== undefined ? { code: result.code, ...(result.error !== undefined ? { message: result.error.slice(0, MESSAGE_MAX) } : {}) } : {}
      if (result.status === 'released') outcome = { outcome: 'failed', message: NO_PARKS }
      else if (result.status === 'failed') outcome = { outcome: 'failed', message: (result.error ?? 'the turn ended without a result').slice(0, MESSAGE_MAX), ...said }
      else if (result.status === 'done') outcome = { outcome: 'done', ...said }
      else outcome = { outcome: result.status }
    } catch (err) {
      if (interrupted !== undefined) outcome = { outcome: 'interrupted' }
      else if (isCancellation(err)) throw err
      else if (err instanceof ActivityFailure && err.cause instanceof ApplicationFailure && err.cause.type === BUDGET_EXHAUSTED) outcome = { outcome: 'budget_exhausted' }
      // Past its wall clock: over, idle, and said, as classic's error_max_turns (spec D5).
      else if (overTime(err)) outcome = { outcome: 'done', code: TURN_MAX_SECONDS, message: `the turn ran past its ${settings.turn_max_seconds} s limit` }
      else outcome = { outcome: 'failed', message: failureText(err) }
    } finally {
      running = undefined
    }
    return outcome
  }

  async function closeTurn(current: TurnStart, result: TurnEnd): Promise<void> {
    const reason = interrupted ?? 'the turn ended'
    await gate.cancelAll(reason)
    await gate.settle(reason)
    await finish[SESSION_FINISH_ACTIVITY]!({
      sessionId: start.session_id,
      turnId: current.turn_id,
      outcome: result.outcome,
      ...(result.message !== undefined ? { message: result.message } : {}),
      ...(result.code !== undefined ? { code: result.code } : {}),
    } satisfies FinishTurnInput)
  }

  let continuation: Continuation | undefined = start.continuation ?? undefined
  for (;;) {
    if (turn !== undefined) {
      // A turn handed over by continue-as-new: its message was sent already.
      continuation ??= { kind: 'restarted' }
    } else {
      await condition(() => message !== undefined || ended)
      if (message === undefined) {
        await releaseHome()
        return
      }
      turn = {
        turn_id: message.turn_id,
        author: message.author as Owner,
        images: message.images,
        system_append: message.system_append,
        refusals: {},
        started_at: new Date().toISOString(),
        flow: message.flow ?? null,
      }
    }
    const current = turn
    await closeTurn(current, await runTurn(current, continuation))
    turn = undefined
    continuation = undefined
    interrupted = undefined
    inTurn = false
    if (ended) {
      await releaseHome()
      await condition(allHandlersFinished)
      return
    }
    if (message === undefined) {
      await condition(allHandlersFinished)
      if (message === undefined) {
        const next: SessionStart = { ...start, ...(home ? { host_queue: home } : {}) }
        delete next.turn
        delete next.interrupted
        delete next.continuation
        await continueAsNew<typeof AgentSession>(next)
      }
    }
  }
}
