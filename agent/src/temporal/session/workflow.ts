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
  patched,
  proxyActivities,
  proxyLocalActivities,
  setHandler,
  TimeoutFailure,
  workflowInfo,
} from '@temporalio/workflow'
import { ASK_USER } from '../../gate/entry.js'
import { END_SIGNAL, INTERRUPT_SIGNAL } from '../../gate/names.js'
import type { Owner, ServerEvent } from '../../sessions/protocol.js'
import type { RuntimeSettings } from '../../sessions/runtimeSettings.js'
import { TASK_QUEUE } from '../names.js'
import { type GateHost, type GateTurn, SessionGate } from './gate.js'
import {
  type AcquireInput,
  BUDGET_EXHAUSTED,
  type Continuation,
  type FinishTurnInput,
  INTERRUPTED,
  MESSAGE_MAX,
  type Message,
  type OpenInputAnswer,
  type OpenInputArgs,
  type SendAnswer,
  type WithdrawInputArgs,
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
  OPEN_INPUT_UPDATE,
  RELEASE_WORKER_ACTIVITY,
  SEND_MESSAGE_UPDATE,
  SESSION_FINISH_ACTIVITY,
  SESSION_SETTINGS_ACTIVITY,
  SESSION_TURN_ACTIVITY,
  SHUTTING_DOWN_FAILURE,
  TURN_QUERY,
  WITHDRAW_INPUT_UPDATE,
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
//   open_input (Update, a call of the running turn)  <- new (§5.4): SessionGate.open
//   withdraw_input (Update, a call it gave up on)    <- new: SessionGate.cancelCall
//
// Not here: `fork_point` (forking an AgentSession session is refused until phase 6),
// `segment_context` (no segment runner) and `owner_changed` (Task 5.6).
//
// The loop: wait for a message or `end`; read the runtime settings and the gate's
// (`gate.settings`, so a Settings change reaches the next turn); run `session_turn`;
// write the turn's end (`session.finish_turn`); continue as new, so every turn runs the
// build current then (D4: the workflow is AUTO_UPGRADE, and a change to this function
// goes behind `patched()` with test/fixtures/agent_session_histories/ replaying).
//
// A message sent once the turn's end is written (the row is idle, so DurableTurns could
// claim it) is taken while the workflow is still closing the turn, and runs as the next
// turn in this run; an `end` that lands while the last handlers finish ends the session
// there, after any message taken before it.
//
// Affinity (§5.2), Go's Worker Sessions on worker-specific queues: `home` is the session host
// queue (`agent-sessions.<identity>`) of the replica `acquire_worker` reserved a place on; each
// turn runs there, and a turn whose home has gone (a timeout, ShuttingDown, NoCapacity) is placed
// again: acquire on the shared `agent-tools` (not one of the homes it was lost on while another
// replica can take it, and the last placement unrestricted), then the turn on the new home,
// resumed from its transcript (`restarted`), at most PLACEMENTS times. Each placement carries
// its attempt id (run id and cycle), so a cut-off one still running stops. An interrupt never
// moves it. On `end` the workflow releases its place on `home`.
//
// Parks (§5.4): the turn's activity sends `open_input` for each call it parks, and holds
// while the entry is answered here (`respond`), times out or is cancelled; the answer
// reaches it as the entry's row in ai_input_responses. A release (Task 5.4) is failed here.

const SHORT = '30s'
/** How many times one turn is placed on a worker (§5.2 "Recovery"). */
const PLACEMENTS = 3
const NO_PARKS = 'the turn parked a call, which this build cannot wait for yet'

const sendUpdate = defineUpdate<SendAnswer, [Message]>(SEND_MESSAGE_UPDATE)
const interruptSignal = defineSignal<[{ reason?: string }?]>(INTERRUPT_SIGNAL)
const endSignal = defineSignal<[unknown?]>(END_SIGNAL)
const turnQuery = defineQuery<string | null>(TURN_QUERY)
const openInputUpdate = defineUpdate<OpenInputAnswer, [OpenInputArgs]>(OPEN_INPUT_UPDATE)
const withdrawInputUpdate = defineUpdate<boolean, [WithdrawInputArgs]>(WITHDRAW_INPUT_UPDATE)

/** `gate.settings` read as each turn starts (Task 5.3). */
const GATE_SETTINGS_PATCH = 'agent-session-gate-settings'
/** An `end` that lands while the last handlers finish ends the session there (Task 5.3). */
const END_WHILE_FINISHING_PATCH = 'agent-session-end-while-finishing'
const THE_TURN_ENDED = 'the turn ended'

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

/**
 * The continuation of a turn placed again: `restarted`, so the next worker resumes the
 * transcript, with the restart message only once the message is in it (sessions/turnHost.ts,
 * which reads the transcript: the cut worker's word cannot be trusted to have reached it). A
 * parked continuation keeps its calls (Task 5.5 runs them first).
 */
export function mergeRestarted(continuation: Continuation | undefined): Continuation {
  return continuation?.kind === 'parked' ? continuation : { kind: 'restarted' }
}

/** How a turn ended, as `session.finish_turn` writes it: the turn's own closing events when its activity built them. */
type TurnEnd = { outcome: TurnOutcome; message?: string; code?: string; tail?: ServerEvent[] }

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
  // From the turn's `session.finish_turn` until it is closed: its end is written, so a message
  // may be taken (it runs next) and an interrupt is the next message's. No call parks then
  // (`runs`: the turn's activity has ended).
  let closing = false
  let interruptNext = false
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
        if (message !== undefined || (inTurn && !closing)) throw busy()
        if (ended) throw ApplicationFailure.nonRetryable('the session was marked done', 'ended')
      },
    },
  )
  setHandler(endSignal, () => {
    ended = true
  })
  setHandler(interruptSignal, () => {
    if (!inTurn) return
    // The turn's end is written: the Stop is for the message taken meanwhile, if any.
    if (closing) {
      if (message !== undefined) interruptNext = true
      return
    }
    interrupted = INTERRUPTED
    // Before the activity starts the flag alone stops the turn; once it runs, the cancel does.
    running?.cancel()
  })
  setHandler(turnQuery, () => turn?.turn_id ?? message?.turn_id ?? null)
  /** Whether turn `turnId`'s activity still runs: only then may a call of it park (closeTurn sees every entry parked before it). */
  const runs = (turnId: string): boolean => running !== undefined && turn?.turn_id === turnId
  const stale = (turnId: string) => ApplicationFailure.nonRetryable(`turn ${String(turnId)} is not running`, 'stale')
  setHandler(
    openInputUpdate,
    async ({ turn_id, call }: OpenInputArgs): Promise<OpenInputAnswer> => {
      // A turn that ended while its call was described writes nothing: the call is settled `cancelled`.
      const requestId = await gate.open({ id: call.id, name: call.name, input: call.input }, () => runs(turn_id))
      // Settled at once: no entry waits, and the activity is told how.
      if (!gate.isParked(requestId)) return { request_id: requestId, resolution: await gate.resolution(requestId) }
      return { request_id: requestId }
    },
    {
      validator: ({ turn_id }: OpenInputArgs) => {
        if (!runs(turn_id)) throw stale(turn_id)
      },
    },
  )
  setHandler(withdrawInputUpdate, ({ tool_use_id, reason }: WithdrawInputArgs) => gate.cancelCall(tool_use_id, reason), {
    validator: ({ turn_id }: WithdrawInputArgs) => {
      if (!runs(turn_id)) throw stale(turn_id)
    },
  })

  /**
   * The turn on the session's home, acquired first when there is none; placed again when its
   * worker is lost (`movesWorker`), on another worker's queue where there is one (the affinity
   * guide): the homes it was lost on are a preference against, not a ban (affinity.ts), and the
   * last placement names none, so a lone replica, restarted in place, still takes its turn back.
   */
  async function placeTurn(input: SessionTurnInput, settings: RuntimeSettings): Promise<SessionTurnResult> {
    let continuation = input.continuation
    let last: unknown
    /** The homes this turn was lost on: acquire_worker places it elsewhere while it can. */
    const lost: string[] = []
    for (let cycle = 0; cycle < PLACEMENTS; cycle++) {
      const { continuation: _previous, ...rest } = input
      try {
        // Inside the try: a replica that refuses (NoCapacity past its retries) costs a placement, as a lost one does.
        if (!home) {
          const exclude = cycle < PLACEMENTS - 1 && lost.length ? { exclude: [...lost] } : {}
          const acquired = (await acquire[ACQUIRE_WORKER_ACTIVITY]!({ sessionId: input.sessionId, ...exclude } satisfies AcquireInput)) as {
            hostQueue: string
            elsewhere?: string[]
          }
          if (!acquired.elsewhere) home = acquired.hostQueue
          else {
            // A home the turn was lost on answered, naming the replicas that could take it instead:
            // the first to reserve a place there (acquire_worker on its own queue) is home.
            for (const queue of acquired.elsewhere) {
              try {
                home = await reserveOn(queue, input.sessionId)
                break
              } catch (err) {
                if (isCancellation(err)) throw err
              }
            }
            // None did: the lost home is asked on its own queue, with no exclusion, so it reserves
            // a place as any home does, or refuses NoCapacity (full), which costs this placement.
            home ??= await reserveOn(acquired.hostQueue, input.sessionId)
          }
        }
        const onHome = proxyActivities<Activities>({
          taskQueue: home,
          scheduleToStartTimeout: '5s',
          heartbeatTimeout: '20s',
          startToCloseTimeout: `${settings.turn_max_seconds}s`,
          retry: { maximumAttempts: 1 },
          cancellationType: ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        })
        // This placement (§5.2): the row records it, and an earlier one that still runs stops.
        const attempt = `${workflowInfo().runId}:${cycle}`
        return (await onHome[SESSION_TURN_ACTIVITY]!({ ...rest, attempt, ...(continuation ? { continuation } : {}) })) as SessionTurnResult
      } catch (err) {
        // An interrupt cancels the turn; it never moves the session (BudgetExhausted, CredentialRefused, SessionGone: the turn's end).
        if (isCancellation(err) || interrupted !== undefined || !movesWorker(err)) throw err
        last = err
        if (home && !lost.includes(home)) lost.push(home)
        home = undefined
        continuation = mergeRestarted(continuation)
      }
    }
    return { status: 'failed', hostQueue: '', error: `the turn could not be placed on a worker: ${failureText(last)}` }
  }

  /** acquire_worker on one replica's own session host queue, with no exclusion: its place there, or its refusal. */
  async function reserveOn(queue: string, sessionId: string): Promise<string> {
    const there = proxyActivities<Activities>({ taskQueue: queue, scheduleToStartTimeout: '5s', startToCloseTimeout: SHORT, retry: { maximumAttempts: 1 } })
    return ((await there[ACQUIRE_WORKER_ACTIVITY]!({ sessionId } satisfies AcquireInput)) as { hostQueue: string }).hostQueue
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
    // The entries' timers as Settings has them now (Python's gate_settings).
    if (patched(GATE_SETTINGS_PATCH)) await gate.loadSettings()
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
      const tail = result.tail?.length ? { tail: result.tail } : {}
      if (result.status === 'released') outcome = { outcome: 'failed', message: NO_PARKS }
      else if (result.status === 'failed') outcome = { outcome: 'failed', message: (result.error ?? 'the turn ended without a result').slice(0, MESSAGE_MAX), ...said, ...tail }
      else if (result.status === 'done') outcome = { outcome: 'done', ...said, ...tail }
      else outcome = { outcome: result.status, ...tail }
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
    const reason = interrupted ?? THE_TURN_ENDED
    await gate.cancelAll(reason)
    await gate.settle(reason)
    // Once this is written the row is idle, and a send may come before it returns.
    closing = true
    await finish[SESSION_FINISH_ACTIVITY]!({
      sessionId: start.session_id,
      turnId: current.turn_id,
      outcome: result.outcome,
      ...(result.message !== undefined ? { message: result.message } : {}),
      ...(result.code !== undefined ? { code: result.code } : {}),
      ...(result.tail ? { tail: result.tail } : {}),
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
    interrupted = interruptNext ? INTERRUPTED : undefined
    interruptNext = false
    closing = false
    // A message taken while the turn closed is the next turn's (it runs below, before an `end`).
    inTurn = message !== undefined
    if (ended && message === undefined) {
      await releaseHome()
      await condition(allHandlersFinished)
      return
    }
    if (message === undefined) {
      await condition(allHandlersFinished)
      // An `end` while the handlers finished: the session ends here, never in a new run.
      if (message === undefined && ended && patched(END_WHILE_FINISHING_PATCH)) {
        await releaseHome()
        await condition(allHandlersFinished)
        return
      }
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
