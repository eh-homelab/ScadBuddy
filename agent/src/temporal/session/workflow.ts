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
  type TurnStart,
} from './models.js'
import { SEND_MESSAGE_UPDATE, SESSION_FINISH_ACTIVITY, SESSION_SETTINGS_ACTIVITY, SESSION_TURN_ACTIVITY, TURN_QUERY } from './names.js'

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
// Recovery of a lost worker (§5.2) and parks (§5.4) are Tasks 5.2 and 5.3: a turn that
// ends `released` is failed here, and a timeout fails the turn.

const SHORT = '30s'
const NO_PARKS = 'the turn parked a call, which this build cannot wait for yet'

const sendUpdate = defineUpdate<SendAnswer, [Message]>(SEND_MESSAGE_UPDATE)
const interruptSignal = defineSignal<[{ reason?: string }?]>(INTERRUPT_SIGNAL)
const endSignal = defineSignal<[unknown?]>(END_SIGNAL)
const turnQuery = defineQuery<string | null>(TURN_QUERY)

type Activities = Record<string, (input?: unknown) => Promise<unknown>>

const local = proxyLocalActivities<{ [SESSION_SETTINGS_ACTIVITY]: () => Promise<RuntimeSettings> }>({ startToCloseTimeout: '10s' })
const finish = proxyActivities<Activities>({ taskQueue: TASK_QUEUE, startToCloseTimeout: SHORT, retry: { maximumInterval: '1m' } })

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

  async function placeTurn(input: SessionTurnInput, settings: RuntimeSettings): Promise<SessionTurnResult> {
    const onHome = proxyActivities<Activities>({
      taskQueue: home ?? TASK_QUEUE,
      scheduleToStartTimeout: '5s',
      heartbeatTimeout: '20s',
      startToCloseTimeout: `${settings.turn_max_seconds}s`,
      retry: { maximumAttempts: 1 },
      cancellationType: ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
    })
    return (await onHome[SESSION_TURN_ACTIVITY]!(input)) as SessionTurnResult
  }

  async function runTurn(current: TurnStart, continuation: Continuation | undefined): Promise<{ outcome: TurnOutcome; message?: string }> {
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
    let outcome: { outcome: TurnOutcome; message?: string }
    try {
      const result = await running.run(() => placeTurn(input, settings))
      if (result.hostQueue) home = result.hostQueue
      if (result.status === 'released') outcome = { outcome: 'failed', message: NO_PARKS }
      else if (result.status === 'failed') outcome = { outcome: 'failed', message: (result.error ?? 'the turn ended without a result').slice(0, MESSAGE_MAX) }
      else outcome = { outcome: result.status }
    } catch (err) {
      if (interrupted !== undefined) outcome = { outcome: 'interrupted' }
      else if (isCancellation(err)) throw err
      else if (err instanceof ActivityFailure && err.cause instanceof ApplicationFailure && err.cause.type === BUDGET_EXHAUSTED) outcome = { outcome: 'budget_exhausted' }
      else outcome = { outcome: 'failed', message: failureText(err) }
    } finally {
      running = undefined
    }
    return outcome
  }

  async function closeTurn(current: TurnStart, result: { outcome: TurnOutcome; message?: string }): Promise<void> {
    const reason = interrupted ?? 'the turn ended'
    await gate.cancelAll(reason)
    await gate.settle(reason)
    await finish[SESSION_FINISH_ACTIVITY]!({
      sessionId: start.session_id,
      turnId: current.turn_id,
      outcome: result.outcome,
      ...(result.message !== undefined ? { message: result.message } : {}),
    } satisfies FinishTurnInput)
  }

  let continuation: Continuation | undefined = start.continuation ?? undefined
  for (;;) {
    if (turn !== undefined) {
      // A turn handed over by continue-as-new: its message was sent already.
      continuation ??= { kind: 'restarted' }
    } else {
      await condition(() => message !== undefined || ended)
      if (message === undefined) return
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
